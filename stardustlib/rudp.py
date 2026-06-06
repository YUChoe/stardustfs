"""신뢰성 UDP 메시지 채널 (순수 파이썬, C 의존 없음).

UDP 홀펀칭으로 연 직접 경로 위에서 임의 크기 메시지(요청/응답, 4MiB 청크 포함)를
신뢰성 있게 주고받기 위한 최소 전송 계층이다. 메시지를 MTU 이하 데이터그램으로
분할(fragment)하고, 프래그먼트별 ACK + 타임아웃 재전송으로 유실을 복구하며,
수신 측에서 재조립한다. 한 소켓에서 msg_id로 다중 메시지를 다중화한다.

핵심 로직(프레이밍/재전송/재조립)은 소켓에 의존하지 않도록 `sendto` 콜러블로
주입받아, 가짜 전송(유실·재정렬 주입)으로 단위 테스트할 수 있다. 실제 소켓 연동은
RudpProtocol(asyncio.DatagramProtocol)로 얇게 감싼다.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable

logger = logging.getLogger(__name__)

_MAGIC = b"RU"
_TYPE_DATA = 1
_TYPE_ACK = 2
# magic(2) + type(1) + msg_id(uint32) + frag_idx(uint16) + frag_count(uint16)
_HEADER = struct.Struct(">2sBIHH")
_HEADER_LEN = _HEADER.size  # 11

DEFAULT_MAX_PAYLOAD = 1200   # MTU 보수치(헤더 포함 < 1280 IPv6 최소 MTU)
DEFAULT_ACK_TIMEOUT = 0.3    # 초 — 미-ack 프래그먼트 재전송 대기
DEFAULT_MAX_RETRIES = 10     # 라운드 상한(초과 시 TimeoutError)
_MAX_FRAGMENTS = 65535       # frag_count(uint16) 한계


def _encode(
    msg_type: int, msg_id: int, frag_idx: int, frag_count: int, payload: bytes
) -> bytes:
    return _HEADER.pack(_MAGIC, msg_type, msg_id, frag_idx, frag_count) + payload


def _decode(frame: bytes) -> tuple[int, int, int, int, bytes] | None:
    """프레임을 (type, msg_id, frag_idx, frag_count, payload)로 디코드. 무효면 None."""
    if len(frame) < _HEADER_LEN:
        return None
    magic, msg_type, msg_id, frag_idx, frag_count = _HEADER.unpack_from(frame)
    if magic != _MAGIC:
        return None
    return msg_type, msg_id, frag_idx, frag_count, frame[_HEADER_LEN:]


def fragment(data: bytes, max_payload: int = DEFAULT_MAX_PAYLOAD) -> list[bytes]:
    """data를 max_payload 이하 조각으로 나눈다. 빈 데이터는 [b""](1조각)."""
    if not data:
        return [b""]
    return [data[i:i + max_payload] for i in range(0, len(data), max_payload)]


class _SendState:
    """한 송신 메시지의 ACK 진행 상태."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.acked: set[int] = set()
        self.event = asyncio.Event()

    def all_acked(self) -> bool:
        return len(self.acked) >= self.count


