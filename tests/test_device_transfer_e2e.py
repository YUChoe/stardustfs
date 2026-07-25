#!/usr/bin/env python3
"""같은 유저 디바이스 간 파일 전송 E2E 테스트 (중앙 서버 경유).

실제 중앙 서버(로컬 또는 원격)를 거쳐 디바이스 등록 → routing 조회 →
P2P read의 전체 경로를 검증한다. mock 중앙 서버를 쓰는 test_p2p_integration과
달리, 디바이스 등록과 라우팅 정보 조회를 실제 서버 API로 수행한다.

PC-A: P2PServer를 띄우고 디바이스로 등록한다 (P2P 접속 주소 등록).
PC-B: 같은 계정으로 RemoteSource를 만들어 routing으로 PC-A를 찾아 파일을 읽는다.

실행:
  STARDUST_TEST_SERVER_URL=http://127.0.0.1:8000 \
  source .venv/Scripts/activate && pytest tests/test_device_transfer_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from stardustlib.auth_client import AuthClient
from stardustlib.device_manager import DeviceManager
from stardustlib.storage_pool import StoragePool
from stardustlib.metadata_store import MetadataStore
from stardustlib.p2p_server import P2PServer
from stardustlib.remote_source import RemoteSource
from stardustlib.storage_source import DirectorySource

SERVER_URL = os.environ.get("STARDUST_TEST_SERVER_URL", "https://stardustfs.noizze.net")
EMAIL = os.environ.get("STARDUST_TEST_EMAIL", "e2e-test@example.com")
PASSWORD = os.environ.get("STARDUST_TEST_PASSWORD", "e2e-test-password-2026")

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _account():
    """테스트 전용 계정 등록 (이미 있으면 무시)."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(
                f"{SERVER_URL}/auth/register",
                json={"email": EMAIL, "password": PASSWORD},
            )
        except Exception:
            pass
    yield


@pytest_asyncio.fixture
async def device_pair(_account):
    """PC-A(P2P 서버 + 디바이스 등록)와 PC-B(요청자) 구성."""
    # --- PC-A: 로컬 스토리지 + P2P 서버 + 디바이스 등록 ---
    a_dir = tempfile.mkdtemp()
    a_source = DirectorySource("a-vol", a_dir)
    a_source.initialize()
    a_store = MetadataStore(os.path.join(a_dir, ".meta.db"), b"\x00" * 32)
    a_store.initialize()
    a_storage_pool = StoragePool([a_source], a_store, encryption_engine=None)

    a_auth = AuthClient(SERVER_URL)
    await a_auth.login(EMAIL, PASSWORD)

    p2p_port = _free_port()
    a_p2p = P2PServer(a_storage_pool, a_auth, p2p_port, SERVER_URL)
    await a_p2p.start()

    # 디바이스 등록 (P2P 접속 주소를 127.0.0.1:p2p_port로 설정)
    a_devmgr = DeviceManager(a_auth, SERVER_URL, "PC-A", p2p_port)
    a_devmgr.set_connection_address(f"127.0.0.1:{p2p_port}")
    device_id = await a_devmgr.register()

    # --- PC-B: 같은 계정으로 RemoteSource ---
    b_auth = AuthClient(SERVER_URL)
    await b_auth.login(EMAIL, PASSWORD)
    b_remote = RemoteSource("b-remote", device_id, b_auth, SERVER_URL)

    yield a_dir, a_storage_pool, device_id, b_remote

    await a_devmgr.stop()
    await a_p2p.stop()
    await a_auth.close()
    await b_auth.close()
    a_store.close()


async def test_device_registration_and_routing(device_pair):
    """PC-A 등록 후 PC-B가 routing으로 PC-A의 P2P 주소를 찾는다."""
    _a_dir, _a_storage_pool, device_id, b_remote = device_pair
    assert device_id

    # RemoteSource.initialize가 routing으로 접속 주소를 확보
    await asyncio.to_thread(b_remote.initialize)
    assert b_remote.is_active, "routing 조회 실패 — PC-A 주소를 못 찾음"
    assert b_remote.peer_address is not None


async def test_cross_device_read_via_central_server(device_pair):
    """PC-A의 파일을 PC-B가 중앙 서버 routing + P2P로 읽는다 (전체 경로)."""
    a_dir, _a_storage_pool, _device_id, b_remote = device_pair

    # PC-A 디스크에 파일 기록
    payload = b"file from PC-A read by PC-B \x00\xff"
    with open(os.path.join(a_dir, "shared.bin"), "wb") as f:
        f.write(payload)

    # PC-B: routing 조회 후 P2P read
    await asyncio.to_thread(b_remote.initialize)
    data = await asyncio.to_thread(b_remote.read, "shared.bin")
    assert data == payload


async def test_cross_device_list_and_exists(device_pair):
    """PC-B가 PC-A 디렉토리를 list하고 파일 존재를 확인한다."""
    a_dir, _a_storage_pool, _device_id, b_remote = device_pair

    os.makedirs(os.path.join(a_dir, "docs"), exist_ok=True)
    with open(os.path.join(a_dir, "docs", "f1.txt"), "wb") as f:
        f.write(b"1")

    await asyncio.to_thread(b_remote.initialize)
    assert await asyncio.to_thread(b_remote.exists, "docs/f1.txt") is True
    entries = await asyncio.to_thread(b_remote.list_dir, "docs")
    assert "f1.txt" in entries
