"""StorageSource ABC 및 DirectorySource 단위 테스트."""

import os
import shutil
import sys
import tempfile

import pytest

from stardustlib.storage_source import DirectorySource, StorageSource

# 확실히 존재하지 않는 경로.
# Windows에서 매핑되지 않은 드라이브 문자(Z: 등)를 조회하면 네트워크 드라이브
# 재연결을 시도해 호출 한 번에 20초가 걸린다. 시스템 드라이브 하위의 없는 경로는
# 즉시 실패한다(DirectorySource.initialize는 os.path.isdir만 보고 디렉토리를
# 만들지 않으므로 결과는 같다).
_NONEXISTENT_PATH = (
    os.path.join(
        os.environ.get("SystemDrive", "C:") + os.sep,
        "__nonexistent_stardustfs_test_xyz__",
        "sub",
    )
    if sys.platform == "win32"
    else "/nonexistent_stardustfs_test_xyz"
)


class TestStorageSourceABC:
    """StorageSource가 ABC로서 올바르게 동작하는지 검증."""

    def test_cannot_instantiate_abc(self) -> None:
        """StorageSource를 직접 인스턴스화할 수 없다."""
        with pytest.raises(TypeError):
            StorageSource("test", "/tmp")  # type: ignore[abstract]


class TestDirectorySource:
    """DirectorySource 기본 동작 검증."""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.source = DirectorySource("dir-test", self.tmpdir)
        self.source.initialize()

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_initialize_valid_directory(self) -> None:
        """유효한 디렉토리로 초기화하면 활성 상태가 된다."""
        assert self.source.is_active is True
        assert self.source.source_id == "dir-test"

    def test_initialize_nonexistent_directory(self) -> None:
        """존재하지 않는 경로로 초기화하면 비활성 상태가 된다."""
        source = DirectorySource("bad", _NONEXISTENT_PATH)
        source.initialize()
        assert source.is_active is False

    def test_write_and_read(self) -> None:
        """파일 쓰기 후 읽기가 동일한 데이터를 반환한다."""
        data = b"hello stardustfs"
        self.source.write("test.bin", data)
        result = self.source.read("test.bin")
        assert result == data

    def test_write_creates_parent_directories(self) -> None:
        """쓰기 시 상위 디렉토리가 자동 생성된다."""
        self.source.write("sub/dir/file.bin", b"nested")
        assert self.source.exists("sub/dir/file.bin")

    def test_delete(self) -> None:
        """파일 삭제 후 exists가 False를 반환한다."""
        self.source.write("to_delete.bin", b"data")
        assert self.source.exists("to_delete.bin")
        self.source.delete("to_delete.bin")
        assert self.source.exists("to_delete.bin") is False

    def test_delete_nonexistent_raises(self) -> None:
        """존재하지 않는 파일 삭제 시 FileNotFoundError가 발생한다."""
        with pytest.raises(FileNotFoundError):
            self.source.delete("no_such_file.bin")
        # FileNotFoundError는 소스를 비활성화하지 않는다
        assert self.source.is_active is True

    def test_exists_nonexistent(self) -> None:
        """존재하지 않는 파일에 대해 False를 반환한다."""
        assert self.source.exists("no_such_file.bin") is False

    def test_mkdir_and_list_dir(self) -> None:
        """디렉토리 생성 후 list_dir로 확인 가능하다."""
        self.source.mkdir("mydir")
        self.source.write("mydir/a.txt", b"a")
        self.source.write("mydir/b.txt", b"b")
        entries = self.source.list_dir("mydir")
        assert sorted(entries) == ["a.txt", "b.txt"]

    def test_rmdir(self) -> None:
        """rmdir로 디렉토리와 내용이 삭제된다."""
        self.source.mkdir("removeme")
        self.source.write("removeme/file.bin", b"x")
        self.source.rmdir("removeme")
        assert self.source.exists("removeme") is False

    def test_get_available_space(self) -> None:
        """사용 가능 공간이 양수를 반환한다."""
        space = self.source.get_available_space()
        assert space > 0

    def test_get_total_space(self) -> None:
        """전체 공간이 사용 가능 공간 이상이다."""
        total = self.source.get_total_space()
        available = self.source.get_available_space()
        assert total >= available

    def test_inactive_source_raises_on_operations(self) -> None:
        """비활성 소스에서 작업 시 OSError가 발생한다."""
        source = DirectorySource("inactive", _NONEXISTENT_PATH)
        source.initialize()
        assert source.is_active is False
        with pytest.raises(OSError):
            source.read("file.bin")
        with pytest.raises(OSError):
            source.write("file.bin", b"data")
        with pytest.raises(OSError):
            source.delete("file.bin")
        with pytest.raises(OSError):
            source.exists("file.bin")
        with pytest.raises(OSError):
            source.get_available_space()

    def test_read_nonexistent_file_does_not_deactivate(self) -> None:
        """존재하지 않는 파일 읽기는 소스를 비활성화하지 않는다."""
        with pytest.raises(FileNotFoundError):
            self.source.read("nonexistent_file.bin")
        assert self.source.is_active is True