class RudpEndpoint:
    """sendto 콜러블 위에서 신뢰성 메시지 송수신을 제공하는 엔드포인트.

    feed(data, addr)로 수신 데이터그램을 주입하면 ACK 처리/재조립을 수행하고,
    완성된 메시지는 recv()로 꺼낸다. send(addr, data)는 전량 ACK까지 대기한다.
    """

    def __init__(
        self,
        sendto: Callable[[bytes, tuple[str, int]], None],
        *,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._sendto = sendto
        self._max_payload = max_payload
        self._ack_timeout = ack_timeout
        self._max_retries = max_retries
        self._next_id = 1
        self._send_states: dict[int, _SendState] = {}
        # 수신 재조립 버퍼: (addr, msg_id) → {idx: payload}
        self._recv: dict[tuple, dict[int, bytes]] = {}
        # 이미 완성·전달한 메시지(재전송 DATA의 중복 전달 방지). ACK는 계속 보낸다.
        self._delivered: set[tuple] = set()
        self._inbox: asyncio.Queue = asyncio.Queue()

    def _alloc_id(self) -> int:
        mid = self._next_id
        self._next_id = self._next_id + 1 if self._next_id < 0xFFFFFFFF else 1
        return mid

    # ------------------------------------------------------------------
    # 수신 처리
    # ------------------------------------------------------------------

    def feed(self, data: bytes, addr: tuple[str, int]) -> None:
        """수신 데이터그램 1개를 처리한다(소켓/가짜 전송 공통 진입점)."""
        decoded = _decode(data)
        if decoded is None:
            return
        msg_type, msg_id, frag_idx, frag_count, payload = decoded
        if msg_type == _TYPE_ACK:
            self._on_ack(msg_id, frag_idx)
        elif msg_type == _TYPE_DATA:
            self._on_data(addr, msg_id, frag_idx, frag_count, payload)

    def _on_ack(self, msg_id: int, frag_idx: int) -> None:
        state = self._send_states.get(msg_id)
        if state is None:
            return
        state.acked.add(frag_idx)
        state.event.set()

    def _on_data(
        self, addr: tuple, msg_id: int, frag_idx: int,
        frag_count: int, payload: bytes,
    ) -> None:
        key = (addr, msg_id)
        # 재전송으로 ACK가 유실됐을 수 있으므로 완성 후에도 항상 ACK한다.
        self._sendto(_encode(_TYPE_ACK, msg_id, frag_idx, frag_count, b""), addr)
        if key in self._delivered:
            return
        buf = self._recv.setdefault(key, {})
        buf[frag_idx] = payload
        if len(buf) < frag_count:
            return
        # 모든 프래그먼트 수신 → 재조립 후 전달(1회)
        message = b"".join(buf[i] for i in range(frag_count))
        self._delivered.add(key)
        self._recv.pop(key, None)
        self._inbox.put_nowait((addr, msg_id, message))

    async def recv(self) -> tuple[tuple, int, bytes]:
        """완성된 수신 메시지 (addr, msg_id, data)를 하나 꺼낸다."""
        return await self._inbox.get()

    # ------------------------------------------------------------------
    # 송신 (신뢰 전달)
    # ------------------------------------------------------------------

    async def send(
        self, addr: tuple[str, int], data: bytes, *, msg_id: int | None = None
    ) -> int:
        """data를 신뢰성 있게 전송한다. 전량 ACK되면 msg_id 반환, 실패 시 TimeoutError.

        msg_id를 주면(요청에 대한 응답을 같은 id로 에코) 그 id를 쓰고, 없으면 새로 할당.
        """
        if msg_id is None:
            msg_id = self._alloc_id()
        frags = fragment(data, self._max_payload)
        if len(frags) > _MAX_FRAGMENTS:
            raise ValueError(
                f"메시지가 너무 큼: {len(frags)} 프래그먼트 > {_MAX_FRAGMENTS}"
            )
        count = len(frags)
        state = _SendState(count)
        self._send_states[msg_id] = state
        try:
            for _attempt in range(self._max_retries):
                for idx, payload in enumerate(frags):
                    if idx not in state.acked:
                        self._sendto(
                            _encode(_TYPE_DATA, msg_id, idx, count, payload), addr
                        )
                if state.all_acked():
                    return msg_id
                state.event.clear()
                try:
                    await asyncio.wait_for(state.event.wait(), self._ack_timeout)
                except asyncio.TimeoutError:
                    pass
                if state.all_acked():
                    return msg_id
            raise TimeoutError(
                f"rudp 전송 실패 msg_id={msg_id}: "
                f"{len(state.acked)}/{count} 프래그먼트만 ACK됨"
            )
        finally:
            self._send_states.pop(msg_id, None)


class RudpProtocol(asyncio.DatagramProtocol):
    """asyncio UDP 소켓을 RudpEndpoint에 연결하는 얇은 어댑터.

    transport.sendto를 endpoint의 sendto로 주입하고, 수신 데이터그램을 endpoint.feed로
    전달한다. 펀치 제어 패킷(JSON/PUNCH) 등 rudp 프레임이 아닌 것은 _decode가 None을
    반환하므로 무시된다(같은 소켓 공유 가능).
    """

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.endpoint: RudpEndpoint | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.endpoint = RudpEndpoint(self._sendto)

    def _sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is not None:
            self.transport.sendto(data, addr)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.endpoint is not None:
            self.endpoint.feed(data, addr)
