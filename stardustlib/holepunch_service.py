"""홀펀칭 직접 전송 서비스 (홀펀칭 전송 Phase 2 + 4.2 통합).

하나의 UDP 소켓을 공유해 (1) 랑데부 제어(register/connect/punch, JSON·PUNCH/ACK)와
(2) rudp 데이터(P2P op)를 다중화한다. daemon이 시작 시 이 서비스를 띄워:
- 랑데부에 등록(register)하고 reflexive UDP 주소를 학습, TTL 내 주기적 재등록(keepalive).
- 인바운드 punch 신호를 받으면 상대에게 펀치해 직접 경로를 연다(서버 역할 수신 대기).
- connect_to(peer)로 같은 사용자의 다른 디바이스와 펀치한 뒤, P2pUdpNode.send_op로
  직접 UDP 전송한다(직접 실패 시 호출자가 릴레이로 fallback).

수신 데이터그램 분기: 앞 2바이트가 rudp magic이면 rudp로, 아니면 제어 큐로 보낸다
(JSON 제어와 PUNCH/ACK는 magic과 겹치지 않는다).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

from stardustlib.holepunch import _PUNCH, _PUNCH_ACK, parse_addr
from stardustlib.p2p_udp import P2pUdpNode
from stardustlib.rudp import _MAGIC, RudpEndpoint

logger = logging.getLogger(__name__)

_REREGISTER_INTERVAL = 60.0   # 랑데부 TTL(120s) 내 갱신
_PUNCH_ATTEMPTS = 20
_PUNCH_INTERVAL = 0.2
_CONNECT_TIMEOUT = 5.0
_REGISTER_TIMEOUT = 3.0


class _MuxProtocol(asyncio.DatagramProtocol):
    """수신 데이터그램을 rudp(바이너리 magic 프레임)와 제어(JSON/PUNCH)로 분기."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.on_rudp = None  # callable(data, addr)
        self.control: asyncio.Queue = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data[:2] == _MAGIC and self.on_rudp is not None:
            self.on_rudp(data, addr)
        else:
            self.control.put_nowait((data, addr))


