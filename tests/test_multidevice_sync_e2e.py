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

SERVER_URL = os.environ.get("STARDUST_TEST_SERVER_URL", "https://stardustfs.noizze.net")
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
        """서버에 metadata를 업로드한다 (강제)."""
        await self.sync_client._force_upload()

    async def sync_upload_pending(self):
        """pending 변경이 있을 때만 업로드한다 (실제 주기 동기화 경로)."""
        await self.sync_client.upload_metadata()

    async def sync_download(self):
        """서버에서 metadata를 다운로드하여 병합한다 (version 추적 포함)."""
        await self.sync_client._download_and_merge()

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
    """케이스 3: A에서 파일 삭제(tombstone) → 업로드 → B에서 다운로드 → B에서도 삭제됨."""
    dev_a, dev_b = devices

    # A에서 파일 생성 + 업로드
    await dev_a.create_file("/to-delete.txt", 50)
    await dev_a.sync_upload()

    # B에서 다운로드 → 파일 존재 확인
    await dev_b.sync_download()
    assert dev_b.lookup("/to-delete.txt") is not None

    # A에서 삭제(soft delete, tombstone) + 업로드
    dev_a.store.delete("/to-delete.txt")
    await dev_a.sync_upload()

    # B에서 다운로드 → tombstone이 전파되어 파일이 사라져야 함
    await dev_b.sync_download()
    result = dev_b.lookup("/to-delete.txt")
    assert result is None, (
        "tombstone 삭제 동기화 실패 — A에서 삭제한 파일이 B에 남아있음"
    )


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
    """케이스 7: 레이스컨디션 — A와 B가 거의 동시에 업로드해도 CAS로 유실 방지.

    공통 기반(공유 파일)을 만든 뒤 A/B가 각자 다른 파일을 추가하고,
    CAS 업로드 경로(upload_metadata)로 거의 동시에 올린다. 낙관적 잠금에 의해
    한쪽은 409 후 재병합·재시도하므로 양쪽 변경이 모두 보존되어야 한다.
    """
    dev_a, dev_b = devices

    # 공통 기반: A가 파일 생성·업로드, B가 동일 서버 상태로 동기화
    await dev_a.create_file("/race-base.txt", 10)
    await dev_a.sync_upload()
    await dev_b.sync_download()

    # 각자 다른 파일 생성 (pending)
    await dev_a.create_file("/race-a.txt", 100)
    await dev_b.create_file("/race-b.txt", 200)

    # CAS 경로로 동시 업로드
    await asyncio.gather(
        dev_a.sync_upload_pending(),
        dev_b.sync_upload_pending(),
    )

    # 양쪽 다운로드
    await dev_a.sync_download()
    await dev_b.sync_download()

    # CAS 덕분에 양쪽 파일이 모두 서버에 반영되어 서로 보여야 함
    assert dev_a.lookup("/race-a.txt") is not None
    assert dev_b.lookup("/race-b.txt") is not None
    assert dev_a.lookup("/race-b.txt") is not None, (
        "CAS 실패: A에서 B의 파일이 보이지 않음 (B 변경 유실)"
    )
    assert dev_b.lookup("/race-a.txt") is not None, (
        "CAS 실패: B에서 A의 파일이 보이지 않음 (A 변경 유실)"
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


async def test_delete_then_recreate_sync(devices):
    """케이스 10: A에서 삭제한 파일을 다시 생성하면 B에서 재생성이 반영됨.

    tombstone(version=2) → 재생성(version=3) 순으로 version이 증가하므로
    재생성이 삭제보다 우선해야 한다.
    """
    dev_a, dev_b = devices

    # A에서 생성 + 업로드 (version=1)
    await dev_a.create_file("/recreate.txt", 100)
    await dev_a.sync_upload()

    # B 다운로드 → 존재 확인
    await dev_b.sync_download()
    assert dev_b.lookup("/recreate.txt") is not None

    # A에서 삭제(version=2) → 재생성(version=3)
    dev_a.store.delete("/recreate.txt")
    await dev_a.create_file("/recreate.txt", 777)
    await dev_a.sync_upload()

    # B 다운로드 → 재생성된 파일이 보여야 함
    await dev_b.sync_download()
    meta = dev_b.lookup("/recreate.txt")
    assert meta is not None, "재생성된 파일이 B에 반영되지 않음"
    assert meta.file_size == 777


async def test_pending_gate_skips_upload_when_no_changes(devices):
    """케이스 11: pending 변경이 없으면 upload_metadata는 서버 version을 올리지 않음.

    삭제 후 dirty 상태에서는 업로드되고, 업로드 후에는 pending이 없어
    추가 업로드가 일어나지 않아야 한다 (version 무한 증가 방지).
    """
    dev_a, dev_b = devices

    # A에서 생성 후 강제 업로드로 서버 초기화
    await dev_a.create_file("/gate.txt", 100)
    await dev_a.sync_upload()

    # 서버 현재 version 조회
    v1 = await _server_version(dev_a)

    # pending 없는 상태에서 upload_metadata 호출 → 업로드 건너뜀
    await dev_a.sync_upload_pending()
    v2 = await _server_version(dev_a)
    assert v2 == v1, f"pending 없는데 업로드됨 (v1={v1}, v2={v2})"

    # 삭제(tombstone, pending 발생) 후 upload_metadata → 업로드됨
    dev_a.store.delete("/gate.txt")
    await dev_a.sync_upload_pending()
    v3 = await _server_version(dev_a)
    assert v3 > v2, f"삭제 후에도 업로드 안 됨 (v2={v2}, v3={v3})"

    # 다시 pending 없는 상태 → 업로드 건너뜀
    await dev_a.sync_upload_pending()
    v4 = await _server_version(dev_a)
    assert v4 == v3, f"pending 없는데 또 업로드됨 (v3={v3}, v4={v4})"


async def test_deletion_sync_via_pending_path(devices):
    """케이스 12: 실제 주기 동기화 경로(upload_metadata)로 삭제가 전파됨."""
    dev_a, dev_b = devices

    # A 생성 + 업로드
    await dev_a.create_file("/pending-delete.txt", 50)
    await dev_a.sync_upload()

    # B 다운로드 → 존재
    await dev_b.sync_download()
    assert dev_b.lookup("/pending-delete.txt") is not None

    # A 삭제 후 pending 경로로 업로드
    dev_a.store.delete("/pending-delete.txt")
    await dev_a.sync_upload_pending()

    # B 다운로드 → 삭제 전파 확인
    await dev_b.sync_download()
    assert dev_b.lookup("/pending-delete.txt") is None, (
        "pending 경로 삭제 동기화 실패"
    )


async def _server_version(dev) -> int:
    """서버의 현재 metadata version을 조회한다. 없으면 0."""
    import httpx

    token = await dev.auth_client.get_valid_token()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{SERVER_URL}/sync/metadata/status",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return 0
    return resp.json().get("version") or 0


async def test_tombstone_gc_removes_expired(devices):
    """케이스 13: 보관기간이 지난 tombstone은 GC로 정리된다."""
    dev_a, dev_b = devices

    # A에서 파일 생성·업로드
    await dev_a.create_file("/gc-target.txt", 50)
    await dev_a.sync_upload()

    # A에서 삭제 (tombstone 생성)
    dev_a.store.delete("/gc-target.txt")

    # tombstone의 modified_at을 과거로 조작 (보관기간 초과 시뮬레이션)
    conn = dev_a.store._get_conn()
    conn.execute(
        "UPDATE files SET modified_at = ? WHERE virtual_path = '/gc-target.txt'",
        (time.time() - 40 * 86400,),
    )
    conn.commit()

    # 보관기간을 30일로 설정하고 GC 수행
    dev_a.sync_client._retention_seconds = 30 * 86400
    removed = dev_a.store.purge_expired_tombstones(30 * 86400)

    assert removed == 1
    # tombstone이 물리적으로 제거됨 (lookup_any로도 조회 안 됨)
    assert dev_a.store.lookup_any("/gc-target.txt") is None


async def test_tombstone_gc_preserves_active_and_fresh(devices):
    """케이스 14: GC는 활성 파일과 최근 tombstone을 보존한다."""
    dev_a, _dev_b = devices

    # 활성 파일 (오래됨)
    await dev_a.create_file("/keep-active.txt", 10)
    conn = dev_a.store._get_conn()
    conn.execute(
        "UPDATE files SET modified_at = ? WHERE virtual_path = '/keep-active.txt'",
        (time.time() - 100 * 86400,),
    )
    conn.commit()

    # 최근 삭제된 tombstone
    await dev_a.create_file("/recent-delete.txt", 10)
    dev_a.store.delete("/recent-delete.txt")  # modified_at = now

    dev_a.store.purge_expired_tombstones(30 * 86400)

    # 활성 파일은 오래돼도 보존
    assert dev_a.store.lookup("/keep-active.txt") is not None
    # 최근 tombstone은 보관기간 내라 보존 (lookup_any로 확인)
    assert dev_a.store.lookup_any("/recent-delete.txt") is not None
