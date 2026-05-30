"""WebDAV 오프라인 Placeholder 단위 테스트.

Requirements: 15.1-15.10
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import FileInfo
from stardustlib.remote_source import RemoteSource
from stardustlib.storage_source import DirectorySource
from stardustlib.webdav_provider import (
    HTTP_SERVICE_UNAVAILABLE,
    OFFLINE_SUFFIX,
    OfflinePlaceholderResource,
    StardustDAVProvider,
    StardustDirectoryResource,
    StardustFileResource,
)


@pytest.fixture
def setup_with_remote(tmp_path):
    """로컬 소스 + 오프라인 RemoteSource를 포함한 테스트 환경."""
    # 로컬 스토리지
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    db_path = str(tmp_path / "metadata.db")
    key = os.urandom(32)

    encryption_engine = EncryptionEngine(key)
    metadata_store = MetadataStore(db_path, key)
    metadata_store.initialize()

    local_source = DirectorySource("local-001", str(storage_dir))
    local_source.initialize()

    # Mock RemoteSource (비활성 상태)
    remote_source = MagicMock(spec=RemoteSource)
    remote_source.source_id = "remote-001"
    remote_source.is_active = False
    remote_source._source_id = "remote-001"
    remote_source._active = False
    # isinstance 체크를 위해 __class__ 설정
    remote_source.__class__ = RemoteSource

    jbod_manager = JBODManager(
        sources=[local_source, remote_source],
        metadata_store=metadata_store,
        encryption_engine=encryption_engine,
    )

    provider = StardustDAVProvider(jbod_manager, encryption_engine)

    environ = {
        "REQUEST_METHOD": "GET",
        "wsgidav.provider": provider,
    }

    return provider, jbod_manager, environ, remote_source, metadata_store


def _insert_remote_file(metadata_store, virtual_path, source_id="remote-001"):
    """RemoteSource에 속하는 파일 메타데이터를 직접 삽입한다."""
    now = time.time()
    metadata_store.insert(
        virtual_path=virtual_path,
        source_id=source_id,
        physical_path=f"fake/{virtual_path.lstrip('/')}",
        file_size=1024,
        created_at=now - 100,
        modified_at=now - 50,
    )


class TestOfflinePlaceholderDisplay:
    """Req 15.1, 15.2: 오프라인 파일이 .offline 확장자로 표시된다."""

    def test_offline_file_shown_with_offline_suffix(self, setup_with_remote):
        """오프라인 RemoteSource 파일이 목록에서 .offline 확장자로 표시된다."""
        provider, jbod, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/docs/report.pdf")

        resource = provider.get_resource_inst("/docs", environ)
        assert resource is not None
        names = resource.get_member_names()
        assert "report.pdf.offline" in names
        assert "report.pdf" not in names

    def test_online_file_shown_normally(self, setup_with_remote):
        """온라인 로컬 파일은 정상 파일명으로 표시된다."""
        provider, jbod, environ, _, _ = setup_with_remote
        jbod.write_file("/docs/local.txt", b"hello")

        resource = provider.get_resource_inst("/docs", environ)
        names = resource.get_member_names()
        assert "local.txt" in names
        assert "local.txt.offline" not in names


class TestOfflinePlaceholderProperties:
    """Req 15.3, 15.4, 15.10: placeholder 속성 확인."""

    def test_content_length_is_zero(self, setup_with_remote):
        """오프라인 placeholder의 content_length는 0이다."""
        provider, _, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/file.dat")

        resource = provider.get_resource_inst("/file.dat.offline", environ)
        assert resource is not None
        assert isinstance(resource, OfflinePlaceholderResource)
        assert resource.get_content_length() == 0

    def test_last_modified_is_original(self, setup_with_remote):
        """오프라인 placeholder의 last_modified는 원본 수정 시각이다."""
        provider, _, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/file.dat")

        resource = provider.get_resource_inst("/file.dat.offline", environ)
        assert resource is not None
        # 원본 modified_at은 time.time() - 50 근처
        assert resource.get_last_modified() > 0

    def test_custom_properties(self, setup_with_remote):
        """커스텀 속성 stardust:availability, stardust:original-name 확인."""
        provider, _, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/docs/report.pdf")

        resource = provider.get_resource_inst(
            "/docs/report.pdf.offline", environ
        )
        assert resource is not None
        assert resource.get_property_value("stardust:availability") == "offline"
        assert (
            resource.get_property_value("stardust:original-name")
            == "report.pdf"
        )


class TestOfflinePlaceholder503:
    """Req 15.5, 15.6, 15.7: GET/PUT/DELETE 시 503 반환."""

    def test_get_returns_503(self, setup_with_remote):
        """GET 요청 시 503 Service Unavailable을 반환한다."""
        from wsgidav.dav_error import DAVError

        provider, _, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/file.dat")

        resource = provider.get_resource_inst("/file.dat.offline", environ)
        with pytest.raises(DAVError) as exc_info:
            resource.get_content()
        assert exc_info.value.value == HTTP_SERVICE_UNAVAILABLE

    def test_put_returns_503(self, setup_with_remote):
        """PUT 요청 시 503 Service Unavailable을 반환한다."""
        from wsgidav.dav_error import DAVError

        provider, _, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/file.dat")

        resource = provider.get_resource_inst("/file.dat.offline", environ)
        with pytest.raises(DAVError) as exc_info:
            resource.begin_write(content_type=None)
        assert exc_info.value.value == HTTP_SERVICE_UNAVAILABLE

    def test_delete_returns_503(self, setup_with_remote):
        """DELETE 요청 시 503 Service Unavailable을 반환한다."""
        from wsgidav.dav_error import DAVError

        provider, _, environ, _, ms = setup_with_remote
        _insert_remote_file(ms, "/file.dat")

        resource = provider.get_resource_inst("/file.dat.offline", environ)
        with pytest.raises(DAVError) as exc_info:
            resource.delete()
        assert exc_info.value.value == HTTP_SERVICE_UNAVAILABLE


class TestOnlineRecovery:
    """Req 15.8: 온라인 복구 시 .offline 제거, 실제 속성 반환."""

    def test_online_recovery_shows_normal_file(self, setup_with_remote):
        """RemoteSource가 다시 활성화되면 정상 파일로 표시된다."""
        provider, jbod, environ, remote_source, ms = setup_with_remote
        _insert_remote_file(ms, "/file.dat")

        # 오프라인 상태에서는 placeholder
        resource = provider.get_resource_inst("/file.dat.offline", environ)
        assert isinstance(resource, OfflinePlaceholderResource)

        # RemoteSource를 온라인으로 전환
        remote_source.is_active = True

        # 캐시 무효화
        provider._cache_invalidate("/file.dat")

        # 이제 원본 경로로 접근하면 일반 파일 리소스
        resource = provider.get_resource_inst("/file.dat", environ)
        assert resource is not None
        assert isinstance(resource, StardustFileResource)

    def test_online_recovery_removes_offline_from_listing(
        self, setup_with_remote
    ):
        """온라인 복구 후 디렉토리 목록에서 .offline이 제거된다."""
        provider, jbod, environ, remote_source, ms = setup_with_remote
        _insert_remote_file(ms, "/docs/report.pdf")

        # 오프라인 상태
        resource = provider.get_resource_inst("/docs", environ)
        names = resource.get_member_names()
        assert "report.pdf.offline" in names

        # 온라인 전환
        remote_source.is_active = True

        resource = provider.get_resource_inst("/docs", environ)
        names = resource.get_member_names()
        assert "report.pdf" in names
        assert "report.pdf.offline" not in names


class TestAllSourcesOnline:
    """Req 15.9: 모든 소스가 온라인이면 정상 표시."""

    def test_all_online_no_offline_suffix(self, setup_with_remote):
        """모든 RemoteSource가 온라인이면 .offline 없이 정상 표시."""
        provider, jbod, environ, remote_source, ms = setup_with_remote
        # RemoteSource를 온라인으로 설정
        remote_source.is_active = True
        _insert_remote_file(ms, "/docs/report.pdf")

        resource = provider.get_resource_inst("/docs", environ)
        names = resource.get_member_names()
        assert "report.pdf" in names
        assert "report.pdf.offline" not in names
