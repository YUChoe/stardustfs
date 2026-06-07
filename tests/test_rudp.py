"""rudp(신뢰성 UDP 메시지 채널) 단위 테스트 — 소켓 없이 가짜 라우터로 검증."""
from __future__ import annotations

import asyncio

import pytest

from stardustlib import rudp
from stardustlib.rudp import RudpEndpoint, _decode, _encode, fragment


# --- 프래그먼트 항등 ---

@pytest.mark.parametrize("size", [0, 1, 1199, 1200, 1201, 4096, 4 * 1024 * 1024 + 7])
def test_fragment_reassemble_identity(size):
    data = bytes((i * 7 + 3) & 0xFF for i in range(size))
    frags = fragment(data, max_payload=1200)
    assert all(len(f) <= 1200 for f in frags)
    assert b"".join(frags) == data
    assert len(frags) >= 1  # 빈 데이터도 1조각


def test_decode_rejects_non_rudp():
    assert _decode(b"") is None
    assert _decode(b"xy\x01\x00\x00\x00\x00\x00\x00\x00\x00") is None  # 잘못된 magic


# --- 가짜 라우터(유실/재정렬 주입) ---

class _Router:
    def __init__(self) -> None:
        self.eps: dict[tuple, RudpEndpoint] = {}
        self.drop = None  # callable(data, src, dst) -> bool

    def make_sendto(self, src):
        def sendto(data: bytes, dst):
            if self.drop is not None and self.drop(data, src, dst):
                return
            target = self.eps.get(dst)
            if target is not None:
                target.feed(data, src)
        return sendto

    def pair(self, **kw):
        a_addr, b_addr = ("A", 1), ("B", 2)
        a = RudpEndpoint(self.make_sendto(a_addr), **kw)
        b = RudpEndpoint(self.make_sendto(b_addr), **kw)
        self.eps[a_addr] = a
        self.eps[b_addr] = b
        return a, a_addr, b, b_addr


@pytest.mark.asyncio
async def test_roundtrip_large_message():
    r = _Router()
    a, _aa, b, b_addr = r.pair(max_payload=4)  # 작은 MTU로 다중 프래그먼트 강제
    payload = b"the quick brown fox" * 100
    await a.send(b_addr, payload)
    addr, _mid, got = await b.recv()
    assert got == payload
    assert addr == ("A", 1)


@pytest.mark.asyncio
async def test_windowed_large_transfer_completes():
    # 작은 윈도우 + 작은 MTU로 다수 프래그먼트를 윈도우 페이싱으로 완주.
    r = _Router()
    a, _aa, b, b_addr = r.pair(max_payload=16, window=8, ack_timeout=0.05)
    payload = bytes((i * 31 + 5) & 0xFF for i in range(4000))  # 250 프래그먼트
    await a.send(b_addr, payload)
    _addr, _mid, got = await b.recv()
    assert got == payload


@pytest.mark.asyncio
async def test_reliable_delivery_under_loss():
    r = _Router()
    a, _aa, b, b_addr = r.pair(max_payload=8, ack_timeout=0.02, max_retries=50)
    # 첫 전송의 일부 DATA 프래그먼트를 유실시킨다(재전송으로 복구되어야 함).
    seen: dict = {}

    def drop(data, src, dst):
        d = _decode(data)
        if d is None:
            return False
        msg_type, msg_id, idx, _cnt, _p = d
        if msg_type == rudp._TYPE_DATA:
            seen[idx] = seen.get(idx, 0) + 1
            return seen[idx] == 1  # 각 프래그먼트의 첫 전송만 드롭
        return False

    r.drop = drop
    payload = bytes(range(256)) * 5
    await a.send(b_addr, payload)
    _addr, _mid, got = await b.recv()
    assert got == payload


@pytest.mark.asyncio
async def test_reorder_reassembly():
    # 프래그먼트가 역순으로 도착해도 idx로 정렬 재조립한다.
    r = _Router()
    a, a_addr, b, b_addr = r.pair(max_payload=4)
    # b에 직접 역순으로 feed(수동) — DATA 프레임 구성
    data = b"0123456789AB"  # 3 프래그먼트(4바이트씩)
    frames = [
        _encode(rudp._TYPE_DATA, 7, idx, 3, data[idx * 4:idx * 4 + 4])
        for idx in range(3)
    ]
    for f in reversed(frames):
        b.feed(f, a_addr)
    _addr, mid, got = await b.recv()
    assert mid == 7 and got == data


@pytest.mark.asyncio
async def test_timeout_when_all_dropped():
    r = _Router()
    a, _aa, _b, b_addr = r.pair(max_payload=8, ack_timeout=0.01, max_retries=3)
    r.drop = lambda data, src, dst: True  # 전부 유실
    with pytest.raises(TimeoutError):
        await a.send(b_addr, b"never arrives")


@pytest.mark.asyncio
async def test_duplicate_data_delivered_once():
    # 완성 후 재전송된 DATA가 또 와도 중복 전달하지 않는다(ACK는 보냄).
    r = _Router()
    a, a_addr, b, b_addr = r.pair(max_payload=64)
    frame = _encode(rudp._TYPE_DATA, 9, 0, 1, b"hello")
    b.feed(frame, a_addr)
    b.feed(frame, a_addr)  # 중복
    _addr, _mid, got = await b.recv()
    assert got == b"hello"
    assert b._inbox.qsize() == 0  # 두 번째는 전달되지 않음
