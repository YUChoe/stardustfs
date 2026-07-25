"""StoragePool 핵심 파일 작업 단위 테스트."""

import os
import tempfile

import pytest

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import InsufficientStorageError
from stardustlib.storage_pool import StoragePool
from stardustlib.metadata_store import MetadataStore
from stardustlib.storage_source import DirectorySource


@pytest.fixture
def temp_dirs():
    """테스트용 임시 디렉토리 2개 생성."""
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        yield d1, d2


@pytest.fixture
def encryption_engine():
    """테스트용 EncryptionEngine."""
    key = os.urandom(32)
    return EncryptionEngine(key)


@pytest.fixture
def metadata_store(tmp_path):
    """테스트용 MetadataStore."""
    db_path = str(tmp_path / "test_meta.db")
    store = MetadataStore(db_path, os.urandom(32))
    store.initialize()
    return store


@pytest.fixture
def storage_pool(temp_dirs, metadata_store, encryption_engine):
    """테스트용 StoragePool."""
    d1, d2 = temp_dirs
    src1 = DirectorySource("src-1", d1)
    src1.initialize()
    src2 = DirectorySource("src-2", d2)
    src2.initialize()
    return StoragePool([src1, src2], metadata_store, encryption_engine)


class TestWriteAndReadFile:
    """write_file / read_file 테스트."""

    def test_write_and_read_roundtrip(self, storage_pool):
        """파일 쓰기 후 읽기 시 원본 데이터가 반환된다."""
        data = b"Hello StardustFS!"
        storage_pool.write_file("/docs/hello.txt", data)
        result = storage_pool.read_file("/docs/hello.txt")
        assert result == data

    def test_overwrite_existing_file(self, storage_pool):
        """기존 파일 덮어쓰기 시 새 데이터가 반환된다."""
        storage_pool.write_file("/file.bin", b"original")
        storage_pool.write_file("/file.bin", b"updated")
        assert storage_pool.read_file("/file.bin") == b"updated"

    def test_read_nonexistent_file_raises(self, storage_pool):
        """존재하지 않는 파일 읽기 시 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            storage_pool.read_file("/no/such/file.txt")

    def test_write_records_device_id(
        self, temp_dirs, metadata_store, encryption_engine
    ):
        """device_id를 가진 스토리지 풀로 파일을 쓰면 메타데이터에 device_id가 기록된다."""
        d1, _d2 = temp_dirs
        src = DirectorySource("src-dev", d1)
        src.initialize()
        storage_pool = StoragePool(
            [src], metadata_store, encryption_engine, device_id="dev-123"
        )

        # 신규 생성 → device_id 기록
        storage_pool.write_file("/a.txt", b"data")
        rec = metadata_store.lookup("/a.txt")
        assert rec is not None
        assert rec.device_id == "dev-123"

        # 수정 → device_id 유지/갱신
        storage_pool.write_file("/a.txt", b"updated data")
        rec2 = metadata_store.lookup("/a.txt")
        assert rec2.device_id == "dev-123"

    def test_write_without_device_id_is_null(self, storage_pool, metadata_store):
        """device_id 없는 스토리지 풀(기본)로 쓰면 device_id는 NULL이다."""
        storage_pool.write_file("/b.txt", b"data")
        rec = metadata_store.lookup("/b.txt")
        assert rec is not None
        assert rec.device_id is None


    def test_write_empty_data(self, storage_pool):
        """빈 데이터 쓰기/읽기."""
        storage_pool.write_file("/empty.bin", b"")
        assert storage_pool.read_file("/empty.bin") == b""


class TestDeleteFile:
    """delete_file 테스트."""

    def test_delete_existing_file(self, storage_pool):
        """파일 삭제 후 읽기 시 FileNotFoundError."""
        storage_pool.write_file("/to_delete.txt", b"data")
        storage_pool.delete_file("/to_delete.txt")
        with pytest.raises(FileNotFoundError):
            storage_pool.read_file("/to_delete.txt")

    def test_delete_nonexistent_file_raises(self, storage_pool):
        """존재하지 않는 파일 삭제 시 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            storage_pool.delete_file("/nonexistent.txt")


class TestMoveFile:
    """move_file 테스트."""

    def test_move_file(self, storage_pool):
        """파일 이동 후 새 경로에서 읽기 가능, 원본 경로는 없음."""
        storage_pool.write_file("/src.txt", b"move me")
        storage_pool.move_file("/src.txt", "/dst.txt")
        assert storage_pool.read_file("/dst.txt") == b"move me"
        with pytest.raises(FileNotFoundError):
            storage_pool.read_file("/src.txt")

    def test_move_nonexistent_file_raises(self, storage_pool):
        """존재하지 않는 파일 이동 시 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            storage_pool.move_file("/no.txt", "/dst.txt")


class TestCopyFile:
    """copy_file 테스트."""

    def test_copy_file(self, storage_pool):
        """파일 복사 후 원본과 사본 모두 읽기 가능."""
        storage_pool.write_file("/original.txt", b"copy me")
        storage_pool.copy_file("/original.txt", "/copy.txt")
        assert storage_pool.read_file("/original.txt") == b"copy me"
        assert storage_pool.read_file("/copy.txt") == b"copy me"

    def test_copy_nonexistent_file_raises(self, storage_pool):
        """존재하지 않는 파일 복사 시 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            storage_pool.copy_file("/no.txt", "/dst.txt")


