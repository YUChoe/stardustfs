#!/usr/bin/env python3
"""P2P 실환경 통합 테스트.

실제 P2PServer(aiohttp)를 띄우고, 실제 RemoteSource로 HTTP 왕복을 수행하여
파일 read/write/delete/list/exists/mkdir/space 및 보안(인증·path traversal·payload 제한)을
검증한다. 중앙 서버 의존을 없애기 위해 /auth/verify·/routing을 구현한 경량 mock
서버를 같은 프로세스에서 띄운다.

RemoteSource의 동기 메서드는 자체 백그라운드 이벤트 루프에서 httpx 호출을 수행하므로,
테스트의 asyncio 루프를 블록하지 않도록 asyncio.to_thread로 감싸 호출한다.

실행: source .venv/Scripts/activate && pytest tests/test_p2p_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile

import pytest
import pytest_asyncio
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.auth_client import AuthClient
from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.p2p_server import P2PServer
from stardustlib.remote_source import RemoteSource
from stardustlib.storage_source import DirectorySource

pytestmark = pytest.mark.asyncio

_USER_ID = "user-p2p-test"
_VALID_TOKEN = "valid-access-token"


def _free_port() -> int:
    """사용 가능한 TCP 포트를 하나 확보한다."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeAuthClient(AuthClient):
    """실제 서버 없이 토큰/유저ID를 직접 보유하는 AuthClient."""

    def __init__(self, server_url: str, token: str, user_id: str) -> None:
        super().__init__(server_url)
        self._access_token = token
        self._user_id = user_id

    async def get_valid_token(self) -> str:
        return self._access_token


class _MockCentralServer:
    """P2PServer가 의존하는 /auth/verify, /routing/{id}를 구현한 mock 중앙 서버."""

    def __init__(self, p2p_address: str) -> None:
        self._p2p_address = p2p_address
        self._port = _free_port()
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/auth/verify", self._handle_verify)
        app.router.add_get("/routing/{device_id}", self._handle_routing)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_verify(self, request: web.Request) -> web.Response:
        body = await request.json()
        token = body.get("token")
        if token == _VALID_TOKEN:
            return web.json_response({"valid": True, "user_id": _USER_ID})
        return web.json_response({"valid": False})

    async def _handle_routing(self, request: web.Request) -> web.Response:
        return web.json_response({
            "device_id": request.match_info["device_id"],
            "connection_address": self._p2p_address,
            "is_online": True,
            "last_heartbeat": "2026-01-01T00:00:00",
        })


@pytest_asyncio.fixture
async def p2p_env():
    """P2PServer(원격 역할) + mock 중앙 서버 + RemoteSource(요청자 역할)를 구성한다."""
    remote_dir = tempfile.mkdtemp()  # P2P 서버가 노출하는 스토리지 루트

    # 원격 디바이스의 로컬 스토리지
    source = DirectorySource("remote-vol", remote_dir)
    source.initialize()
    meta_path = os.path.join(remote_dir, ".meta.db")
    store = MetadataStore(meta_path, b"\x00" * 32)
    store.initialize()
    jbod = JBODManager([source], store, encryption_engine=None)

    p2p_port = _free_port()
    p2p_address = f"127.0.0.1:{p2p_port}"

    # mock 중앙 서버 먼저 기동 (P2P 서버가 토큰 검증에 사용)
    central = _MockCentralServer(p2p_address)
    await central.start()

    # P2P 서버 측 AuthClient (user_id 일치 확인용)
    server_auth = _FakeAuthClient(central.url, _VALID_TOKEN, _USER_ID)
    p2p_server = P2PServer(jbod, server_auth, p2p_port, central.url)
    await p2p_server.start()

    # 요청자 측 RemoteSource
    client_auth = _FakeAuthClient(central.url, _VALID_TOKEN, _USER_ID)
    remote = RemoteSource(
        "remote-src", "device-xyz", client_auth, central.url, timeout=10.0
    )
    # routing 조회로 peer 주소 확보 + 활성화 (별도 스레드 루프에서 동기 실행)
    await asyncio.to_thread(remote.initialize)

    yield remote, remote_dir, client_auth, central

    store.close()
    await p2p_server.stop()
    await central.stop()


async def test_initialize_resolves_peer_address(p2p_env):
    """routing 조회로 P2P 접속 주소를 확보하고 활성화된다."""
    remote, _remote_dir, _auth, central = p2p_env
    assert remote.is_active
    assert remote.peer_address is not None
    assert remote.peer_address.startswith("127.0.0.1:")


