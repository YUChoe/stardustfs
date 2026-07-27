"""리플리케이션 운영 활성화: parity 자동 활성 + 호스팅 사용량 보고."""
from __future__ import annotations

import socket

import pytest
import pytest_asyncio
from aiohttp import web

from stardustfs import _build_parity_store
from stardustlib.auth_client import AuthClient
from stardustlib.metadata_store import MetadataStore
from stardustlib.replication_hosting import fetch_policy, report_usage
from stardustlib.storage_pool import StoragePool
from stardustlib.storage_source import DirectorySource


def _pool(tmp_path):
    """보관 청크를 놓을 소스 1개짜리 StoragePool + MetadataStore."""
    store = MetadataStore(str(tmp_path / "m.db"), b"k" * 32)
    store.initialize()
    src_dir = tmp_path / "src-1"
    src_dir.mkdir()
    source = DirectorySource("src-1", str(src_dir))
    source.initialize()
    return StoragePool([source], store, None, device_id="dev-self"), store


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- _build_parity_store ---

def test_parity_max_is_server_quota(tmp_path):
    """보관 한도는 서버가 정한 호스팅 상한이다(제공 용량 비율 폐기)."""
    config = {
        "metadata_db": str(tmp_path / "m.db"),
        "replication": {"enabled": True},
    }
    pool, store = _pool(tmp_path)
    ps = _build_parity_store(config, pool, store, 4096)
    assert ps is not None
    assert ps._max_bytes == 4096


def test_parity_disabled_returns_none(tmp_path):
    config = {"metadata_db": str(tmp_path / "m.db"),
              "replication": {"enabled": False}}
    pool, store = _pool(tmp_path)
    assert _build_parity_store(config, pool, store) is None


def test_parity_without_quota_hosts_nothing(tmp_path):
    """정책을 받지 못하면(quota=None) 타 사용자 청크를 받지 않는다(한도 0)."""
    pool, store = _pool(tmp_path)
    ps = _build_parity_store({"metadata_db": str(tmp_path / "m.db")}, pool, store)
    assert ps is not None and ps._max_bytes == 0


def test_parity_zero_quota_hosts_nothing(tmp_path):
    """할당량 0(호스팅 금지)도 한도 0이다."""
    pool, store = _pool(tmp_path)
    ps = _build_parity_store(
        {"metadata_db": str(tmp_path / "m.db"), "replication": {"enabled": True}},
        pool, store, 0,
    )
    assert ps is not None and ps._max_bytes == 0


def test_parity_ignores_legacy_config_keys(tmp_path):
    """레거시 provided_bytes·p2p.parity_max_bytes는 더 이상 한도를 정하지 않는다."""
    config = {
        "metadata_db": str(tmp_path / "m.db"),
        "replication": {"enabled": True, "provided_bytes": 1000},
        "p2p": {"parity_max_bytes": 123},
    }
    pool, store = _pool(tmp_path)
    ps = _build_parity_store(config, pool, store)
    assert ps is not None and ps._max_bytes == 0


# --- report_usage ---

class _FakeAuth(AuthClient):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self._access_token = "tok"

    async def get_valid_token(self) -> str:
        return "tok"


class _HostingServer:
    def __init__(self, status: int = 200) -> None:
        self._port = _free_port()
        self._status = status
        self._runner: web.AppRunner | None = None
        self.received: dict | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/replication/hosting", self._handle)
        app.router.add_get("/replication/policy", self._policy)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        self.received = await request.json()
        return web.json_response({"status": "ok"}, status=self._status)

    async def _policy(self, request: web.Request) -> web.Response:
        body = {"reciprocity_fraction": 0.5, "min_replicas": 3}
        # 프로비저닝 스위치를 반환하는 서버를 모사할 때만 채운다(미설정=구버전 서버).
        body.update(getattr(self, "policy_extra", {}) or {})
        return web.json_response(body, status=self._status)


@pytest_asyncio.fixture
async def hosting_server():
    srv = _HostingServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_report_usage_success(hosting_server):
    """제공 용량 신고가 아니라 실제 사용량(hosted/총 용량)을 보고한다."""
    auth = _FakeAuth(hosting_server.url)
    ok = await report_usage(auth, hosting_server.url, "dev-1", 5000, 10_000)
    await auth.close()
    assert ok is True
    assert hosting_server.received == {
        "device_id": "dev-1", "hosted_bytes": 5000, "total_bytes": 10_000,
    }


