"""리모트 대용량 파일 청크 전송 테스트.

rudp(~78.6MB)·홀더(100MB)·릴레이 한계를 넘는 파일을 4 MiB 청크로 나눠 쓰고 읽는
경로를 검증한다. 계층별로:
- storage_source: write_chunk/read_chunk (offset=0 생성, offset>0 seek, 용량 부족).
- p2p_server: _op_write_chunk/_op_read_chunk (total_size로 소스 선택, 507, 404).
- remote_source: _push_chunked/_read_chunked 라운드트립 + 중간 실패 rollback.
"""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock

import pytest

from stardustlib.auth_client import AuthClient
from stardustlib.jbod_manager import JBODManager
from stardustlib.p2p_server import P2PServer
from stardustlib.remote_source import REMOTE_CHUNK_SIZE, RemoteSource
from stardustlib.storage_source import DirectorySource, LoopbackSource


def _loopback(tmp_path, name="vault.img", size=200 * 1024 * 1024):
    src = LoopbackSource(name, str(tmp_path / name), size)
    src.initialize()
    return src


# ---------------------------------------------------------------------------
# storage_source: write_chunk / read_chunk
# ---------------------------------------------------------------------------

def test_loopback_chunk_roundtrip(tmp_path):
    src = _loopback(tmp_path)
    data = os.urandom(10 * 1024 * 1024 + 123)  # 4 MiB 배수 아님
    total = len(data)
    for off in range(0, total, REMOTE_CHUNK_SIZE):
        src.write_chunk("f.bin", data[off:off + REMOTE_CHUNK_SIZE], off, total)
    # read_chunk 범위 읽기로 재조립
    out = b""
    off = 0
    while True:
        part = src.read_chunk("f.bin", off, REMOTE_CHUNK_SIZE)
        out += part
        off += len(part)
        if len(part) < REMOTE_CHUNK_SIZE:
            break
    assert out == data
    # 일반 read도 동일
    assert src.read("f.bin") == data


def test_loopback_write_chunk_insufficient_space(tmp_path):
    src = _loopback(tmp_path, size=10 * 1024 * 1024)  # 10 MiB 쿼터(최소)
    with pytest.raises(OSError, match="insufficient space"):
        src.write_chunk("big.bin", b"x" * 1024, 0, 20 * 1024 * 1024)


