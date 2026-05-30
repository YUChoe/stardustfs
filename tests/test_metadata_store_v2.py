"""MetadataStore v2 메서드 단위 테스트.

Task 4.2: version 증가 및 sync_status 관리 메서드 테스트.
Requirements: 4.5, 4.6
"""

import time

import pytest

from stardustlib.metadata_store import MetadataStore


@pytest.fixture
def store(tmp_path):
    """테스트용 MetadataStore (v2 마이그레이션 완료 상태)."""
    db_path = str(tmp_path / "test_meta.db")
    ms = MetadataStore(db_path, b"\x00" * 32)
    ms.initialize()
    return ms


@pytest.fixture
def store_with_file(store):
    """파일 1개가 삽입된 MetadataStore."""
    store.insert("/test/file.txt", "src-1", "phys/file.enc", 1024, time.time(), time.time())
    return store


class TestInsertDefaults:
    """insert() 시 version=1, sync_status='pending' 기본값 테스트."""

    def test_insert_sets_version_1(self, store):
        now = time.time()
        store.insert("/a.txt", "src-1", "a.enc", 100, now, now)
        meta = store.lookup("/a.txt")
        assert meta is not None
        assert meta.version == 1

    def test_insert_sets_sync_status_pending(self, store):
        now = time.time()
        store.insert("/b.txt", "src-1", "b.enc", 200, now, now)
        meta = store.lookup("/b.txt")
        assert meta is not None
        assert meta.sync_status == "pending"


class TestUpdate:
    """update() 시 version 증가 및 sync_status='pending' 테스트."""

    def test_update_increments_version(self, store_with_file):
        store_with_file.update("/test/file.txt", 2048, time.time())
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.version == 2

    def test_update_sets_pending(self, store_with_file):
        # insert 시 이미 pending이므로, synced로 변경 후 update 확인
        store_with_file.set_sync_status("/test/file.txt", "synced")
        store_with_file.update("/test/file.txt", 2048, time.time())
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.sync_status == "pending"

    def test_update_multiple_increments(self, store_with_file):
        store_with_file.update("/test/file.txt", 3000, time.time())
        store_with_file.update("/test/file.txt", 4000, time.time())
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.version == 3


class TestIncrementVersion:
    """increment_version() 테스트."""

    def test_increments_version(self, store_with_file):
        store_with_file.increment_version("/test/file.txt", "device-abc")
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.version == 2

    def test_sets_device_id(self, store_with_file):
        store_with_file.increment_version("/test/file.txt", "device-xyz")
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.device_id == "device-xyz"

    def test_multiple_increments(self, store_with_file):
        store_with_file.increment_version("/test/file.txt", "dev-1")
        store_with_file.increment_version("/test/file.txt", "dev-2")
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.version == 3
        assert meta.device_id == "dev-2"


class TestSetSyncStatus:
    """set_sync_status() 테스트."""

    def test_set_synced(self, store_with_file):
        store_with_file.set_sync_status("/test/file.txt", "synced")
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.sync_status == "synced"

    def test_set_pending(self, store_with_file):
        store_with_file.set_sync_status("/test/file.txt", "synced")
        store_with_file.set_sync_status("/test/file.txt", "pending")
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.sync_status == "pending"

    def test_set_conflict(self, store_with_file):
        store_with_file.set_sync_status("/test/file.txt", "conflict")
        meta = store_with_file.lookup("/test/file.txt")
        assert meta is not None
        assert meta.sync_status == "conflict"

    def test_invalid_status_raises(self, store_with_file):
        with pytest.raises(ValueError, match="유효하지 않은 sync_status"):
            store_with_file.set_sync_status("/test/file.txt", "invalid")


class TestGetPendingFiles:
    """get_pending_files() 테스트."""

    def test_returns_pending_files(self, store):
        now = time.time()
        store.insert("/p1.txt", "src-1", "p1.enc", 100, now, now)
        store.insert("/p2.txt", "src-1", "p2.enc", 200, now, now)
        store.insert("/s1.txt", "src-1", "s1.enc", 300, now, now)
        store.set_sync_status("/s1.txt", "synced")

        pending = store.get_pending_files()
        paths = [f.virtual_path for f in pending]
        assert "/p1.txt" in paths
        assert "/p2.txt" in paths
        assert "/s1.txt" not in paths

    def test_empty_when_all_synced(self, store):
        now = time.time()
        store.insert("/x.txt", "src-1", "x.enc", 50, now, now)
        store.set_sync_status("/x.txt", "synced")

        pending = store.get_pending_files()
        assert pending == []

    def test_returns_correct_metadata(self, store):
        now = time.time()
        store.insert("/detail.txt", "src-2", "detail.enc", 512, now, now)
        store.increment_version("/detail.txt", "my-device")

        pending = store.get_pending_files()
        assert len(pending) == 1
        f = pending[0]
        assert f.virtual_path == "/detail.txt"
        assert f.source_id == "src-2"
        assert f.version == 2
        assert f.device_id == "my-device"
        assert f.sync_status == "pending"