@pytest.mark.asyncio
async def test_report_usage_404_graceful():
    srv = _HostingServer(status=404)
    await srv.start()
    auth = _FakeAuth(srv.url)
    try:
        ok = await report_usage(auth, srv.url, "dev-1", 5000, 10_000)
        assert ok is False
    finally:
        await auth.close()
        await srv.stop()


@pytest.mark.asyncio
async def test_fetch_policy_success(hosting_server):
    """스위치를 반환하지 않는 구버전 서버는 기본 허용(True)으로 채운다."""
    auth = _FakeAuth(hosting_server.url)
    policy = await fetch_policy(auth, hosting_server.url)
    await auth.close()
    # 폐기 필드(reciprocity_fraction·min_replicas)는 읽지 않는다.
    assert policy == {
        "p2p_enabled": True, "hosting_enabled": True,
        # 새 필드를 안 주는 서버 → 목표 카피 기본 3, 할당량은 None(미수신)
        "target_copies": 3, "hosting_quota_bytes": None,
    }


@pytest.mark.asyncio
async def test_fetch_policy_reads_provisioning_switches(hosting_server):
    """서버가 스위치를 내려주면 그 값을 그대로 반영한다."""
    hosting_server.policy_extra = {
        "p2p_enabled": True, "hosting_enabled": False,
    }
    auth = _FakeAuth(hosting_server.url)
    policy = await fetch_policy(auth, hosting_server.url)
    await auth.close()
    assert policy["p2p_enabled"] is True
    assert policy["hosting_enabled"] is False


@pytest.mark.asyncio
async def test_fetch_policy_unreachable_returns_none():
    url = f"http://127.0.0.1:{_free_port()}"
    auth = _FakeAuth(url)
    try:
        assert await fetch_policy(auth, url, timeout=1.0) is None
    finally:
        await auth.close()


@pytest.mark.asyncio
async def test_report_usage_unreachable_graceful():
    # 사용 가능한 포트지만 리스너 없음 → 연결 실패 → False
    url = f"http://127.0.0.1:{_free_port()}"
    auth = _FakeAuth(url)
    try:
        ok = await report_usage(auth, url, "dev-1", 5000, 10_000, timeout=1.0)
        assert ok is False
    finally:
        await auth.close()


# --- 호스팅 상한 프로비저닝 (스펙 chunk-copy-policy Phase 1) ---

@pytest.mark.asyncio
async def test_fetch_policy_reads_quota_and_target_copies(hosting_server):
    """서버가 내려준 목표 카피 수와 호스팅 상한을 읽는다."""
    hosting_server.policy_extra = {
        "target_copies": 3, "hosting_quota_bytes": 8192,
    }
    auth = _FakeAuth(hosting_server.url)
    policy = await fetch_policy(auth, hosting_server.url, device_id="dev-1")
    await auth.close()
    assert policy["target_copies"] == 3
    assert policy["hosting_quota_bytes"] == 8192


@pytest.mark.asyncio
async def test_fetch_policy_zero_quota_is_not_none(hosting_server):
    """할당량 0(호스팅 금지)과 미수신(None)을 구분한다."""
    hosting_server.policy_extra = {"hosting_quota_bytes": 0}
    auth = _FakeAuth(hosting_server.url)
    policy = await fetch_policy(auth, hosting_server.url, device_id="dev-1")
    await auth.close()
    assert policy["hosting_quota_bytes"] == 0


def test_build_parity_store_migrates_legacy_dir(tmp_path):
    """기동 시 구 `.parity/` 청크를 소스로 이관한다(쿼터와 무관하게 지킨다)."""
    import json

    db_path = str(tmp_path / "m.db")
    legacy = tmp_path / "m.db.parity"
    legacy.mkdir()
    (legacy / "aa.bin").write_bytes(b"legacy-chunk")
    (legacy / "index.json").write_text(
        json.dumps({"aa": {"owner": "owner-a", "size": 12}}), encoding="utf-8"
    )

    pool, store = _pool(tmp_path)
    # 상한 0(호스팅 금지)이어도 이미 맡은 청크는 옮겨 보관한다
    ps = _build_parity_store({"metadata_db": db_path}, pool, store, 0)

    assert ps is not None
    assert ps.fetch("aa", "owner-a") == b"legacy-chunk"
    assert ps._max_bytes == 0  # 이관 후 상한은 정책값으로 복원된다