def test_directory_chunk_roundtrip(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    src = DirectorySource("vol", str(d))
    src.initialize()
    data = os.urandom(9 * 1024 * 1024)
    total = len(data)
    for off in range(0, total, REMOTE_CHUNK_SIZE):
        src.write_chunk("f.bin", data[off:off + REMOTE_CHUNK_SIZE], off, total)
    assert src.read("f.bin") == data
    assert src.read_chunk("f.bin", REMOTE_CHUNK_SIZE, 100) == \
        data[REMOTE_CHUNK_SIZE:REMOTE_CHUNK_SIZE + 100]


# ---------------------------------------------------------------------------
# p2p_server: _op_write_chunk / _op_read_chunk
# ---------------------------------------------------------------------------

def _server(sources):
    store = MagicMock()
    jbod = JBODManager(sources=sources, metadata_store=store)
    auth = MagicMock(spec=AuthClient)
    auth.user_id = "u1"
    return P2PServer(
        jbod_manager=jbod, auth_client=auth, port=0,
        server_url="http://localhost:8000",
    )


def test_op_write_chunk_selects_source_by_total_size(tmp_path):
    small = _loopback(tmp_path, "s.img", 10 * 1024 * 1024)
    big = _loopback(tmp_path, "b.img", 100 * 1024 * 1024)
    srv = _server([small, big])
    chunk = os.urandom(REMOTE_CHUNK_SIZE)
    body = {
        "physical_path": "f.bin",
        "data": base64.b64encode(chunk).decode("ascii"),
        "offset": 0,
        "total_size": 12 * 1024 * 1024,  # small(10MiB)엔 안 들어감 → big 선택
    }
    status, res = srv._op_write_chunk(body)
    assert status == 200
    assert res["source_id"] == "b.img"


def test_op_write_chunk_insufficient_returns_507(tmp_path):
    small = _loopback(tmp_path, "s.img", 10 * 1024 * 1024)
    srv = _server([small])
    body = {
        "physical_path": "f.bin",
        "data": base64.b64encode(b"x" * 1024).decode("ascii"),
        "offset": 0,
        "total_size": 20 * 1024 * 1024,
    }
    status, res = srv._op_write_chunk(body)
    assert status == 507


def test_op_write_read_chunk_roundtrip(tmp_path):
    src = _loopback(tmp_path)
    srv = _server([src])
    data = os.urandom(9 * 1024 * 1024 + 7)
    total = len(data)
    sid = None
    for off in range(0, total, REMOTE_CHUNK_SIZE):
        body = {
            "physical_path": "f.bin",
            "data": base64.b64encode(data[off:off + REMOTE_CHUNK_SIZE]).decode("ascii"),
            "offset": off,
            "total_size": total,
        }
        if sid:
            body["source_id"] = sid
        status, res = srv._op_write_chunk(body)
        assert status == 200
        sid = res["source_id"]
    # read_chunk 범위 읽기
    out = b""
    off = 0
    while True:
        status, res = srv._op_read_chunk(
            {"physical_path": "f.bin", "offset": off, "length": REMOTE_CHUNK_SIZE,
             "source_id": sid}
        )
        assert status == 200
        part = base64.b64decode(res["data"])
        out += part
        off += len(part)
        if len(part) < REMOTE_CHUNK_SIZE:
            break
    assert out == data


def test_op_read_chunk_missing_returns_404(tmp_path):
    src = _loopback(tmp_path)
    srv = _server([src])
    status, _res = srv._op_read_chunk(
        {"physical_path": "nope.bin", "offset": 0, "length": 100,
         "source_id": "vault.img"}
    )
    assert status == 404


# ---------------------------------------------------------------------------
# remote_source: 청크 push/read 라운드트립 + rollback (HTTP/인증 우회)
# ---------------------------------------------------------------------------

def _remote_to(server):
    """RemoteSource를 만들고 _p2p_request를 홀더 server.dispatch로 직결한다.

    dispatch는 인증 없이 op_map(write/read/write_chunk/read_chunk/delete 등)을 실행하므로
    HTTP·토큰을 우회해 청크 로직만 검증한다.
    """
    auth = MagicMock(spec=AuthClient)
    auth.user_id = "u1"
    rs = RemoteSource("rs", "devB", auth, "http://localhost:8000")
    rs._active = True
    rs._peer_address = "127.0.0.1:0"

    async def fake_request(endpoint, payload):
        op = rs._ENDPOINT_OP.get(endpoint)
        status, result = server.dispatch(op, payload)
        if status >= 400:
            raise OSError(f"HTTP {status}")
        return result

    rs._p2p_request = fake_request
    return rs


def test_remote_chunked_push_and_read_roundtrip(tmp_path):
    src = _loopback(tmp_path)
    srv = _server([src])
    rs = _remote_to(srv)
    data = os.urandom(9 * 1024 * 1024 + 321)  # > REMOTE_CHUNK_SIZE
    sid = rs.push_blob("f.bin", data)
    assert sid == "vault.img"
    # 홀더 디스크에 전량 저장됐는지(청크 합산)
    assert src.read("f.bin") == data
    # file_size로 청크 읽기 분기 → 동일 바이트
    out = rs.read_from_source("f.bin", sid, file_size=len(data))
    assert out == data


def test_remote_small_blob_uses_single_write(tmp_path):
    src = _loopback(tmp_path)
    srv = _server([src])
    rs = _remote_to(srv)
    calls = []
    orig = rs._p2p_request

    async def spy(endpoint, payload):
        calls.append(endpoint)
        return await orig(endpoint, payload)

    rs._p2p_request = spy
    data = os.urandom(1024)  # < REMOTE_CHUNK_SIZE
    rs.push_blob("s.bin", data)
    assert "/p2p/write" in calls
    assert "/p2p/write_chunk" not in calls


def test_remote_push_chunked_rollback_on_failure(tmp_path):
    src = _loopback(tmp_path)
    srv = _server([src])
    rs = _remote_to(srv)
    orig = rs._p2p_request
    state = {"n": 0}

    async def flaky(endpoint, payload):
        if endpoint == "/p2p/write_chunk":
            state["n"] += 1
            if state["n"] == 2:  # 두 번째 청크에서 실패
                raise OSError("transient")
        return await orig(endpoint, payload)

    rs._p2p_request = flaky
    data = os.urandom(9 * 1024 * 1024)  # 3 청크
    with pytest.raises(OSError):
        rs.push_blob("f.bin", data)
    # 부분 파일이 삭제(rollback)됐는지
    assert not src.exists("f.bin")
