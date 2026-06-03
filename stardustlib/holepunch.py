"""순수 파이썬 UDP 홀펀칭 (C 의존 없음).

서버 랑데부로 peer의 reflexive UDP 주소를 얻고, 양쪽이 동시에 UDP 패킷을 발사해
NAT 매핑을 연다(동시 오픈). 한 세션은 단일 UDP 소켓을 소유하므로, 등록 시 학습된
reflexive 매핑이 곧 punch에 사용되는 매핑이다.

punch 성공 시 직접 UDP 경로가 열렸음을 의미하고, 실패(symmetric/CGNAT/이중 NAT
등) 시 호출자가 서버 릴레이로 fallback 한다(보장된 fallback). 데이터 전송 자체의
HTTP→UDP 통합은 별도 범위다.

랑데부 메시지(JSON over UDP) 및 punch 패킷은 app/rendezvous.py와 짝을 이룬다.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

DEFAULT_RENDEZVOUS_PORT = 9091
_PUNCH = b"SDFS-PUNCH"
_PUNCH_ACK = b"SDFS-ACK"


def parse_addr(text: str) -> tuple[str, int]:
    """"ip:port" 문자열을 (ip, port) 튜플로 파싱한다."""
    host, _, port = text.rpartition(":")
    return host, int(port)


class _PunchProtocol(asyncio.DatagramProtocol):
    """수신 데이터그램을 큐로 전달하는 단순 프로토콜."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queue.put_nowait((data, addr))


class HolePunchSession:
    """단일 UDP 소켓으로 랑데부 등록 + 동시 오픈을 수행하는 세션."""

    def __init__(
        self,
        server_host: str,
        server_port: int,
        token: str,
        device_id: str,
        *,
        local_port: int = 0,
    ) -> None:
        self._server = (server_host, server_port)
        self._token = token
        self._device_id = device_id
        self._local_port = local_port
        self._transport: asyncio.DatagramTransport | None = None
        self._proto: _PunchProtocol | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, self._proto = await loop.create_datagram_endpoint(
            _PunchProtocol, local_addr=("0.0.0.0", self._local_port)
        )

    @property
    def local_port(self) -> int:
        if self._transport is not None:
            return self._transport.get_extra_info("sockname")[1]
        return self._local_port

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ------------------------------------------------------------------
    # 랑데부 통신
    # ------------------------------------------------------------------

    def _send_json(self, addr: tuple[str, int], obj: dict) -> None:
        assert self._transport is not None
        self._transport.sendto(json.dumps(obj).encode("utf-8"), addr)

    async def _recv_json(self, timeout: float) -> dict | None:
        """JSON 제어 메시지를 기다린다. 비-JSON(punch) 패킷은 건너뛴다."""
        assert self._proto is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                data, _addr = await asyncio.wait_for(
                    self._proto.queue.get(), remaining
                )
            except asyncio.TimeoutError:
                return None
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue  # punch 패킷 등 — 무시
            if isinstance(msg, dict):
                return msg

    async def register(self, *, timeout: float = 3.0) -> tuple[str, int] | None:
        """랑데부 서버에 등록하고 reflexive(ip, port)를 반환한다(실패 시 None)."""
        self._send_json(
            self._server,
            {"op": "register", "token": self._token,
             "device_id": self._device_id},
        )
        msg = await self._recv_json(timeout)
        if msg is None or msg.get("op") != "registered":
            return None
        return parse_addr(msg["reflexive"])

    async def request_peer(
        self, peer_id: str, *, timeout: float = 3.0
    ) -> tuple[str, int] | None:
        """peer로의 connect를 요청하고 peer reflexive 주소를 반환한다(실패 시 None)."""
        self._send_json(
            self._server,
            {"op": "connect", "token": self._token,
             "device_id": self._device_id, "peer": peer_id},
        )
        msg = await self._recv_json(timeout)
        if msg is None or msg.get("op") != "peer":
            return None
        return parse_addr(msg["addr"])

    # ------------------------------------------------------------------
    # 동시 오픈 (punch)
    # ------------------------------------------------------------------

    async def punch(
        self,
        peer_addr: tuple[str, int],
        *,
        attempts: int = 20,
        interval: float = 0.25,
    ) -> bool:
        """peer와 동시에 UDP 패킷을 교환해 직접 경로를 연다.

        peer의 PUNCH를 받으면 ACK로 응답하고, ACK를 받으면 성공(True)을 반환한다.
        attempts*interval 안에 양방향 교환이 안 되면 False(호출자가 릴레이 fallback).
        """
        assert self._transport is not None and self._proto is not None
        loop = asyncio.get_running_loop()
        for _ in range(attempts):
            self._transport.sendto(_PUNCH, peer_addr)
            deadline = loop.time() + interval
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    data, addr = await asyncio.wait_for(
                        self._proto.queue.get(), remaining
                    )
                except asyncio.TimeoutError:
                    break
                if data == _PUNCH_ACK:
                    return True
                if data == _PUNCH:
                    # peer의 punch 수신 → ACK 응답(양쪽이 성공 수렴)
                    self._transport.sendto(_PUNCH_ACK, addr)
        return False
