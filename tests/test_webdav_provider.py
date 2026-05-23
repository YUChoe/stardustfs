"""StardustDAVProvider 단위 테스트."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.storage_source import DirectorySource
from stardustlib.webdav_provider import (
    StardustDAVProvider,
    StardustDirectoryResource,
    StardustFileResource,
)


@pytest.fixture
def setup_provider(tmp_path):
    """테스트용 프로바이더를 설정한다."""
    # 스토리지 디렉토리 생성
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    # 메타데이터 DB
    db_path = str(tmp_path / "metadata.db")

    # 암호화 키
    key = os.urandom(32)

    # 컴포넌트 초기화
    encryption_engine = EncryptionEngine(key)
    metadata_store = MetadataStore(db_path, key)
    metadata_store.initialize()

    source = DirectorySource("src-001", str(storage_dir))
    source.initialize()

    jbod_manager = JBODManager(
        sources=[source],
        metadata_store=metadata_store,
        encryption_engine=encryption_engine,
    )

    provider = StardustDAVProvider(jbod_manager, encryption_engine)

    # 가짜 environ (wsgidav가 요구하는 키 포함)
    environ = {
        "REQUEST_METHOD": "GET",
        "wsgidav.provider": provider,
    }

    return provider, jbod_manager, environ


class TestStardustDAVProvider:
    """StardustDAVProvider 테스트."""

    def test_root_returns_directory_resource(self, setup_provider):
        """루트 경로는 항상 디렉토리 리소스를 반환한다."""
        provider, _, environ = setup_provider
        resource = provider.get_resource_inst("/", environ)
        assert resource is not None
        assert isinstance(resource, StardustDirectoryResource)

    def test_nonexistent_path_returns_none(self, setup_provider):
        """존재하지 않는 경로는 None을 반환한다."""
        provider, _, environ = setup_provider
        resource = provider.get_resource_inst("/nonexistent.txt", environ)
        assert resource is None

    def test_file_resource_returned_for_existing_file(self, setup_provider):
        """존재하는 파일에 대해 StardustFileResource를 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/test.txt", b"hello world")

        resource = provider.get_resource_inst("/test.txt", environ)
        assert resource is not None
        assert isinstance(resource, StardustFileResource)

    def test_directory_resource_returned_for_existing_dir(self, setup_provider):
        """존재하는 디렉토리에 대해 StardustDirectoryResource를 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.create_directory("/docs")

        resource = provider.get_resource_inst("/docs", environ)
        assert resource is not None
        assert isinstance(resource, StardustDirectoryResource)


class TestStardustFileResource:
    """StardustFileResource 테스트."""

    def test_get_content_length(self, setup_provider):
        """파일 크기를 올바르게 반환한다."""
        provider, jbod, environ = setup_provider
        data = b"hello world"
        jbod.write_file("/test.txt", data)

        resource = provider.get_resource_inst("/test.txt", environ)
        assert resource.get_content_length() == len(data)

    def test_get_content(self, setup_provider):
        """파일 내용을 올바르게 반환한다."""
        provider, jbod, environ = setup_provider
        data = b"hello world"
        jbod.write_file("/test.txt", data)

        resource = provider.get_resource_inst("/test.txt", environ)
        content = resource.get_content()
        assert content.read() == data

    def test_get_content_type(self, setup_provider):
        """MIME 타입을 올바르게 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/test.txt", b"hello")

        resource = provider.get_resource_inst("/test.txt", environ)
        assert "text/plain" in resource.get_content_type()

    def test_get_creation_date(self, setup_provider):
        """생성 시간을 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/test.txt", b"hello")

        resource = provider.get_resource_inst("/test.txt", environ)
        assert resource.get_creation_date() > 0

    def test_get_last_modified(self, setup_provider):
        """수정 시간을 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/test.txt", b"hello")

        resource = provider.get_resource_inst("/test.txt", environ)
        assert resource.get_last_modified() > 0

    def test_delete(self, setup_provider):
        """파일을 삭제한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/test.txt", b"hello")

        resource = provider.get_resource_inst("/test.txt", environ)
        resource.delete()

        assert provider.get_resource_inst("/test.txt", environ) is None

    def test_begin_write(self, setup_provider):
        """begin_write로 파일을 덮어쓴다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/test.txt", b"old data")

        resource = provider.get_resource_inst("/test.txt", environ)
        stream = resource.begin_write(content_type=None)
        stream.write(b"new data")
        stream.close()

        # 새 데이터 확인
        assert jbod.read_file("/test.txt") == b"new data"

    def test_copy_move_single_copy(self, setup_provider):
        """파일을 복사한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/src.txt", b"data")

        resource = provider.get_resource_inst("/src.txt", environ)
        resource.copy_move_single("/dst.txt", is_move=False)

        assert jbod.read_file("/dst.txt") == b"data"
        assert jbod.read_file("/src.txt") == b"data"

    def test_copy_move_single_move(self, setup_provider):
        """파일을 이동한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/src.txt", b"data")

        resource = provider.get_resource_inst("/src.txt", environ)
        resource.copy_move_single("/dst.txt", is_move=True)

        assert jbod.read_file("/dst.txt") == b"data"
        assert not jbod.file_exists("/src.txt")


