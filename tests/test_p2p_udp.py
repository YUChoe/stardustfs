"""p2p_udp(rudp 위 P2P op) 단위 테스트 — 소켓 없이 가짜 라우터로 2노드 왕복."""
from __future__ import annotations

import base64

import pytest

from stardustlib.p2p_udp import P2pUdpNode
from stardustlib.rudp import RudpEndpoint


class _Router:
    def __init__(self) -> None:
        self.eps: dict[tuple, RudpEndpoint] = {}
        self.drop = None

    def make_sendto(self, src):
        def sendto(data: bytes, dst):
            if self.drop is not None and self.drop(data, src, dst):
                return
            target = self.eps.get(dst)
            if target is not None:
                target.feed(data, src)
        return sendto

    def endpoint(self, addr, **kw):
        ep = RudpEndpoint(self.make_sendto(addr), **kw)
        self.eps[addr] = ep
        return ep


def _echo_dispatch(replies: dict):
    async def dispatch(op, payload):
        return replies.get(op, (200, {"op": op, "payload": payload}))
    return dispatch


@pytest.mark.asyncio
async def test_op_roundtrip_small():
    r = _Router()
    server_addr, client_addr = ("S", 1), ("C", 2)
    srv = P2pUdpNode(r.endpoint(server_addr), _echo_dispatch({}))
    cli = P2pUdpNode(r.endpoint(client_addr), _echo_dispatch({}))
    await srv.start()
    await cli.start()
    try:
        status, result = await cli.send_op(
            server_addr, "exists", {"physical_path": "f.bin"}, timeout=5
        )
        assert status == 200
        assert result == {"op": "exists", "payload": {"physical_path": "f.bin"}}
    finally:
        await srv.stop()
        await cli.stop()


@pytest.mark.asyncio
async def test_op_roundtrip_large_chunk():
    # 4MiB 청크(base64)를 직접 UDP로 왕복 — rudp 분할·재조립을 통과해야 한다.
    r = _Router()
    server_addr, client_addr = ("S", 1), ("C", 2)
    chunk = bytes((i * 13 + 1) & 0xFF for i in range(4 * 1024 * 1024))
    b64 = base64.b64encode(chunk).decode("ascii")

    async def store_dispatch(op, payload):
        assert op == "replica_store"
        # 서버가 받은 data가 원본과 일치하는지 확인 후 바이트 수 응답
        got = base64.b64decode(payload["data"])
        return (200, {"bytes_written": len(got), "ok": got == chunk})

    srv = P2pUdpNode(r.endpoint(server_addr, max_payload=1200), store_dispatch)
    cli = P2pUdpNode(r.endpoint(client_addr, max_payload=1200),
                     _echo_dispatch({}))
    await srv.start()
    await cli.start()
    try:
        status, result = await cli.send_op(
            server_addr, "replica_store",
            {"chunk_id": "a" * 64, "data": b64}, timeout=30,
        )
        assert status == 200
        assert result["ok"] is True
        assert result["bytes_written"] == len(chunk)
    finally:
        await srv.stop()
        await cli.stop()


@pytest.mark.asyncio
async def test_dispatch_error_returns_500():
    r = _Router()
    server_addr, client_addr = ("S", 1), ("C", 2)

    async def boom(op, payload):
        raise RuntimeError("boom")

    srv = P2pUdpNode(r.endpoint(server_addr), boom)
    cli = P2pUdpNode(r.endpoint(client_addr), _echo_dispatch({}))
    await srv.start()
    await cli.start()
    try:
        status, result = await cli.send_op(server_addr, "read", {}, timeout=5)
        assert status == 500 and "error" in result
    finally:
        await srv.stop()
        await cli.stop()


@pytest.mark.asyncio
async def test_concurrent_ops_matched_by_msg_id():
    # 동시 다중 요청이 msg_id로 올바르게 매칭되는지.
    r = _Router()
    server_addr, client_addr = ("S", 1), ("C", 2)

    async def dispatch(op, payload):
        return (200, {"n": payload["n"]})

    srv = P2pUdpNode(r.endpoint(server_addr), dispatch)
    cli = P2pUdpNode(r.endpoint(client_addr), _echo_dispatch({}))
    await srv.start()
    await cli.start()
    try:
        import asyncio
        results = await asyncio.gather(*[
            cli.send_op(server_addr, "ping", {"n": i}, timeout=5)
            for i in range(10)
        ])
        assert [res[1]["n"] for res in results] == list(range(10))
    finally:
        await srv.stop()
        await cli.stop()
