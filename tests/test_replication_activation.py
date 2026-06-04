"""리플리케이션 운영 활성화 Phase A1: parity 자동 활성 + 호스팅 신고."""
from __future__ import annotations

import socket

import pytest
import pytest_asyncio
from aiohttp import web

from stardustfs import _RECIPROCITY_FRACTION, _build_parity_store
from stardustlib.auth_client import AuthClient
from stardustlib.replication_hosting import fetch_policy, report_hosting


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- _build_parity_store ---

def test_parity_max_is_half_of_provided(tmp_path):
    config = {
        "metadata_db": str(tmp_path / "m.db"),
        "replication": {"enabled": True, "provided_bytes": 1000},
    }
    ps = _build_parity_store(config)
    assert ps is not None
    assert ps._max_bytes == int(1000 * _RECIPROCITY_FRACTION)


def test_parity_disabled_returns_none(tmp_path):
    config = {"metadata_db": str(tmp_path / "m.db"),
              "replication": {"enabled": False}}
    assert _build_parity_store(config) is None


def test_parity_enabled_by_default_with_zero_host_capacity(tmp_path):
    # replication 섹션이 없어도 기본 활성, provided 미설정 → max 0(타인 보관 안 함)
    ps = _build_parity_store({"metadata_db": str(tmp_path / "m.db")})
    assert ps is not None and ps._max_bytes == 0


def test_parity_uses_policy_fraction(tmp_path):
    config = {
        "metadata_db": str(tmp_path / "m.db"),
        "replication": {"enabled": True, "provided_bytes": 1000},
    }
    # 정책 비율 0.25를 주입 → max=250
    ps = _build_parity_store(config, 0.25)
    assert ps is not None and ps._max_bytes == 250


def test_parity_legacy_p2p_flag(tmp_path):
    config = {
        "metadata_db": str(tmp_path / "m.db"),
        "p2p": {"parity_enabled": True, "parity_max_bytes": 123},
    }
    ps = _build_parity_store(config)
    assert ps is not None and ps._max_bytes == 123


# --- report_hosting ---

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
        return web.json_response(
            {"reciprocity_fraction": 0.5, "min_replicas": 3}, status=self._status
        )


@pytest_asyncio.fixture
async def hosting_server():
    srv = _HostingServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_report_hosting_success(hosting_server):
    auth = _FakeAuth(hosting_server.url)
    ok = await report_hosting(auth, hosting_server.url, "dev-1", 5000)
    await auth.close()
    assert ok is True
    assert hosting_server.received == {"device_id": "dev-1", "provided_bytes": 5000}


@pytest.mark.asyncio
async def test_report_hosting_404_graceful():
    srv = _HostingServer(status=404)
    await srv.start()
    auth = _FakeAuth(srv.url)
    try:
        ok = await report_hosting(auth, srv.url, "dev-1", 5000)
        assert ok is False
    finally:
        await auth.close()
        await srv.stop()


@pytest.mark.asyncio
async def test_fetch_policy_success(hosting_server):
    auth = _FakeAuth(hosting_server.url)
    policy = await fetch_policy(auth, hosting_server.url)
    await auth.close()
    assert policy == {"reciprocity_fraction": 0.5, "min_replicas": 3}


@pytest.mark.asyncio
async def test_fetch_policy_unreachable_returns_none():
    url = f"http://127.0.0.1:{_free_port()}"
    auth = _FakeAuth(url)
    try:
        assert await fetch_policy(auth, url, timeout=1.0) is None
    finally:
        await auth.close()


@pytest.mark.asyncio
async def test_report_hosting_unreachable_graceful():
    # 사용 가능한 포트지만 리스너 없음 → 연결 실패 → False
    url = f"http://127.0.0.1:{_free_port()}"
    auth = _FakeAuth(url)
    try:
        ok = await report_hosting(auth, url, "dev-1", 5000, timeout=1.0)
        assert ok is False
    finally:
        await auth.close()