class TestSelectSource:
    """select_source 테스트."""

    def test_selects_source_with_most_space(self, storage_pool):
        """가용 공간이 가장 많은 소스를 선택한다."""
        source = storage_pool.select_source(100)
        assert source.is_active

    def test_raises_when_no_space(self, temp_dirs, metadata_store, encryption_engine):
        """모든 소스에 공간이 부족하면 InsufficientStorageError."""
        d1, d2 = temp_dirs
        src1 = DirectorySource("src-1", d1)
        src1.initialize()
        mgr = StoragePool([src1], metadata_store, encryption_engine)
        # 매우 큰 파일 크기 요청
        with pytest.raises(InsufficientStorageError):
            mgr.select_source(10**18)


class TestFileExistsAndInfo:
    """file_exists / get_file_info 테스트."""

    def test_file_exists(self, storage_pool):
        """파일 존재 여부 확인."""
        assert not storage_pool.file_exists("/test.txt")
        storage_pool.write_file("/test.txt", b"data")
        assert storage_pool.file_exists("/test.txt")

    def test_get_file_info(self, storage_pool):
        """파일 정보 조회."""
        storage_pool.write_file("/info.txt", b"hello")
        info = storage_pool.get_file_info("/info.txt")
        assert info is not None
        assert info.file_size == 5
        assert info.virtual_path == "/info.txt"
        assert not info.is_directory

    def test_get_file_info_nonexistent(self, storage_pool):
        """존재하지 않는 파일 정보 조회 시 None."""
        assert storage_pool.get_file_info("/no.txt") is None


class TestDeactivateSource:
    """deactivate_source 테스트."""

    def test_deactivate_source(self, storage_pool):
        """소스 비활성화 후 해당 소스의 파일 접근 시 OSError."""
        storage_pool.write_file("/on_src.txt", b"data")
        info = storage_pool.get_file_info("/on_src.txt")
        storage_pool.deactivate_source(info.source_id)
        with pytest.raises(OSError):
            storage_pool.read_file("/on_src.txt")

    def test_deactivate_nonexistent_source_raises(self, storage_pool):
        """존재하지 않는 소스 비활성화 시 ValueError."""
        with pytest.raises(ValueError):
            storage_pool.deactivate_source("nonexistent-id")


class TestSpaceInfo:
    """get_total_space / get_available_space 테스트."""

    def test_total_space_positive(self, storage_pool):
        """전체 공간이 양수."""
        assert storage_pool.get_total_space() > 0

    def test_available_space_positive(self, storage_pool):
        """가용 공간이 양수."""
        assert storage_pool.get_available_space() > 0


class _FakeRemoteSource:
    """오프라인 원격 소스 모사: 용량/쓰기 호출 시 OSError를 던진다.

    routing은 성공해 is_active=True지만 실제 디바이스는 오프라인인 상황을 재현한다.
    is_remote=True이므로 스토리지 풀의 로컬 용량 집계/쓰기 대상 선택에서 제외되어야 한다.
    """

    def __init__(self, source_id: str) -> None:
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def is_active(self) -> bool:
        return True

    @property
    def is_remote(self) -> bool:
        return True

    def get_total_space(self) -> int:
        raise OSError("P2P connection failed (/p2p/space)")

    def get_available_space(self) -> int:
        raise OSError("P2P connection failed (/p2p/space)")


class TestRemoteSourceExcludedFromLocalCapacity:
    """오프라인 원격 소스가 로컬 용량 집계/쓰기 선택을 깨뜨리지 않아야 한다."""

    def test_total_space_ignores_remote(self, storage_pool, monkeypatch):
        """get_total_space는 원격 소스의 P2P를 호출하지 않고 로컬만 합산한다."""
        # DirectorySource는 실제 디스크 용량(shutil.disk_usage)을 쓰므로, 호출 간
        # 시스템 디스크 변동으로 값이 흔들린다. 테스트 결정성을 위해 고정한다.
        import collections

        import stardustlib.storage_source as ss

        usage = collections.namedtuple("usage", "total used free")
        monkeypatch.setattr(
            ss.shutil, "disk_usage", lambda _p: usage(1000, 400, 600)
        )
        local_total = storage_pool.get_total_space()
        storage_pool.add_source(_FakeRemoteSource("remote-offline"))
        # 원격이 OSError를 던져도 예외 없이 동일한 로컬 합계를 반환해야 한다
        assert storage_pool.get_total_space() == local_total

    def test_available_space_ignores_remote(self, storage_pool, monkeypatch):
        """get_available_space는 원격 소스를 제외한다."""
        import collections

        import stardustlib.storage_source as ss

        usage = collections.namedtuple("usage", "total used free")
        monkeypatch.setattr(
            ss.shutil, "disk_usage", lambda _p: usage(1000, 400, 600)
        )
        local_avail = storage_pool.get_available_space()
        storage_pool.add_source(_FakeRemoteSource("remote-offline"))
        assert storage_pool.get_available_space() == local_avail

    def test_select_source_ignores_remote(self, storage_pool):
        """select_source는 원격 소스를 쓰기 대상으로 선택하지 않는다."""
        storage_pool.add_source(_FakeRemoteSource("remote-offline"))
        selected = storage_pool.select_source(1024)
        assert selected.is_remote is False
