"""UDP 홀펀칭(동시 오픈) + 랑데부 통신 테스트.

NAT 없는 로컬호스트에서 프로토콜 정확성을 검증한다. 실제 NAT 통과는 환경 의존이라
단위 테스트 범위 밖이며, punch 실패 시 호출자가 릴레이로 fallback 한다(5.3).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from stardustlib.holepunch import HolePunchSession, parse_addr


def test_parse_addr():
    assert parse_addr("10.0.0.1:9091") == ("10.0.0.1", 9091)


@pytest.mark.asyncio
async def test_punch_succeeds_simultaneous_open():
    a = HolePunchSession("127.0.0.1", 0, "tok", "da")
    b = HolePunchSession("127.0.0.1", 0, "tok", "db")
    await a.open()
    await b.open()
    a_peer = ("127.0.0.1", b.local_port)
    b_peer = ("127.0.0.1", a.local_port)
    try:
        ra, rb = await asyncio.gather(
            a.punch(a_peer, attempts=20, interval=0.1),
            b.punch(b_peer, attempts=20, interval=0.1),
        )
        assert ra is True and rb is True
    finally:
        a.close()
        b.close()


@pytest.mark.asyncio
async def test_punch_fails_without_peer():
    a = HolePunchSession("127.0.0.1", 0, "tok", "da")
    await a.open()
    try:
        # 응답 없는 주소(루프백의 닫힌 포트) → 타임아웃 → False
        ok = await a.punch(("127.0.0.1", 1), attempts=3, interval=0.05)
        assert ok is False
    finally:
        a.close()


# --- 인테스트 랑데부 fake (서버 프로토콜과 동일 메시지 형식) ---


class _Rendezvous:
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
                msg = json.loads(data.decode("utf-8"))
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


@pytest.mark.asyncio
async def test_register_and_request_peer_via_rendezvous():
    rv = _Rendezvous()
    await rv.start()
    a = HolePunchSession("127.0.0.1", rv.port, "tok", "da")
    b = HolePunchSession("127.0.0.1", rv.port, "tok", "db")
    await a.open()
    await b.open()
    try:
        refl_a = await a.register(timeout=2.0)
        assert refl_a == ("127.0.0.1", a.local_port)
        refl_b = await b.register(timeout=2.0)
        assert refl_b == ("127.0.0.1", b.local_port)

        # a가 b로 connect → b의 reflexive 주소 회신
        peer = await a.request_peer("db", timeout=2.0)
        assert peer == ("127.0.0.1", b.local_port)
    finally:
        a.close()
        b.close()
        rv.close()


@pytest.mark.asyncio
async def test_request_peer_unavailable_returns_none():
    rv = _Rendezvous()
    await rv.start()
    a = HolePunchSession("127.0.0.1", rv.port, "tok", "da")
    await a.open()
    try:
        await a.register(timeout=2.0)
        peer = await a.request_peer("ghost", timeout=2.0)
        assert peer is None
    finally:
        a.close()
        rv.close()