class HolePunchService:
    """공유 UDP 소켓 위의 랑데부 + 펀치 + rudp(P2P op) 직접 전송 서비스."""

    def __init__(
        self,
        server_host: str,
        server_port: int,
        token_provider,
        device_id: str,
        dispatch,
        *,
        local_port: int = 0,
    ) -> None:
        self._server = (server_host, server_port)
        self._token_provider = token_provider  # async () -> str
        self._device_id = device_id
        self._dispatch = dispatch
        self._local_port = local_port
        self._mux: _MuxProtocol | None = None
        self._rudp: RudpEndpoint | None = None
        self._node: P2pUdpNode | None = None
        self._reflexive: tuple[str, int] | None = None
        self._control_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._punch_tasks: set[asyncio.Task] = set()
        self._reg_fut: asyncio.Future | None = None
        self._peer_fut: asyncio.Future | None = None
        self._connect_lock = asyncio.Lock()
        self._punch_events: dict[tuple, asyncio.Event] = {}

    @property
    def reflexive(self) -> tuple[str, int] | None:
        return self._reflexive

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # 랑데부 호스트명을 IPv4로 해석한다. asyncio UDP transport.sendto는 호스트명을
        # 해석하지 않으므로(IP 필요), 호스트명을 그대로 두면 패킷이 잘못 전달돼 응답이
        # 오지 않는다(Windows에서 특히). 해석 실패 시 원래 값 유지(루프백 등).
        host, port = self._server
        try:
            infos = await loop.getaddrinfo(
                host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
            )
            if infos:
                self._server = infos[0][4]  # (ip, port)
        except OSError as e:
            logger.warning("랑데부 호스트 해석 실패(%s): %s", host, e)
        transport, mux = await loop.create_datagram_endpoint(
            _MuxProtocol, local_addr=("0.0.0.0", self._local_port)
        )
        self._mux = mux
        self._rudp = RudpEndpoint(lambda d, a: transport.sendto(d, a))
        mux.on_rudp = self._rudp.feed
        self._node = P2pUdpNode(self._rudp, self._dispatch)
        await self._node.start()
        self._control_task = asyncio.create_task(self._control_loop())
        await self.register()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def stop(self) -> None:
        for task in (self._control_task, self._keepalive_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for t in list(self._punch_tasks):
            t.cancel()
        if self._node is not None:
            await self._node.stop()
        if self._mux is not None and self._mux.transport is not None:
            self._mux.transport.close()

    @property
    def local_port(self) -> int:
        if self._mux is not None and self._mux.transport is not None:
            return self._mux.transport.get_extra_info("sockname")[1]
        return self._local_port

    # ------------------------------------------------------------------
    # 랑데부 제어
    # ------------------------------------------------------------------

    def _sendto(self, addr: tuple[str, int], data: bytes) -> None:
        if self._mux is not None and self._mux.transport is not None:
            self._mux.transport.sendto(data, addr)

    def _send_json(self, addr: tuple[str, int], obj: dict) -> None:
        self._sendto(addr, json.dumps(obj).encode("utf-8"))

    async def register(self, timeout: float = _REGISTER_TIMEOUT) -> tuple | None:
        """랑데부에 등록하고 reflexive 주소를 학습한다(실패 시 None)."""
        token = await self._token_provider()
        loop = asyncio.get_running_loop()
        self._reg_fut = loop.create_future()
        self._send_json(self._server, {
            "op": "register", "token": token, "device_id": self._device_id,
        })
        try:
            self._reflexive = await asyncio.wait_for(self._reg_fut, timeout)
        except asyncio.TimeoutError:
            logger.warning("랑데부 등록 타임아웃")
            return None
        finally:
            self._reg_fut = None
        return self._reflexive

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(_REREGISTER_INTERVAL)
            try:
                await self.register()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 다음 주기 재시도
                logger.debug("랑데부 재등록 실패: %s", e)

    async def connect_to(
        self, peer_device_id: str, timeout: float = _CONNECT_TIMEOUT
    ) -> tuple | None:
        """peer와 펀치해 직접 UDP 경로를 연다. 성공 시 peer 주소, 실패 시 None."""
        async with self._connect_lock:
            token = await self._token_provider()
            loop = asyncio.get_running_loop()
            self._peer_fut = loop.create_future()
            self._send_json(self._server, {
                "op": "connect", "token": token,
                "device_id": self._device_id, "peer": peer_device_id,
            })
            try:
                peer_addr = await asyncio.wait_for(self._peer_fut, timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._peer_fut = None
            if peer_addr is None:
                return None
            if await self._punch(peer_addr):
                return peer_addr
            return None

    async def send_op(
        self, peer_device_id: str, op: str, payload: dict, *, timeout: float = 30.0
    ) -> tuple[int, dict]:
        """peer와 펀치 후 직접 UDP로 op를 전송한다. 펀치 실패 시 OSError."""
        addr = await self.connect_to(peer_device_id)
        if addr is None:
            raise OSError(f"홀펀칭 직접 연결 실패: peer={peer_device_id}")
        assert self._node is not None
        return await self._node.send_op(addr, op, payload, timeout=timeout)

    # ------------------------------------------------------------------
    # 펀치(동시 오픈)
    # ------------------------------------------------------------------

    async def _punch(
        self, addr: tuple[str, int],
        attempts: int = _PUNCH_ATTEMPTS, interval: float = _PUNCH_INTERVAL,
    ) -> bool:
        ev = self._punch_events.setdefault(addr, asyncio.Event())
        for _ in range(attempts):
            self._sendto(addr, _PUNCH)
            try:
                await asyncio.wait_for(ev.wait(), interval)
                return True
            except asyncio.TimeoutError:
                pass
        return ev.is_set()

    def _signal_punch(self, addr: tuple[str, int]) -> None:
        self._punch_events.setdefault(addr, asyncio.Event()).set()

    async def _control_loop(self) -> None:
        assert self._mux is not None
        while True:
            data, addr = await self._mux.control.get()
            if data == _PUNCH:
                self._sendto(addr, _PUNCH_ACK)
                self._signal_punch(addr)
                continue
            if data == _PUNCH_ACK:
                self._signal_punch(addr)
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            self._handle_control(msg)

    def _handle_control(self, msg: dict) -> None:
        op = msg.get("op")
        if op == "registered":
            if self._reg_fut is not None and not self._reg_fut.done():
                self._reg_fut.set_result(parse_addr(msg["reflexive"]))
        elif op == "peer":
            if self._peer_fut is not None and not self._peer_fut.done():
                self._peer_fut.set_result(parse_addr(msg["addr"]))
        elif op == "peer_unavailable":
            if self._peer_fut is not None and not self._peer_fut.done():
                self._peer_fut.set_result(None)
        elif op == "punch":
            # 우리가 펀치 대상 — 상대에게 마주 펀치해 양방향 경로를 연다.
            try:
                peer_addr = parse_addr(msg["addr"])
            except (KeyError, ValueError):
                return
            t = asyncio.create_task(self._punch(peer_addr))
            self._punch_tasks.add(t)
            t.add_done_callback(self._punch_tasks.discard)
        elif op == "error":
            # 랑데부가 토큰 거부 등으로 오류 응답 — 대기 중인 register/connect를 즉시
            # 실패 처리해(타임아웃까지 기다리지 않음) 원인을 로그로 드러낸다.
            logger.warning(
                "랑데부 오류 응답: %s (토큰 거부면 서버 랑데부의 JWT 시크릿/배포 확인)",
                msg.get("error"),
            )
            for fut in (self._reg_fut, self._peer_fut):
                if fut is not None and not fut.done():
                    fut.set_result(None)
