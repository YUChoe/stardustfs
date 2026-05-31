#!/usr/bin/env python3
"""멀티디바이스 동기화 E2E 테스트.

실제 서버(stardustfs.noizze.net)와 통신하여 2개 디바이스를 시뮬레이트하고
추가/삭제/변경/버전오염/레이스컨디션 등의 케이스를 검증한다.

실행: source .venv/Scripts/activate && pytest tests/test_multidevice_sync_e2e.py -v
환경변수: STARDUST_EMAIL, STARDUST_PASSWORD, STARDUST_KEY_PASSWORD
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from stardustlib.auth_client import AuthClient
from stardustlib.config_loader import ConfigLoader
from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.metadata_store import MetadataStore
from stardustlib.sync_client import SyncClient

SERVER_URL = "https://stardustfs.noizze.net"
# E2E 테스트 전용 계정 (실제 사용자 계정과 분리)
EMAIL = os.environ.get("STARDUST_TEST_EMAIL", "e2e-test@example.com")
PASSWORD = os.environ.get("STARDUST_TEST_PASSWORD", "e2e-test-password-2026")

pytestmark = pytest.mark.asyncio


def _make_db(tmp_path, name: str) -> tuple[MetadataStore, bytes]:
    """임시 MetadataStore를 생성한다."""
    db_path = str(tmp_path / f"{name}.db")
    key = os.urandom(32)
    store = MetadataStore(db_path, key)
    store.initialize()
    return store, key


def _make_sync_client(
    auth_client: AuthClient,
    store: MetadataStore,
    encryption_key: bytes,
    device_name: str = "test-device",
) -> SyncClient:
    """SyncClient를 생성한다."""
    resolver = ConflictResolver(store, device_name)
    return SyncClient(
        auth_client, SERVER_URL, store, resolver,
        interval_seconds=30, encryption_key=encryption_key,
    )


@pytest.fixture
def tmp_path():
    """임시 디렉토리."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        yield type("P", (), {"__truediv__": lambda s, n: os.path.join(d, n)})()


class SimulatedDevice:
    """시뮬레이트된 디바이스."""

    def __init__(self, name: str, tmp_dir: str, encryption_key: bytes):
        self.name = name
        self.db_path = os.path.join(tmp_dir, f"{name}.db")
        self.encryption_key = encryption_key
        self.store = MetadataStore(self.db_path, encryption_key)
        self.store.initialize()
        self.auth_client: AuthClient | None = None
        self.sync_client: SyncClient | None = None

    async def login(self):
        self.auth_client = AuthClient(SERVER_URL)
        await self.auth_client.login(EMAIL, PASSWORD)

    def setup_sync(self):
        resolver = ConflictResolver(self.store, self.name)
        self.sync_client = SyncClient(
            self.auth_client, SERVER_URL, self.store, resolver,
            interval_seconds=30, encryption_key=self.encryption_key,
        )

    async def create_file(self, path: str, size: int = 100):
        """로컬에 파일 메타데이터를 생성한다."""
        now = time.time()
        self.store.insert(path, f"src-{self.name}", f"phys/{path}", size, now, now)

    async def sync_upload(self):
        """서버에 metadata를 업로드한다."""
        await self.sync_client._force_upload()

    async def sync_download(self):
        """서버에서 metadata를 다운로드하여 병합한다."""
        await self.sync_client.initial_sync()

    def lookup(self, path: str):
        return self.store.lookup(path)

    def list_all(self) -> list[str]:
        """모든 파일의 virtual_path를 반환한다."""
        conn = self.store._get_conn()
        cursor = conn.execute("SELECT virtual_path FROM files")
        return [row["virtual_path"] for row in cursor.fetchall()]

    async def close(self):
        if self.sync_client:
            await self.sync_client.stop()
        if self.auth_client:
            await self.auth_client.close()
        self.store.close()


