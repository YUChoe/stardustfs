"""JBODManager 디렉토리 작업 단위 테스트."""

import os
import tempfile

import pytest

from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import EntryInfo
from stardustlib.storage_source import DirectorySource


@pytest.fixture
def setup_jbod(tmp_path):
    """테스트용 JBODManager 환경을 구성한다."""
    # 두 개의 디렉토리 소스 생성
    src1_path = str(tmp_path / "source1")
    src2_path = str(tmp_path / "source2")
    os.makedirs(src1_path)
    os.makedirs(src2_path)

    source1 = DirectorySource("src-001", src1_path)
    source1.initialize()
    source2 = DirectorySource("src-002", src2_path)
    source2.initialize()

    # 메타데이터 저장소 초기화
    db_path = str(tmp_path / "test_meta.db")
    metadata_store = MetadataStore(db_path, b"\x00" * 32)
    metadata_store.initialize()

    jbod = JBODManager(
        sources=[source1, source2],
        metadata_store=metadata_store,
        encryption_engine=None,
    )

    return jbod, metadata_store, source1, source2


class TestListDirectory:
    """list_directory 테스트."""

    def test_empty_directory(self, setup_jbod):
        """빈 디렉토리 목록 조회 시 빈 리스트를 반환한다."""
        jbod, _, _, _ = setup_jbod
        entries = jbod.list_directory("/")
        assert entries == []

    def test_list_files(self, setup_jbod):
        """파일이 있는 디렉토리 목록을 조회한다."""
        jbod, _, _, _ = setup_jbod
        jbod.write_file("/docs/file1.txt", b"hello")
        jbod.write_file("/docs/file2.txt", b"world")

        entries = jbod.list_directory("/docs")
        names = {e.name for e in entries}
        assert "file1.txt" in names
        assert "file2.txt" in names
        assert len(entries) == 2

    def test_list_mixed_entries(self, setup_jbod):
        """파일과 디렉토리가 혼합된 목록을 조회한다."""
        jbod, _, _, _ = setup_jbod
        jbod.create_directory("/projects/subdir")
        jbod.write_file("/projects/readme.md", b"# README")

        entries = jbod.list_directory("/projects")
        names = {e.name for e in entries}
        assert "subdir" in names
        assert "readme.md" in names

    def test_deduplication(self, setup_jbod):
        """동일 이름 엔트리는 중복 없이 하나만 반환한다."""
        jbod, _, _, _ = setup_jbod
        # 디렉토리 생성 후 같은 이름의 하위 파일 생성
        jbod.create_directory("/data/logs")
        jbod.write_file("/data/logs/app.log", b"log data")

        entries = jbod.list_directory("/data")
        names = [e.name for e in entries]
        assert names.count("logs") == 1


class TestCreateDirectory:
    """create_directory 테스트."""

    def test_create_in_all_sources(self, setup_jbod):
        """모든 활성 소스에 디렉토리가 생성된다."""
        jbod, _, source1, source2 = setup_jbod
        jbod.create_directory("/newdir")

        assert source1.exists("newdir")
        assert source2.exists("newdir")

    def test_create_nested_directory(self, setup_jbod):
        """중첩 디렉토리를 생성한다."""
        jbod, _, source1, source2 = setup_jbod
        jbod.create_directory("/a/b/c")

        assert source1.exists("a/b/c")
        assert source2.exists("a/b/c")

    def test_partial_failure_logged(self, setup_jbod):
        """일부 소스에서 실패해도 나머지 소스에서는 디렉토리가 유지된다."""
        jbod, _, source1, source2 = setup_jbod
        # source2를 비활성화
        source2._active = False

        jbod.create_directory("/onlyone")
        assert source1.exists("onlyone")
        # source2는 비활성이므로 생성되지 않음

    def test_metadata_recorded(self, setup_jbod):
        """디렉토리 메타데이터가 기록된다."""
        jbod, metadata_store, _, _ = setup_jbod
        jbod.create_directory("/recorded")

        # list_entries로 확인 (루트에서 조회)
        entries = metadata_store.list_entries("/")
        names = [e.name for e in entries]
        assert "recorded" in names


class TestDeleteDirectory:
    """delete_directory 테스트."""

    def test_delete_empty_directory(self, setup_jbod):
        """빈 디렉토리를 삭제한다."""
        jbod, metadata_store, source1, source2 = setup_jbod
        jbod.create_directory("/todelete")
        jbod.delete_directory("/todelete")

        # 물리 디렉토리 삭제 확인
        assert not source1.exists("todelete")
        assert not source2.exists("todelete")

    def test_delete_with_files(self, setup_jbod):
        """하위 파일이 있는 디렉토리를 재귀적으로 삭제한다."""
        jbod, metadata_store, _, _ = setup_jbod
        jbod.create_directory("/parent")
        jbod.write_file("/parent/child.txt", b"data")

        jbod.delete_directory("/parent")

        # 파일 메타데이터도 삭제됨
        assert metadata_store.lookup("/parent/child.txt") is None

    def test_delete_nested(self, setup_jbod):
        """중첩 디렉토리를 재귀적으로 삭제한다."""
        jbod, metadata_store, _, _ = setup_jbod
        jbod.create_directory("/a/b")
        jbod.write_file("/a/b/file.txt", b"nested")
        jbod.write_file("/a/top.txt", b"top")

        jbod.delete_directory("/a")

        assert metadata_store.lookup("/a/b/file.txt") is None
        assert metadata_store.lookup("/a/top.txt") is None


class TestMoveDirectory:
    """move_directory 테스트."""

    def test_move_updates_metadata(self, setup_jbod):
        """디렉토리 이동 시 하위 파일 경로가 갱신된다."""
        jbod, metadata_store, _, _ = setup_jbod
        jbod.create_directory("/old")
        jbod.write_file("/old/file.txt", b"content")

        jbod.move_directory("/old", "/new")

        # 기존 경로로는 조회 불가
        assert metadata_store.lookup("/old/file.txt") is None
        # 새 경로로 조회 가능
        result = metadata_store.lookup("/new/file.txt")
        assert result is not None
        assert result.virtual_path == "/new/file.txt"

    def test_move_nested_files(self, setup_jbod):
        """중첩 파일들의 경로가 모두 갱신된다."""
        jbod, metadata_store, _, _ = setup_jbod
        jbod.create_directory("/src/sub")
        jbod.write_file("/src/a.txt", b"a")
        jbod.write_file("/src/sub/b.txt", b"b")

        jbod.move_directory("/src", "/dst")

        assert metadata_store.lookup("/dst/a.txt") is not None
        assert metadata_store.lookup("/dst/sub/b.txt") is not None
        assert metadata_store.lookup("/src/a.txt") is None
        assert metadata_store.lookup("/src/sub/b.txt") is None
