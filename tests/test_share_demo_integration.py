#!/usr/bin/env python3
"""MVP5 파일 공유 데모 통합 테스트.

사용자 A(소유자)가 파일에 공유 토큰을 발급하고, 사용자 B(수신자)가 그 토큰으로
A의 P2P 서버에서 직접 파일을 읽는 전체 흐름을 검증한다.

실서버 의존을 없애기 위해, 중앙 서버의 공유 토큰 검증(/shares/{token}/verify)을
구현한 경량 mock 서버를 같은 프로세스에 띄우고, 실제 P2PServer와 httpx로 HTTP
왕복을 수행한다.

실행: source .venv/Scripts/activate && pytest tests/test_share_demo_integration.py -v
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import socket
import sys
import tempfile
import time

import httpx
import pytest
import pytest_asyncio
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.auth_client import AuthClient
from stardustlib.storage_pool import StoragePool
from stardustlib.metadata_store import MetadataStore
from stardustlib.p2p_server import P2PServer
from stardustlib.storage_source import DirectorySource

pytestmark = pytest.mark.asyncio

_OWNER_USER_ID = "user-A-owner"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeAuthClient(AuthClient):
    def __init__(self, server_url: str, token: str, user_id: str) -> None:
        super().__init__(server_url)
        self._access_token = token
        self._user_id = user_id

    async def get_valid_token(self) -> str:
        return self._access_token


class _MockShareServer:
    """공유 토큰 발급/검증을 흉내내는 경량 중앙 서버.

    - POST /shares: {device_id, physical_path, expires_in_seconds} → {share_token, ...}
    - POST /shares/{token}/verify: {physical_path} → {valid, device_id}
    """

    def __init__(self) -> None:
        self._port = _free_port()
        self._runner: web.AppRunner | None = None
        # token -> (physical_path, expires_at_epoch)
        self._shares: dict[str, tuple[str, float]] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/shares", self._create)
        app.router.add_post("/shares/{token}/verify", self._verify)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def issue_share(self, physical_path: str, expires_in: float) -> str:
        """테스트 헬퍼: 토큰을 직접 발급한다."""
        token = secrets.token_urlsafe(16)
        self._shares[token] = (physical_path, time.time() + expires_in)
        return token

    async def _create(self, request: web.Request) -> web.Response:
        body = await request.json()
        token = self.issue_share(
            body["physical_path"], body.get("expires_in_seconds", 3600)
        )
        return web.json_response({"share_token": token, "expires_at": 0})

    async def _verify(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        body = await request.json()
        entry = self._shares.get(token)
        if entry is None:
            return web.json_response({"valid": False, "device_id": None})
        bound_path, expires_at = entry
        if time.time() >= expires_at:
            # 만료: device_id None → P2P가 401로 처리
            return web.json_response({"valid": False, "device_id": None})
        if bound_path != body.get("physical_path"):
            # 경로 불일치: device_id 존재 → P2P가 403으로 처리
            return web.json_response({"valid": False, "device_id": "dev-A"})
        return web.json_response({"valid": True, "device_id": "dev-A"})


@pytest_asyncio.fixture
async def share_env():
    """A의 P2P 서버 + mock 중앙 서버 구성."""
    owner_dir = tempfile.mkdtemp()
    source = DirectorySource("owner-vol", owner_dir)
    source.initialize()
    store = MetadataStore(os.path.join(owner_dir, ".meta.db"), b"\x00" * 32)
    store.initialize()
    storage_pool = StoragePool([source], store, encryption_engine=None)

    central = _MockShareServer()
    await central.start()

    p2p_port = _free_port()
    server_auth = _FakeAuthClient(central.url, "tokenA", _OWNER_USER_ID)
    p2p = P2PServer(storage_pool, server_auth, p2p_port, central.url)
    await p2p.start()

    p2p_address = f"127.0.0.1:{p2p_port}"
    yield central, owner_dir, p2p_address

    store.close()
    await p2p.stop()
    await central.stop()


async def _p2p_read(p2p_address: str, physical_path: str,
                    share_token: str) -> httpx.Response:
    """수신자 B가 share_token으로 /p2p/read를 직접 호출한다."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(
            f"http://{p2p_address}/p2p/read",
            json={"physical_path": physical_path, "share_token": share_token},
        )


async def test_recipient_reads_shared_file(share_env):
    """A가 공유한 파일을 B가 share_token으로 읽어 동일 내용을 받는다."""
    central, owner_dir, p2p_address = share_env

    # A가 파일을 물리적으로 기록
    payload = b"shared content for user B \x00\x01"
    target = os.path.join(owner_dir, "shared.bin")
    with open(target, "wb") as f:
        f.write(payload)

    # A가 공유 토큰 발급
    token = central.issue_share("shared.bin", expires_in=3600)

    # B가 토큰으로 읽기
    resp = await _p2p_read(p2p_address, "shared.bin", token)
    assert resp.status_code == 200
    received = base64.b64decode(resp.json()["data"])
    assert received == payload


async def test_expired_share_rejected(share_env):
    """만료된 공유 토큰으로 읽으면 401로 거부된다."""
    central, owner_dir, p2p_address = share_env

    with open(os.path.join(owner_dir, "f.bin"), "wb") as f:
        f.write(b"data")

    # 이미 만료된 토큰
    token = central.issue_share("f.bin", expires_in=-1)

    resp = await _p2p_read(p2p_address, "f.bin", token)
    assert resp.status_code == 401


async def test_path_isolation_enforced(share_env):
    """토큰에 묶이지 않은 다른 경로를 읽으려 하면 403으로 거부된다."""
    central, owner_dir, p2p_address = share_env

    with open(os.path.join(owner_dir, "allowed.bin"), "wb") as f:
        f.write(b"allowed")
    with open(os.path.join(owner_dir, "secret.bin"), "wb") as f:
        f.write(b"secret - must not leak")

    # allowed.bin에만 묶인 토큰
    token = central.issue_share("allowed.bin", expires_in=3600)

    # secret.bin을 같은 토큰으로 읽으려 시도 → 403
    resp = await _p2p_read(p2p_address, "secret.bin", token)
    assert resp.status_code == 403


async def test_path_traversal_still_blocked_with_share(share_env):
    """share_token이 있어도 path traversal은 차단된다."""
    central, _owner_dir, p2p_address = share_env

    # 토큰을 traversal 경로에 묶어 발급해도 P2P의 _validate_path가 먼저 차단
    token = central.issue_share("../escape.bin", expires_in=3600)

    resp = await _p2p_read(p2p_address, "../escape.bin", token)
    assert resp.status_code == 400


async def test_missing_token_rejected(share_env):
    """존재하지 않는 share_token으로 읽으면 401로 거부된다."""
    _central, _owner_dir, p2p_address = share_env

    resp = await _p2p_read(p2p_address, "x.bin", "nonexistent-token")
    assert resp.status_code == 401