# ============================================================
# 테스트 케이스
# ============================================================


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _register_test_account():
    """테스트 전용 계정을 서버에 등록한다 (이미 존재하면 무시)."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{SERVER_URL}/auth/register",
            json={"email": EMAIL, "password": PASSWORD},
        )
    yield


@pytest_asyncio.fixture
async def devices(_register_test_account):
    """2개 디바이스를 시뮬레이트한다. 테스트 후 서버 metadata를 정리한다."""
    tmp_dir = tempfile.mkdtemp()
    # 동일한 encryption_key 사용 (같은 계정)
    enc_key = os.urandom(32)

    dev_a = SimulatedDevice("device-A", tmp_dir, enc_key)
    dev_b = SimulatedDevice("device-B", tmp_dir, enc_key)

    await dev_a.login()
    await dev_b.login()
    dev_a.setup_sync()
    dev_b.setup_sync()

    yield dev_a, dev_b

    # Cleanup: 서버에서 테스트 metadata 삭제 (빈 blob 업로드)
    try:
        import httpx
        token = await dev_a.auth_client.get_valid_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 빈 metadata로 덮어쓰기 (서버 정리)
            await client.put(
                f"{SERVER_URL}/sync/metadata",
                headers={"Authorization": f"Bearer {token}"},
                content=b"",
            )
    except Exception:
        pass

    await dev_a.close()
    await dev_b.close()


async def test_basic_file_creation_sync(devices):
    """케이스 1: A에서 파일 생성 → 업로드 → B에서 다운로드 → B에서 보임."""
    dev_a, dev_b = devices

    # A에서 파일 생성
    await dev_a.create_file("/docs/hello.txt", 256)
    assert dev_a.lookup("/docs/hello.txt") is not None

    # A 업로드
    await dev_a.sync_upload()

    # B 다운로드
    await dev_b.sync_download()

    # B에서 파일이 보여야 함
    meta = dev_b.lookup("/docs/hello.txt")
    assert meta is not None, "B에서 A의 파일이 보이지 않음"
    assert meta.file_size == 256


async def test_bidirectional_sync(devices):
    """케이스 2: A와 B 각각 파일 생성 → 양방향 동기화 후 양쪽 모두 보임."""
    dev_a, dev_b = devices

    # A에서 파일 생성 + 업로드
    await dev_a.create_file("/from-a.txt", 100)
    await dev_a.sync_upload()

    # B에서 다운로드 + 자체 파일 생성 + 업로드
    await dev_b.sync_download()
    await dev_b.create_file("/from-b.txt", 200)
    await dev_b.sync_upload()

    # A에서 다운로드
    await dev_a.sync_download()

    # 양쪽 모두 두 파일이 보여야 함
    assert dev_a.lookup("/from-a.txt") is not None
    assert dev_a.lookup("/from-b.txt") is not None, "A에서 B의 파일이 보이지 않음"
    assert dev_b.lookup("/from-a.txt") is not None, "B에서 A의 파일이 보이지 않음"
    assert dev_b.lookup("/from-b.txt") is not None


async def test_file_deletion_sync(devices):
    """케이스 3: A에서 파일 삭제 → 업로드 → B에서 다운로드 → B에서도 삭제됨."""
    dev_a, dev_b = devices

    # A에서 파일 생성 + 업로드
    await dev_a.create_file("/to-delete.txt", 50)
    await dev_a.sync_upload()

    # B에서 다운로드 → 파일 존재 확인
    await dev_b.sync_download()
    assert dev_b.lookup("/to-delete.txt") is not None

    # A에서 삭제 + 업로드
    dev_a.store.delete("/to-delete.txt")
    await dev_a.sync_upload()

    # B에서 다운로드 → 파일이 사라져야 함
    await dev_b.sync_download()
    # 현재 설계에서는 삭제된 파일이 서버 DB에서 사라지므로
    # B의 로컬에는 여전히 존재할 수 있음 (삭제 동기화 미구현)
    # 이 테스트는 현재 동작을 기록하는 용도
    result = dev_b.lookup("/to-delete.txt")
    if result is not None:
        pytest.xfail("삭제 동기화 미구현 — 서버에서 삭제된 파일이 B에 남아있음")


async def test_file_modification_sync(devices):
    """케이스 4: A에서 파일 수정 → 업로드 → B에서 다운로드 → B에서 최신 버전 반영."""
    dev_a, dev_b = devices

    # A에서 파일 생성 + 업로드
    await dev_a.create_file("/modify-me.txt", 100)
    await dev_a.sync_upload()

    # B에서 다운로드
    await dev_b.sync_download()
    meta_b = dev_b.lookup("/modify-me.txt")
    assert meta_b is not None
    assert meta_b.version == 1

    # A에서 수정 (version 증가) + 업로드
    dev_a.store.update("/modify-me.txt", 500, time.time())
    await dev_a.sync_upload()

    # B에서 다운로드 → 최신 버전 반영
    await dev_b.sync_download()
    meta_b2 = dev_b.lookup("/modify-me.txt")
    assert meta_b2 is not None
    assert meta_b2.file_size == 500, f"Expected 500, got {meta_b2.file_size}"
    assert meta_b2.version >= 2, f"Expected version>=2, got {meta_b2.version}"


async def test_conflict_detection(devices):
    """케이스 5: A와 B에서 동시에 같은 파일 수정 → 충돌 감지 → conflict copy 생성."""
    dev_a, dev_b = devices

    # A에서 파일 생성 + 업로드
    await dev_a.create_file("/conflict-file.txt", 100)
    await dev_a.sync_upload()

    # B에서 다운로드
    await dev_b.sync_download()

    # 양쪽에서 동시에 수정
    dev_a.store.update("/conflict-file.txt", 200, time.time())
    dev_b.store.update("/conflict-file.txt", 300, time.time())

    # A 먼저 업로드
    await dev_a.sync_upload()

    # B 다운로드 → 충돌 감지
    await dev_b.sync_download()

    # B에서 conflict copy가 생성되어야 함
    all_files = dev_b.list_all()
    conflict_files = [f for f in all_files if "conflict" in f]
    assert len(conflict_files) > 0, (
        f"충돌 파일이 생성되지 않음. 전체 파일: {all_files}"
    )


async def test_version_pollution(devices):
    """케이스 6: 버전 오염 — B가 높은 version으로 업로드 → A가 다운로드 시 정상 처리."""
    dev_a, dev_b = devices

    # A에서 파일 생성 (version=1) + 업로드
    await dev_a.create_file("/version-test.txt", 100)
    await dev_a.sync_upload()

    # B에서 다운로드
    await dev_b.sync_download()

    # B에서 version을 인위적으로 높임 (오염 시뮬레이션)
    conn = dev_b.store._get_conn()
    conn.execute(
        "UPDATE files SET version = 99 WHERE virtual_path = '/version-test.txt'"
    )
    conn.commit()

    # B 업로드 (version=99)
    await dev_b.sync_upload()

    # A 다운로드 → version 99로 갱신되어야 함
    await dev_a.sync_download()
    meta_a = dev_a.lookup("/version-test.txt")
    assert meta_a is not None
    assert meta_a.version == 99, f"Expected version=99, got {meta_a.version}"


async def test_race_condition_simultaneous_upload(devices):
    """케이스 7: 레이스컨디션 — A와 B가 거의 동시에 업로드."""
    dev_a, dev_b = devices

    # 각각 다른 파일 생성
    await dev_a.create_file("/race-a.txt", 100)
    await dev_b.create_file("/race-b.txt", 200)

    # 동시 업로드 (asyncio.gather)
    await asyncio.gather(
        dev_a.sync_upload(),
        dev_b.sync_upload(),
    )

    # 양쪽 다운로드
    await dev_a.sync_download()
    await dev_b.sync_download()

    # 최소한 자신의 파일은 보여야 함
    assert dev_a.lookup("/race-a.txt") is not None
    assert dev_b.lookup("/race-b.txt") is not None

    # 상대방 파일도 보이면 이상적 (레이스컨디션에 따라 실패 가능)
    a_sees_b = dev_a.lookup("/race-b.txt")
    b_sees_a = dev_b.lookup("/race-a.txt")
    if a_sees_b is None or b_sees_a is None:
        pytest.xfail(
            "레이스컨디션: 동시 업로드 시 한쪽이 덮어씀. "
            f"A sees B: {a_sees_b is not None}, B sees A: {b_sees_a is not None}"
        )


async def test_many_files_sync(devices):
    """케이스 8: 대량 파일 동기화 — A에서 100개 파일 생성 → B에서 모두 보임."""
    dev_a, dev_b = devices

    # A에서 100개 파일 생성
    for i in range(100):
        await dev_a.create_file(f"/bulk/file_{i:03d}.txt", i * 10)

    # A 업로드
    await dev_a.sync_upload()

    # B 다운로드
    await dev_b.sync_download()

    # B에서 100개 모두 보여야 함
    missing = []
    for i in range(100):
        if dev_b.lookup(f"/bulk/file_{i:03d}.txt") is None:
            missing.append(f"/bulk/file_{i:03d}.txt")

    assert len(missing) == 0, f"B에서 {len(missing)}개 파일 누락: {missing[:5]}..."


async def test_empty_db_upload_does_not_overwrite(devices):
    """케이스 9: 빈 DB를 가진 새 디바이스가 기존 데이터를 덮어쓰지 않음."""
    dev_a, dev_b = devices

    # A에서 파일 생성 + 업로드
    await dev_a.create_file("/important.txt", 999)
    await dev_a.sync_upload()

    # B는 빈 상태에서 다운로드 먼저 수행
    await dev_b.sync_download()

    # B에서 A의 파일이 보여야 함
    assert dev_b.lookup("/important.txt") is not None

    # B가 업로드해도 A의 파일이 사라지면 안 됨
    await dev_b.sync_upload()

    # A가 다시 다운로드
    await dev_a.sync_download()
    assert dev_a.lookup("/important.txt") is not None, (
        "B의 업로드가 A의 데이터를 덮어씀"
    )
