"""HolePunchService e2e — 로컬호스트 UDP로 랑데부 등록→connect→punch→rudp op 왕복.

NAT 없는 로컬호스트에서 전체 직접 전송 스택(공유 소켓 demux + 랑데부 + 펀치 + rudp +
P2P op)을 검증한다. 실제 NAT 통과는 환경 의존이라 단위 테스트 범위 밖이다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from stardustlib.holepunch_service import HolePunchService


class _FakeRendezvous:
    """서버 app/rendezvous.py와 동일 메시지 형식의 인테스트 랑데부."""

    def __init__(self) -> None:
        self.registry: dict[str, tuple] = {}
        self.transport = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        rv = self

        class _P(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                rv.transport = transport

            def datagram_received(self, data, addr):
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return
                op = msg.get("op")
                if op == "register":
                    rv.registry[msg["device_id"]] = addr
                    rv._send(addr, {"op": "registered",
                                    "reflexive": f"{addr[0]}:{addr[1]}"})
                elif op == "connect":
                    rv.registry[msg["device_id"]] = addr
                    peer = rv.registry.get(msg["peer"])
                    if peer is None:
                        rv._send(addr, {"op": "peer_unavailable"})
                    else:
                        rv._send(addr, {"op": "peer",
                                        "addr": f"{peer[0]}:{peer[1]}"})
                        rv._send(peer, {"op": "punch",
                                        "addr": f"{addr[0]}:{addr[1]}"})

        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _P(), local_addr=("127.0.0.1", 0)
        )

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    def _send(self, addr, obj) -> None:
        self.transport.sendto(json.dumps(obj).encode("utf-8"), addr)

    def close(self) -> None:
        if self.transport:
            self.transport.close()


async def _token():
    return "tok"


@pytest.mark.asyncio
async def test_register_connect_punch_send_op_roundtrip():
    rv = _FakeRendezvous()
    await rv.start()

    async def dispatch(op, payload):
        return (200, {"op": op, "echo": payload.get("n")})

    a = HolePunchService("127.0.0.1", rv.port, _token, "devA", dispatch)
    b = HolePunchService("127.0.0.1", rv.port, _token, "devB", dispatch)
    await a.start()
    await b.start()
    try:
        assert a.reflexive == ("127.0.0.1", a.local_port)
        assert b.reflexive == ("127.0.0.1", b.local_port)
        # A가 B로 펀치 후 직접 UDP op 전송
        status, result = await a.send_op("devB", "ping", {"n": 42}, timeout=10)
        assert status == 200
        assert result == {"op": "ping", "echo": 42}
    finally:
        await a.stop()
        await b.stop()
        rv.close()


@pytest.mark.asyncio
async def test_register_resolves_hostname():
    # 호스트명(localhost)을 IP로 해석해 전송해야 등록이 성공한다.
    rv = _FakeRendezvous()
    await rv.start()

    async def dispatch(op, payload):
        return (200, {})

    a = HolePunchService("localhost", rv.port, _token, "devA", dispatch)
    await a.start()
    try:
        assert a.reflexive is not None  # 해석·등록 성공(타임아웃이면 None)
        assert a.reflexive[0] == "127.0.0.1"
    finally:
        await a.stop()
        rv.close()


@pytest.mark.asyncio
async def test_punch_state_cleaned_after_roundtrip():
    """펀치 성공 후 per-peer 펀치 상태(_punch_events)가 양쪽 모두 정리된다."""
    rv = _FakeRendezvous()
    await rv.start()

    async def dispatch(op, payload):
        return (200, {})

    a = HolePunchService("127.0.0.1", rv.port, _token, "devA", dispatch)
    b = HolePunchService("127.0.0.1", rv.port, _token, "devB", dispatch)
    await a.start()
    await b.start()
    try:
        status, _ = await a.send_op("devB", "ping", {}, timeout=10)
        assert status == 200
        await asyncio.sleep(0.3)  # 응답측(B) 펀치 태스크 정리 대기
        assert a._punch_events == {}
        assert b._punch_events == {}
    finally:
        await a.stop()
        await b.stop()
        rv.close()


@pytest.mark.asyncio
async def test_offline_peer_leaves_no_session():
    """오프라인 피어로의 connect는 None을 반환하고 펀치 세션을 남기지 않는다."""
    rv = _FakeRendezvous()
    await rv.start()

    async def dispatch(op, payload):
        return (200, {})

    a = HolePunchService("127.0.0.1", rv.port, _token, "devA", dispatch)
    await a.start()
    try:
        assert await a.connect_to("ghost", timeout=2) is None
        assert a._punch_events == {}
    finally:
        await a.stop()
        rv.close()


@pytest.mark.asyncio
async def test_connect_unavailable_peer_returns_none():
    rv = _FakeRendezvous()
    await rv.start()

    async def dispatch(op, payload):
        return (200, {})

    a = HolePunchService("127.0.0.1", rv.port, _token, "devA", dispatch)
    await a.start()
    try:
        addr = await a.connect_to("ghost", timeout=2)
        assert addr is None
        with pytest.raises(OSError):
            await a.send_op("ghost", "ping", {}, timeout=2)
    finally:
        await a.stop()
        rv.close()