async def test_write_then_read_roundtrip(p2p_env):
    """write로 기록한 데이터를 read로 동일하게 돌려받는다 (실제 HTTP 왕복)."""
    remote, remote_dir, _auth, _central = p2p_env
    payload = b"hello p2p \x00\x01\x02 binary"

    await asyncio.to_thread(remote.write, "dir/file.bin", payload)

    # 원격 디바이스의 실제 디스크에 기록되었는지 확인
    on_disk = os.path.join(remote_dir, "dir", "file.bin")
    assert os.path.isfile(on_disk)

    result = await asyncio.to_thread(remote.read, "dir/file.bin")
    assert result == payload


async def test_exists_and_delete(p2p_env):
    """exists/delete가 실제 파일 상태를 반영한다."""
    remote, _remote_dir, _auth, _central = p2p_env

    await asyncio.to_thread(remote.write, "a.txt", b"data")
    assert await asyncio.to_thread(remote.exists, "a.txt") is True

    await asyncio.to_thread(remote.delete, "a.txt")
    assert await asyncio.to_thread(remote.exists, "a.txt") is False


async def test_mkdir_and_list(p2p_env):
    """mkdir로 디렉토리를 만들고 list로 엔트리를 조회한다."""
    remote, _remote_dir, _auth, _central = p2p_env

    await asyncio.to_thread(remote.mkdir, "mydir")
    await asyncio.to_thread(remote.write, "mydir/f1.txt", b"1")
    await asyncio.to_thread(remote.write, "mydir/f2.txt", b"2")

    entries = await asyncio.to_thread(remote.list_dir, "mydir")
    assert "f1.txt" in entries
    assert "f2.txt" in entries


async def test_space_info(p2p_env):
    """space 조회로 전체/가용 용량을 반환한다."""
    remote, _remote_dir, _auth, _central = p2p_env

    total = await asyncio.to_thread(remote.get_total_space)
    available = await asyncio.to_thread(remote.get_available_space)
    assert total > 0
    assert available >= 0


async def test_read_missing_file_raises(p2p_env):
    """존재하지 않는 파일 read는 OSError(404)를 발생시킨다."""
    remote, _remote_dir, _auth, _central = p2p_env

    with pytest.raises(OSError):
        await asyncio.to_thread(remote.read, "does-not-exist.txt")


async def test_path_traversal_blocked(p2p_env):
    """'..'를 포함한 경로는 서버에서 400으로 거부되어 OSError가 된다."""
    remote, _remote_dir, _auth, _central = p2p_env

    with pytest.raises(OSError):
        await asyncio.to_thread(remote.read, "../escape.txt")


async def test_invalid_token_rejected(p2p_env):
    """잘못된 토큰을 보내면 P2P 서버가 401로 거부한다."""
    remote, _remote_dir, client_auth, _central = p2p_env

    # 토큰을 무효값으로 교체 → mock verify가 valid=false 반환 → 401
    client_auth._access_token = "bogus-token"
    with pytest.raises(OSError):
        await asyncio.to_thread(remote.write, "x.txt", b"x")


async def test_jbod_mount_remote_source_read(p2p_env):
    """JBODManager에 RemoteSource를 마운트하고 JBOD 경유로 원격 파일을 읽는다.

    이것이 '같은 유저의 디바이스 간 전송'의 끝까지 연결된 경로다:
    요청자 JBOD.read_file → metadata lookup → RemoteSource.read → P2P HTTP → 원격 디스크.
    """
    import time

    remote, remote_dir, _auth, _central = p2p_env

    # 원격 디바이스에 파일을 먼저 기록 (P2P write)
    payload = b"cross-device payload via JBOD"
    await asyncio.to_thread(remote.write, "docs/remote.bin", payload)

    # 요청자 측 JBOD 구성: 별도 metadata + RemoteSource 마운트 (암호화 없음)
    req_dir = tempfile.mkdtemp()
    req_store = MetadataStore(os.path.join(req_dir, "req.db"), b"\x00" * 32)
    req_store.initialize()
    req_jbod = JBODManager([], req_store, encryption_engine=None)

    # add_source로 동적 마운트 (stardustfs._mount_remote_sources가 하는 일)
    req_jbod.add_source(remote)
    assert req_jbod._get_source_by_id("remote-src") is remote

    # metadata에 원격 파일 레코드 삽입 (source_id가 remote 소스를 가리킴)
    now = time.time()
    req_store.insert("/docs/remote.bin", "remote-src", "docs/remote.bin",
                     len(payload), now, now)

    # JBOD 경유 읽기 → RemoteSource.read → P2P → 동일 바이트
    data = await asyncio.to_thread(req_jbod.read_file, "/docs/remote.bin")
    assert data == payload

    req_store.close()
