"""ConflictResolver 단위 테스트."""

import os
import tempfile

import pytest

from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.metadata_store import MetadataStore


@pytest.fixture
def metadata_store():
    """테스트용 MetadataStore 인스턴스를 생성한다."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    store = MetadataStore(db_path, b"\x00" * 32)
    store.initialize()
    yield store
    store.close()
    os.unlink(db_path)
    # 백업 파일도 정리
    backup = db_path + ".v1.bak"
    if os.path.exists(backup):
        os.unlink(backup)


@pytest.fixture
def resolver(metadata_store):
    """테스트용 ConflictResolver 인스턴스를 생성한다."""
    return ConflictResolver(metadata_store, device_name="my-desktop")


class TestDetectConflict:
    """detect_conflict() 테스트."""

    def test_conflict_both_modified(self, resolver):
        """양쪽 모두 base 이후 수정 → 충돌."""
        assert resolver.detect_conflict(
            "/docs/file.txt",
            server_version=3,
            local_version=2,
            local_base_version=1,
        ) is True

    def test_no_conflict_only_server_modified(self, resolver):
        """서버만 수정, 로컬은 base와 동일 → 충돌 아님."""
        assert resolver.detect_conflict(
            "/docs/file.txt",
            server_version=3,
            local_version=1,
            local_base_version=1,
        ) is False

    def test_no_conflict_only_local_modified(self, resolver):
        """로컬만 수정, 서버는 base와 동일 → 충돌 아님."""
        assert resolver.detect_conflict(
            "/docs/file.txt",
            server_version=1,
            local_version=2,
            local_base_version=1,
        ) is False

    def test_no_conflict_same_version(self, resolver):
        """양쪽 모두 base와 동일 → 충돌 아님."""
        assert resolver.detect_conflict(
            "/docs/file.txt",
            server_version=1,
            local_version=1,
            local_base_version=1,
        ) is False

    def test_no_conflict_server_equals_base(self, resolver):
        """server_version == local_base_version → 충돌 아님."""
        assert resolver.detect_conflict(
            "/docs/file.txt",
            server_version=2,
            local_version=3,
            local_base_version=2,
        ) is False


class TestGenerateConflictName:
    """generate_conflict_name() 테스트."""

    def test_basic_format(self, resolver):
        """기본 형식 검증."""
        result = resolver.generate_conflict_name("/docs/report.txt")
        assert "conflict" in result
        assert "my-desktop" in result
        assert result.startswith("/docs/")
        assert result.endswith(".txt")

    def test_no_extension(self, resolver):
        """확장자 없는 파일."""
        result = resolver.generate_conflict_name("/docs/Makefile")
        assert "conflict" in result
        assert "my-desktop" in result
        assert not result.endswith(".")

    def test_duplicate_adds_sequence(self, resolver, metadata_store):
        """동일 파일명 존재 시 순번 추가."""
        import time

        # 첫 번째 conflict name 생성
        first = resolver.generate_conflict_name("/docs/file.txt")

        # 해당 경로에 레코드 삽입
        metadata_store.insert(
            first, "vol1", "/phys/file.enc", 100,
            time.time(), time.time(),
        )

        # 두 번째 conflict name 생성 → (2) 순번
        second = resolver.generate_conflict_name("/docs/file.txt")
        assert second != first
        assert "(2)" in second

    def test_multiple_duplicates(self, resolver, metadata_store):
        """여러 중복 시 순번 증가."""
        import time

        first = resolver.generate_conflict_name("/docs/file.txt")
        metadata_store.insert(
            first, "vol1", "/phys/file.enc", 100,
            time.time(), time.time(),
        )

        second = resolver.generate_conflict_name("/docs/file.txt")
        metadata_store.insert(
            second, "vol1", "/phys/file2.enc", 100,
            time.time(), time.time(),
        )

        third = resolver.generate_conflict_name("/docs/file.txt")
        assert "(3)" in third

    def test_root_path_file(self, resolver):
        """루트 경로의 파일."""
        result = resolver.generate_conflict_name("file.txt")
        assert "conflict" in result
        assert result.endswith(".txt")
        assert "/" not in result or result.startswith("/")


class TestResolveConflict:
    """resolve_conflict() 테스트."""

    def test_resolve_creates_conflict_copy(self, resolver, metadata_store):
        """충돌 해결 시 conflict copy가 생성된다."""
        import time

        now = time.time()
        metadata_store.insert(
            "/docs/file.txt", "vol1", "/phys/file.enc",
            1024, now, now,
        )

        conflict_path = resolver.resolve_conflict("/docs/file.txt", server_version=3)

        # conflict copy가 존재해야 함
        conflict_meta = metadata_store.lookup(conflict_path)
        assert conflict_meta is not None
        assert conflict_meta.sync_status == "conflict"

        # 원본 경로는 rename으로 이동했으므로 없어야 함
        original_meta = metadata_store.lookup("/docs/file.txt")
        assert original_meta is None

    def test_resolve_returns_conflict_path(self, resolver, metadata_store):
        """resolve_conflict()이 conflict copy 경로를 반환한다."""
        import time

        now = time.time()
        metadata_store.insert(
            "/docs/report.pdf", "vol1", "/phys/report.enc",
            2048, now, now,
        )

        conflict_path = resolver.resolve_conflict(
            "/docs/report.pdf", server_version=5,
        )

        assert "conflict" in conflict_path
        assert "my-desktop" in conflict_path
        assert conflict_path.endswith(".pdf")

    def test_resolve_failure_keeps_pending(self, metadata_store):
        """conflict copy 생성 실패 시 sync_status가 pending으로 유지된다."""
        import time
        from unittest.mock import patch

        now = time.time()
        metadata_store.insert(
            "/docs/file.txt", "vol1", "/phys/file.enc",
            1024, now, now,
        )

        resolver = ConflictResolver(metadata_store, device_name="my-desktop")

        # rename_path가 예외를 발생시키도록 mock
        with patch.object(
            metadata_store, "rename_path", side_effect=OSError("disk full")
        ):
            with pytest.raises(OSError):
                resolver.resolve_conflict("/docs/file.txt", server_version=3)

        # sync_status가 "pending"으로 설정되어야 함
        meta = metadata_store.lookup("/docs/file.txt")
        assert meta is not None
        assert meta.sync_status == "pending"