class TestStardustDirectoryResource:
    """StardustDirectoryResource 테스트."""

    def test_get_member_names_empty(self, setup_provider):
        """빈 디렉토리의 멤버 이름 목록은 빈 리스트이다."""
        provider, jbod, environ = setup_provider
        jbod.create_directory("/empty")

        resource = provider.get_resource_inst("/empty", environ)
        assert resource.get_member_names() == []

    def test_get_member_names_with_files(self, setup_provider):
        """파일이 있는 디렉토리의 멤버 이름을 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/docs/a.txt", b"aaa")
        jbod.write_file("/docs/b.txt", b"bbb")

        resource = provider.get_resource_inst("/docs", environ)
        names = resource.get_member_names()
        assert sorted(names) == ["a.txt", "b.txt"]

    def test_get_member_file(self, setup_provider):
        """get_member로 파일 리소스를 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.write_file("/docs/a.txt", b"aaa")

        resource = provider.get_resource_inst("/docs", environ)
        member = resource.get_member("a.txt")
        assert isinstance(member, StardustFileResource)

    def test_get_member_nonexistent(self, setup_provider):
        """존재하지 않는 멤버는 None을 반환한다."""
        provider, jbod, environ = setup_provider
        jbod.create_directory("/docs")

        resource = provider.get_resource_inst("/docs", environ)
        member = resource.get_member("nonexistent.txt")
        assert member is None

    def test_create_empty_resource(self, setup_provider):
        """빈 파일 리소스를 생성한다."""
        provider, jbod, environ = setup_provider
        jbod.create_directory("/docs")

        resource = provider.get_resource_inst("/docs", environ)
        new_file = resource.create_empty_resource("new.txt")
        assert isinstance(new_file, StardustFileResource)
        assert jbod.file_exists("/docs/new.txt")

    def test_create_collection(self, setup_provider):
        """하위 디렉토리를 생성한다."""
        provider, jbod, environ = setup_provider
        jbod.create_directory("/docs")

        resource = provider.get_resource_inst("/docs", environ)
        new_dir = resource.create_collection("subdir")
        assert isinstance(new_dir, StardustDirectoryResource)

    def test_delete_directory(self, setup_provider):
        """디렉토리를 삭제한다."""
        provider, jbod, environ = setup_provider
        jbod.create_directory("/docs")
        jbod.write_file("/docs/a.txt", b"aaa")

        resource = provider.get_resource_inst("/docs", environ)
        resource.delete()

        assert provider.get_resource_inst("/docs", environ) is None
        assert not jbod.file_exists("/docs/a.txt")



class TestErrorMapping:
    """에러를 HTTP 상태 코드로 변환하는 테스트 (Req 8.6)."""

    def test_file_not_found_returns_404(self, setup_provider):
        """FileNotFoundError → HTTP 404."""
        from wsgidav.dav_error import DAVError

        provider, jbod, environ = setup_provider
        # 존재하지 않는 파일에 대해 get_content 호출
        jbod.write_file("/temp.txt", b"data")
        resource = provider.get_resource_inst("/temp.txt", environ)
        jbod.delete_file("/temp.txt")

        with pytest.raises(DAVError) as exc_info:
            resource.get_content()
        assert exc_info.value.value == 404

    def test_file_not_found_delete_returns_404(self, setup_provider):
        """삭제 시 FileNotFoundError → HTTP 404."""
        from wsgidav.dav_error import DAVError

        provider, jbod, environ = setup_provider
        jbod.write_file("/temp.txt", b"data")
        resource = provider.get_resource_inst("/temp.txt", environ)
        jbod.delete_file("/temp.txt")

        with pytest.raises(DAVError) as exc_info:
            resource.delete()
        assert exc_info.value.value == 404

    def test_insufficient_storage_returns_507(self, setup_provider):
        """InsufficientStorageError → HTTP 507."""
        from unittest.mock import patch
        from wsgidav.dav_error import DAVError

        from stardustlib.exceptions import InsufficientStorageError

        provider, jbod, environ = setup_provider
        jbod.write_file("/src.txt", b"data")
        resource = provider.get_resource_inst("/src.txt", environ)

        with patch.object(
            jbod, "copy_file", side_effect=InsufficientStorageError("공간 부족")
        ):
            with pytest.raises(DAVError) as exc_info:
                resource.copy_move_single("/dst.txt", is_move=False)
            assert exc_info.value.value == 507
