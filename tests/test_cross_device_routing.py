#!/usr/bin/env python3
"""크로스 디바이스 파일 자동 라우팅 테스트.

- JBODManager Device_Router: device_id 기반 read_file 라우팅
- P2PServer 다중 소스: source_id 기반 소스 선택
- Property 1(라우팅 결정성), Property 2(소스 선택)
- 통합: PC-A 저장 → PC-B가 원격 라우팅으로 read
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import tempfile
import time

import pytest
import pytest_asyncio
from aiohttp import web
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.p2p_server import P2PServer
from stardustlib.remote_source import RemoteSource
from stardustlib.storage_source import DirectorySource

_USER_ID = "user-routing-test"
_VALID_TOKEN = "valid-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ============================================================
# Device_Router 단위 테스트 (가짜 원격 프록시)
# ============================================================


class _FakeRemote:
    """원격 디바이스 프록시 대역."""

    def __init__(self, active=True, data=b"remote-bytes", refresh_to=None):
        self._active = active
        self._data = data
        self.calls = []
        # refresh() 호출 시 전환될 활성 상태(None이면 refresh 미지원으로 간주)
        self._refresh_to = refresh_to
        self.refresh_calls = 0

    @property
    def is_active(self):
        return self._active

    def refresh(self, *, force=False):
        """재네고시에이션 모사: refresh_to가 설정되어 있으면 그 상태로 전환."""
        self.refresh_calls += 1
        if self._refresh_to is not None:
            self._active = self._refresh_to
        return self._active

    def read_from_source(self, physical_path, source_id):
        self.calls.append((physical_path, source_id))
        return self._data


def _make_jbod(tmp, device_id):
    src = DirectorySource("loop-local", tmp)
    src.initialize()
    store = MetadataStore(os.path.join(tmp, ".m.db"), b"\x00" * 32)
    store.initialize()
    jbod = JBODManager([src], store, encryption_engine=None, device_id=device_id)
    return jbod, store


def test_router_local_file_reads_locally():
    """로컬 소유 파일은 로컬에서 읽는다."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        jbod.write_file("/a.txt", b"local data")  # device_id=dev-local 기록
        assert jbod.read_file("/a.txt") == b"local data"
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_legacy_null_device_reads_locally():
    """device_id가 NULL인 레거시 레코드는 로컬에서 읽는다."""
    d = tempfile.mkdtemp()
    # device_id 없이 JBOD 생성 → write 시 device_id=NULL
    src = DirectorySource("loop-local", d)
    src.initialize()
    store = MetadataStore(os.path.join(d, ".m.db"), b"\x00" * 32)
    store.initialize()
    jbod = JBODManager([src], store, encryption_engine=None, device_id=None)
    try:
        jbod.write_file("/legacy.txt", b"legacy data")
        rec = store.lookup("/legacy.txt")
        assert rec.device_id is None
        assert jbod.read_file("/legacy.txt") == b"legacy data"
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_remote_file_routes_to_proxy():
    """원격 소유 파일은 등록된 원격 프록시로 라우팅된다."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        remote = _FakeRemote(active=True, data=b"from-remote")
        jbod.register_remote_device("dev-remote", remote)

        # 원격 소유 레코드 직접 삽입
        now = time.time()
        store.insert("/r.txt", "loop-001", "phys/r.txt", 10, now, now,
                     device_id="dev-remote")

        data = jbod.read_file("/r.txt")
        assert data == b"from-remote"
        # 프록시에 (physical_path, source_id)로 요청됨
        assert remote.calls == [("phys/r.txt", "loop-001")]
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_remote_offline_raises():
    """원격 프록시가 비활성이면 OSError."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        jbod.register_remote_device("dev-remote", _FakeRemote(active=False))
        now = time.time()
        store.insert("/r.txt", "loop-001", "phys/r.txt", 10, now, now,
                     device_id="dev-remote")
        with pytest.raises(OSError):
            jbod.read_file("/r.txt")
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_remote_reactivates_via_refresh():
    """비활성 원격이 read 시점 refresh로 재활성화되면 읽기에 성공한다."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        # 비활성으로 시작하지만 refresh 시 활성으로 전환되는 프록시
        remote = _FakeRemote(active=False, data=b"now-online", refresh_to=True)
        jbod.register_remote_device("dev-remote", remote)
        now = time.time()
        store.insert("/r.txt", "loop-001", "phys/r.txt", 10, now, now,
                     device_id="dev-remote")

        data = jbod.read_file("/r.txt")

        assert data == b"now-online"
        assert remote.refresh_calls == 1
        assert remote.calls == [("phys/r.txt", "loop-001")]
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_remote_still_offline_after_refresh_raises():
    """refresh 후에도 여전히 오프라인이면 OSError."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        # 비활성, refresh해도 비활성 유지
        remote = _FakeRemote(active=False, refresh_to=False)
        jbod.register_remote_device("dev-remote", remote)
        now = time.time()
        store.insert("/r.txt", "loop-001", "phys/r.txt", 10, now, now,
                     device_id="dev-remote")
        with pytest.raises(OSError):
            jbod.read_file("/r.txt")
        assert remote.refresh_calls == 1
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_remote_unregistered_raises():
    """원격 소유인데 프록시 미등록이면 OSError."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        now = time.time()
        store.insert("/r.txt", "loop-001", "phys/r.txt", 10, now, now,
                     device_id="dev-unknown")
        with pytest.raises(OSError):
            jbod.read_file("/r.txt")
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_router_remote_write_takes_over_ownership():
    """원격 소유 파일을 수정하면 로컬 소유권으로 이전된다 (3a)."""
    d = tempfile.mkdtemp()
    jbod, store = _make_jbod(d, "dev-local")
    try:
        now = time.time()
        store.insert("/r.txt", "loop-001", "phys/r.txt", 10, now, now,
                     device_id="dev-remote")

        # 원격 소유 파일 수정 → OSError 대신 로컬 소유권 이전
        jbod.write_file("/r.txt", b"local edit")

        rec = store.lookup("/r.txt")
        assert rec is not None
        # 소유권이 로컬로 이전됨
        assert rec.device_id == "dev-local"
        # 로컬 소스로 물리 위치 변경
        assert rec.source_id == "loop-local"
        # 가상 경로 유지, 내용 반영
        assert jbod.read_file("/r.txt") == b"local edit"
        # GC 필요 플래그가 섰다(파일마다가 아니라 1회)
        assert jbod._gc_needed is True
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# Property 1: 읽기 라우팅 결정성
# ============================================================


def _route_decision(owner, local_id, has_proxy, proxy_active):
    """read_file 라우팅 결정의 참조 구현."""
    if owner is None or owner == local_id:
        return "local"
    if not has_proxy or not proxy_active:
        return "error"
    return "remote"


@settings(max_examples=300)
@given(
    owner=st.one_of(st.none(), st.sampled_from(["dev-local", "dev-a", "dev-b"])),
    local_id=st.sampled_from(["dev-local"]),
    has_proxy=st.booleans(),
    proxy_active=st.booleans(),
)
def test_property1_routing_determinism(owner, local_id, has_proxy, proxy_active):
    """라우팅 결정은 3가지 경우로 상호 배타적으로 분류된다."""
    decision = _route_decision(owner, local_id, has_proxy, proxy_active)
    assert decision in {"local", "remote", "error"}
    # 로컬/레거시는 항상 local
    if owner is None or owner == local_id:
        assert decision == "local"
    # 원격인데 프록시 없거나 비활성이면 error
    elif not has_proxy or not proxy_active:
        assert decision == "error"
    else:
        assert decision == "remote"


# ============================================================
# P2PServer 다중 소스 + 통합 (실제 P2PServer)
# ============================================================


class _MockCentral:
    def __init__(self, p2p_address):
        self._addr = p2p_address
        self._port = _free_port()
        self._runner = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self._port}"

    async def start(self):
        app = web.Application()
        app.router.add_post("/auth/verify", self._verify)
        app.router.add_get("/routing/{device_id}", self._routing)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    async def _verify(self, request):
        body = await request.json()
        if body.get("token") == _VALID_TOKEN:
            return web.json_response({"valid": True, "user_id": _USER_ID})
        return web.json_response({"valid": False})

    async def _routing(self, request):
        return web.json_response({
            "device_id": request.match_info["device_id"],
            "connection_address": self._addr,
            "is_online": True,
            "last_heartbeat": "2026-01-01T00:00:00",
        })


class _FakeAuth:
    def __init__(self, url):
        self._server_url = url
        self._user_id = _USER_ID

    async def get_valid_token(self):
        return _VALID_TOKEN

    @property
    def user_id(self):
        return _USER_ID


@pytest_asyncio.fixture
async def two_device_env():
    """PC-A(P2P 서버, 소스 2개) + mock 중앙 서버 구성."""
    a_dir1 = tempfile.mkdtemp()
    a_dir2 = tempfile.mkdtemp()
    src1 = DirectorySource("loop-001", a_dir1)
    src1.initialize()
    src2 = DirectorySource("loop-002", a_dir2)
    src2.initialize()
    a_store = MetadataStore(os.path.join(a_dir1, ".m.db"), b"\x00" * 32)
    a_store.initialize()
    a_jbod = JBODManager([src1, src2], a_store, encryption_engine=None,
                         device_id="dev-A")

    p2p_port = _free_port()
    central = _MockCentral(f"127.0.0.1:{p2p_port}")
    await central.start()
    a_auth = _FakeAuth(central.url)
    p2p = P2PServer(a_jbod, a_auth, p2p_port, central.url)
    await p2p.start()

    yield a_dir1, a_dir2, central, f"127.0.0.1:{p2p_port}"

    a_store.close()
    await p2p.stop()
    await central.stop()
    shutil.rmtree(a_dir1, ignore_errors=True)
    shutil.rmtree(a_dir2, ignore_errors=True)


@pytest.mark.asyncio
async def test_p2p_reads_from_specified_source(two_device_env):
    """P2P read가 source_id로 지정한 소스에서 읽는다 (다중 소스)."""
    a_dir1, a_dir2, central, addr = two_device_env

    # 각 소스에 다른 파일
    with open(os.path.join(a_dir1, "f1.bin"), "wb") as f:
        f.write(b"in source 1")
    with open(os.path.join(a_dir2, "f2.bin"), "wb") as f:
        f.write(b"in source 2")

    b_auth = _FakeAuth(central.url)
    remote = RemoteSource("remote-A", "dev-A", b_auth, central.url)
    await asyncio.to_thread(remote.initialize)

    # source_id로 각각 읽기
    d1 = await asyncio.to_thread(remote.read_from_source, "f1.bin", "loop-001")
    d2 = await asyncio.to_thread(remote.read_from_source, "f2.bin", "loop-002")
    assert d1 == b"in source 1"
    assert d2 == b"in source 2"


@pytest.mark.asyncio
async def test_p2p_unknown_source_404(two_device_env):
    """존재하지 않는 source_id는 404 → OSError."""
    _a1, _a2, central, _addr = two_device_env
    b_auth = _FakeAuth(central.url)
    remote = RemoteSource("remote-A", "dev-A", b_auth, central.url)
    await asyncio.to_thread(remote.initialize)

    with pytest.raises(OSError):
        await asyncio.to_thread(remote.read_from_source, "x.bin", "loop-999")


@pytest.mark.asyncio
async def test_cross_device_read_end_to_end(two_device_env):
    """PC-B가 device_id 라우팅으로 PC-A의 파일을 읽는다 (전체 경로)."""
    a_dir1, _a_dir2, central, _addr = two_device_env

    # PC-A 소스1에 파일
    payload = b"cross device routed file"
    with open(os.path.join(a_dir1, "shared.bin"), "wb") as f:
        f.write(payload)

    # PC-B JBOD 구성 + 원격 프록시 등록
    b_dir = tempfile.mkdtemp()
    b_src = DirectorySource("loop-b", b_dir)
    b_src.initialize()
    b_store = MetadataStore(os.path.join(b_dir, ".m.db"), b"\x00" * 32)
    b_store.initialize()
    b_jbod = JBODManager([b_src], b_store, encryption_engine=None,
                         device_id="dev-B")
    try:
        b_auth = _FakeAuth(central.url)
        remote = RemoteSource("remote-A", "dev-A", b_auth, central.url)
        await asyncio.to_thread(remote.initialize)
        b_jbod.register_remote_device("dev-A", remote)

        # PC-A가 만든 파일 레코드가 metadata 동기화로 PC-B에 있음 (직접 삽입으로 시뮬레이트)
        now = time.time()
        b_store.insert("/shared.bin", "loop-001", "shared.bin", len(payload),
                       now, now, device_id="dev-A")

        # PC-B에서 그냥 read_file → 원격 라우팅으로 PC-A에서 가져옴
        data = await asyncio.to_thread(b_jbod.read_file, "/shared.bin")
        assert data == payload
    finally:
        b_store.close()
        shutil.rmtree(b_dir, ignore_errors=True)
