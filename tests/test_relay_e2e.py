#!/usr/bin/env python3
"""P2P 릴레이 fallback E2E 테스트 (중앙 서버 경유).

직접 P2P 연결이 불가능한 상황(도달 불가 peer_address)을 강제하고, 중앙 서버
릴레이를 통해 PC-B가 PC-A의 파일을 읽는 전체 경로를 검증한다.

PC-A: 로컬 스토리지 + P2PServer + RelayWorker(폴링) + 디바이스 등록.
      단, 디바이스 connection_address를 도달 불가 주소로 등록해 직접 연결을 차단.
PC-B: 같은 계정 RemoteSource. 직접 연결 timeout → 릴레이 fallback으로 read.

로컬 서버 필요. STARDUST_TEST_SERVER_URL 미설정 시 기본 원격 서버를 쓰므로,
릴레이 워커가 동작하는 서버(이 브랜치 배포본)가 필요하다.

실행:
  STARDUST_TEST_SERVER_URL=http://127.0.0.1:8000 \
  source .venv/Scripts/activate && pytest tests/test_relay_e2e.py -v
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
from stardustlib.relay_worker import RelayWorker
from stardustlib.remote_source import RemoteSource
from stardustlib.storage_source import DirectorySource

SERVER_URL = os.environ.get("STARDUST_TEST_SERVER_URL", "https://stardustfs.noizze.net")
EMAIL = os.environ.get("STARDUST_TEST_EMAIL", "e2e-test@example.com")
PASSWORD = os.environ.get("STARDUST_TEST_PASSWORD", "e2e-test-password-2026")

# 도달 불가 주소(RFC 5737 TEST-NET-1) — 직접 연결을 강제 실패시켜 릴레이를 유도
UNREACHABLE = "192.0.2.1:9"

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _account():
    import httpx
    # 릴레이 엔드포인트 미배포 서버(예: 운영 원격)에서는 이 테스트를 건너뛴다.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(
                f"{SERVER_URL}/auth/register",
                json={"email": EMAIL, "password": PASSWORD},
            )
        except Exception:
            pass
        # 릴레이 라우터 존재 여부 확인 (인증 없이 호출 시 401/403/422면 존재,
        # 404면 미배포)
        try:
            probe = await client.get(f"{SERVER_URL}/relay/poll")
            if probe.status_code == 404:
                pytest.skip("서버에 릴레이 엔드포인트가 없습니다(미배포)")
        except Exception:
            pytest.skip("서버에 연결할 수 없습니다")
    yield


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def relay_pair(_account):
    """PC-A(P2PServer + RelayWorker + 도달 불가 주소 등록), PC-B(요청자).

    워커를 한 번만 띄우는 운영 패턴을 따르기 위해 module-scope로 구성한다.
    """
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

    # 디바이스 등록: connection_address를 도달 불가 주소로 → 직접 연결 차단
    a_devmgr = DeviceManager(a_auth, SERVER_URL, "PC-A-relay", p2p_port)
    a_devmgr.set_connection_address(UNREACHABLE)
    device_id = await a_devmgr.register()

    # PC-A 릴레이 워커 시작 (서버 폴링)
    a_worker = RelayWorker(a_p2p, a_auth, SERVER_URL, device_id)
    await a_worker.start()

    # PC-B: 같은 계정 RemoteSource (짧은 timeout으로 직접 연결 빠르게 실패)
    b_auth = AuthClient(SERVER_URL)
    await b_auth.login(EMAIL, PASSWORD)
    b_remote = RemoteSource("b-remote", device_id, b_auth, SERVER_URL, timeout=3.0)

    yield a_dir, a_storage_pool, device_id, b_remote

    await a_worker.stop()
    await a_devmgr.stop()
    await a_p2p.stop()
    await a_auth.close()
    await b_auth.close()
    a_store.close()


async def test_relay_read_when_direct_unreachable(relay_pair):
    """직접 연결 불가 시 PC-B가 릴레이로 PC-A 파일을 읽는다."""
    a_dir, _a_storage_pool, _device_id, b_remote = relay_pair

    payload = b"relayed file content \x00\xfe\xff"
    with open(os.path.join(a_dir, "relay.bin"), "wb") as f:
        f.write(payload)

    # routing 조회(도달 불가 주소를 받지만 is_active=True로 마운트됨)
    await asyncio.to_thread(b_remote.initialize)

    # read: 직접 연결 timeout → 릴레이 fallback → 동일 바이트 수신
    data = await asyncio.to_thread(b_remote.read, "relay.bin")
    assert data == payload


async def test_relay_exists_when_direct_unreachable(relay_pair):
    """릴레이로 exists 조회."""
    a_dir, _a_storage_pool, _device_id, b_remote = relay_pair

    with open(os.path.join(a_dir, "present.bin"), "wb") as f:
        f.write(b"x")

    await asyncio.to_thread(b_remote.initialize)
    assert await asyncio.to_thread(b_remote.exists, "present.bin") is True


async def test_relay_read_missing_file_errors(relay_pair):
    """릴레이 경유라도 없는 파일은 규격 오류(OSError)로 전달된다."""
    _a_dir, _a_storage_pool, _device_id, b_remote = relay_pair

    await asyncio.to_thread(b_remote.initialize)
    with pytest.raises(OSError):
        await asyncio.to_thread(b_remote.read, "no-such-file.bin")
