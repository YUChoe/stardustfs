"""SyncClient 단위 테스트.

초기 동기화, 주기적 업로드, 오프라인→온라인 복구,
key 업로드/다운로드를 검증한다.
"""

import asyncio
import os
import tempfile
import time

import httpx
import pytest
import pytest_asyncio

from stardustlib.auth_client import AuthClient
from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.exceptions import KeyNotFoundError, SyncError
from stardustlib.metadata_store import MetadataStore
from stardustlib.sync_client import SyncClient


@pytest.fixture
def tmp_db_path(tmp_path):
    """임시 DB 경로."""
    return str(tmp_path / "metadata.db")


@pytest.fixture
def metadata_store(tmp_db_path):
    """초기화된 MetadataStore."""
    store = MetadataStore(tmp_db_path, b"\x00" * 32)
    store.initialize()
    return store


@pytest.fixture
def conflict_resolver(metadata_store):
    """ConflictResolver 인스턴스."""
    return ConflictResolver(metadata_store, "test-device")


@pytest_asyncio.fixture
async def auth_client(httpx_mock):
    """Mock된 AuthClient."""
    client = AuthClient("http://test-server", timeout=5.0)
    # 토큰을 직접 설정
    client._access_token = "test-token"
    client._refresh_token_value = "test-refresh"
    client._token_expires_at = time.time() + 3600
    client._user_id = "user-123"
    client._offline = False
    return client


@pytest_asyncio.fixture
async def sync_client(auth_client, metadata_store, conflict_resolver):
    """SyncClient 인스턴스."""
    sc = SyncClient(
        auth_client=auth_client,
        server_url="http://test-server",
        metadata_store=metadata_store,
        conflict_resolver=conflict_resolver,
        interval_seconds=1,
    )
    yield sc
    await sc.stop()


class TestInitialSync:
    """initial_sync() 테스트."""

    @pytest.mark.asyncio
    async def test_initial_sync_no_server_metadata(
        self, sync_client, httpx_mock
    ):
        """서버에 metadata가 없으면 (404) 로컬 DB를 강제 업로드한다."""
        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="GET",
            status_code=404,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="PUT",
            json={"version": 1},
            status_code=200,
        )
        await sync_client.initial_sync()
        # 404 → force_upload PUT 수행, 에러 없이 완료

    @pytest.mark.asyncio
    async def test_initial_sync_network_error(
        self, sync_client, httpx_mock
    ):
        """네트워크 오류 시 로컬 DB 사용."""
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="http://test-server/sync/metadata",
        )
        await sync_client.initial_sync()
        # 에러 없이 완료 (오프라인 모드)

    @pytest.mark.asyncio
    async def test_initial_sync_server_error(
        self, sync_client, httpx_mock
    ):
        """서버 5xx 응답 시 로컬 DB 사용."""
        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="GET",
            status_code=500,
        )
        await sync_client.initial_sync()

    @pytest.mark.asyncio
    async def test_initial_sync_merges_new_records(
        self, sync_client, metadata_store, httpx_mock, tmp_path
    ):
        """서버 DB에 새 레코드가 있으면 로컬에 삽입."""
        # 서버 DB 생성
        server_db_path = str(tmp_path / "server.db")
        server_store = MetadataStore(server_db_path, b"\x00" * 32)
        server_store.initialize()
        server_store.insert(
            "/docs/readme.txt", "vol1", "readme.txt",
            100, time.time(), time.time(),
        )
        # version을 2로 설정
        conn = server_store._get_conn()
        conn.execute(
            "UPDATE files SET version = 2, sync_status = 'synced' "
            "WHERE virtual_path = '/docs/readme.txt'"
        )
        conn.commit()
        server_store.close()

        with open(server_db_path, "rb") as f:
            server_blob = f.read()

        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="GET",
            content=server_blob,
        )

        await sync_client.initial_sync()

        # 로컬에 레코드가 삽입되었는지 확인
        rec = metadata_store.lookup("/docs/readme.txt")
        assert rec is not None
        assert rec.version == 2
        assert rec.sync_status == "synced"

    @pytest.mark.asyncio
    async def test_initial_sync_server_version_higher(
        self, sync_client, metadata_store, httpx_mock, tmp_path
    ):
        """서버 version이 높으면 서버 메타데이터로 갱신."""
        # 로컬에 version=1 레코드 삽입
        metadata_store.insert(
            "/docs/file.txt", "vol1", "file.txt",
            50, time.time(), time.time(),
        )
        # sync_status를 synced로 설정
        metadata_store.set_sync_status("/docs/file.txt", "synced")
        conn = metadata_store._get_conn()
        conn.execute(
            "UPDATE files SET version = 1 WHERE virtual_path = '/docs/file.txt'"
        )
        conn.commit()

        # 서버 DB: version=3
        server_db_path = str(tmp_path / "server.db")
        server_store = MetadataStore(server_db_path, b"\x00" * 32)
        server_store.initialize()
        server_store.insert(
            "/docs/file.txt", "vol1", "file.txt",
            200, time.time(), time.time(),
        )
        conn2 = server_store._get_conn()
        conn2.execute(
            "UPDATE files SET version = 3, sync_status = 'synced' "
            "WHERE virtual_path = '/docs/file.txt'"
        )
        conn2.commit()
        server_store.close()

        with open(server_db_path, "rb") as f:
            server_blob = f.read()

        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="GET",
            content=server_blob,
        )

        await sync_client.initial_sync()

        rec = metadata_store.lookup("/docs/file.txt")
        assert rec is not None
        assert rec.version == 3
        assert rec.file_size == 200

    @pytest.mark.asyncio
    async def test_initial_sync_local_version_higher(
        self, sync_client, metadata_store, httpx_mock, tmp_path
    ):
        """로컬 version이 높으면 변경 없음 (다음 업로드 시 반영)."""
        # 로컬: version=5
        metadata_store.insert(
            "/docs/local.txt", "vol1", "local.txt",
            300, time.time(), time.time(),
        )
        conn = metadata_store._get_conn()
        conn.execute(
            "UPDATE files SET version = 5, sync_status = 'pending' "
            "WHERE virtual_path = '/docs/local.txt'"
        )
        conn.commit()

        # 서버: version=2
        server_db_path = str(tmp_path / "server.db")
        server_store = MetadataStore(server_db_path, b"\x00" * 32)
        server_store.initialize()
        server_store.insert(
            "/docs/local.txt", "vol1", "local.txt",
            100, time.time(), time.time(),
        )
        conn2 = server_store._get_conn()
        conn2.execute(
            "UPDATE files SET version = 2, sync_status = 'synced' "
            "WHERE virtual_path = '/docs/local.txt'"
        )
        conn2.commit()
        server_store.close()

        with open(server_db_path, "rb") as f:
            server_blob = f.read()

        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="GET",
            content=server_blob,
        )

        await sync_client.initial_sync()

        # 로컬 레코드 변경 없음
        rec = metadata_store.lookup("/docs/local.txt")
        assert rec is not None
        assert rec.version == 5
        assert rec.file_size == 300


class TestUploadMetadata:
    """upload_metadata() 테스트."""

    @pytest.mark.asyncio
    async def test_upload_success(
        self, sync_client, metadata_store, httpx_mock
    ):
        """업로드 성공 시 pending → synced."""
        metadata_store.insert(
            "/test.txt", "vol1", "test.txt",
            10, time.time(), time.time(),
        )
        # insert는 pending으로 설정됨

        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="PUT",
            status_code=200,
        )

        await sync_client.upload_metadata()

        rec = metadata_store.lookup("/test.txt")
        assert rec is not None
        assert rec.sync_status == "synced"

    @pytest.mark.asyncio
    async def test_upload_network_failure(
        self, sync_client, metadata_store, httpx_mock
    ):
        """업로드 실패 시 pending 유지."""
        metadata_store.insert(
            "/test.txt", "vol1", "test.txt",
            10, time.time(), time.time(),
        )

        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="http://test-server/sync/metadata",
        )

        await sync_client.upload_metadata()

        rec = metadata_store.lookup("/test.txt")
        assert rec is not None
        assert rec.sync_status == "pending"

    @pytest.mark.asyncio
    async def test_upload_consecutive_failures_logged(
        self, sync_client, metadata_store, httpx_mock
    ):
        """3회 연속 실패 시 로그 기록 (에러 없이 계속)."""
        metadata_store.insert(
            "/test.txt", "vol1", "test.txt",
            10, time.time(), time.time(),
        )

        for _ in range(3):
            httpx_mock.add_response(
                url="http://test-server/sync/metadata",
                method="PUT",
                status_code=500,
            )
            await sync_client.upload_metadata()

        assert sync_client._consecutive_failures == 3


class TestUploadKey:
    """upload_key() 테스트."""

    @pytest.mark.asyncio
    async def test_upload_key_success(self, sync_client, httpx_mock):
        """key 업로드 성공."""
        httpx_mock.add_response(
            url="http://test-server/sync/key",
            method="PUT",
            status_code=200,
        )
        await sync_client.upload_key(b"encrypted-blob")

    @pytest.mark.asyncio
    async def test_upload_key_retries_on_failure(
        self, sync_client, httpx_mock
    ):
        """3회 재시도 후 실패 시 SyncError."""
        for _ in range(3):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"),
                url="http://test-server/sync/key",
            )

        with pytest.raises(SyncError, match="retries"):
            await sync_client.upload_key(b"encrypted-blob")

    @pytest.mark.asyncio
    async def test_upload_key_succeeds_on_retry(
        self, sync_client, httpx_mock
    ):
        """2회 실패 후 3회째 성공."""
        httpx_mock.add_exception(
            httpx.ConnectError("fail"),
            url="http://test-server/sync/key",
        )
        httpx_mock.add_exception(
            httpx.ConnectError("fail"),
            url="http://test-server/sync/key",
        )
        httpx_mock.add_response(
            url="http://test-server/sync/key",
            method="PUT",
            status_code=200,
        )
        await sync_client.upload_key(b"encrypted-blob")


class TestDownloadKey:
    """download_key() 테스트."""

    @pytest.mark.asyncio
    async def test_download_key_success(self, sync_client, httpx_mock):
        """key 다운로드 성공."""
        httpx_mock.add_response(
            url="http://test-server/sync/key",
            method="GET",
            content=b"encrypted-key-blob",
        )
        result = await sync_client.download_key()
        assert result == b"encrypted-key-blob"

    @pytest.mark.asyncio
    async def test_download_key_not_found(self, sync_client, httpx_mock):
        """서버에 key 없으면 KeyNotFoundError."""
        httpx_mock.add_response(
            url="http://test-server/sync/key",
            method="GET",
            status_code=404,
        )
        with pytest.raises(KeyNotFoundError):
            await sync_client.download_key()

    @pytest.mark.asyncio
    async def test_download_key_retries_on_failure(
        self, sync_client, httpx_mock
    ):
        """3회 재시도 후 실패 시 SyncError."""
        for _ in range(3):
            httpx_mock.add_exception(
                httpx.TimeoutException("timeout"),
                url="http://test-server/sync/key",
            )

        with pytest.raises(SyncError, match="retries"):
            await sync_client.download_key()


class TestPeriodicSync:
    """start_periodic_sync() / stop() 테스트."""

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True, assert_all_responses_were_requested=False)
    async def test_periodic_sync_starts_and_stops(
        self, auth_client, metadata_store, conflict_resolver, httpx_mock
    ):
        """주기적 동기화 시작/중지."""
        sc = SyncClient(
            auth_client=auth_client,
            server_url="http://test-server",
            metadata_store=metadata_store,
            conflict_resolver=conflict_resolver,
            interval_seconds=1,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/status",
            method="GET",
            json={"version": 0},
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="PUT",
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/wait?known_version=0",
            method="GET",
            json={"version": 0, "changed": False},
        )

        await sc.start_periodic_sync()
        assert sc._running is True
        assert sc._sync_task is not None

        # 잠시 대기 후 중지
        await asyncio.sleep(0.1)
        await sc.stop()
        assert sc._running is False

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True, assert_all_responses_were_requested=False)
    async def test_periodic_sync_idempotent_start(
        self, auth_client, metadata_store, conflict_resolver, httpx_mock
    ):
        """이미 실행 중이면 중복 시작 안 함."""
        sc = SyncClient(
            auth_client=auth_client,
            server_url="http://test-server",
            metadata_store=metadata_store,
            conflict_resolver=conflict_resolver,
            interval_seconds=1,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/status",
            method="GET",
            json={"version": 0},
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="PUT",
            status_code=200,
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/wait?known_version=0",
            method="GET",
            json={"version": 0, "changed": False},
        )

        await sc.start_periodic_sync()
        task1 = sc._sync_task
        await sc.start_periodic_sync()
        task2 = sc._sync_task
        assert task1 is task2
        await sc.stop()


class TestVersionWait:
    """버전 롱폴링(_wait_for_version) 테스트."""

    @pytest.mark.asyncio
    async def test_wait_changed_true(self, sync_client, httpx_mock):
        """changed=true 응답이면 True 반환."""
        sync_client._last_synced_version = 5
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/wait?known_version=5",
            method="GET",
            json={"version": 6, "changed": True},
        )
        async with httpx.AsyncClient() as client:
            result = await sync_client._wait_for_version(client)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_changed_false(self, sync_client, httpx_mock):
        """changed=false(타임아웃) 응답이면 False 반환."""
        sync_client._last_synced_version = 5
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/wait?known_version=5",
            method="GET",
            json={"version": 5, "changed": False},
        )
        async with httpx.AsyncClient() as client:
            result = await sync_client._wait_for_version(client)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_unsupported_404(self, sync_client, httpx_mock):
        """404면 _WaitUnsupported를 발생시킨다(구버전 서버)."""
        from stardustlib.sync_client import _WaitUnsupported

        sync_client._last_synced_version = 0
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/wait?known_version=0",
            method="GET",
            status_code=404,
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(_WaitUnsupported):
                await sync_client._wait_for_version(client)

    @pytest.mark.asyncio
    async def test_wait_server_error_returns_false(
        self, sync_client, httpx_mock
    ):
        """5xx면 False(다음 사이클 재시도)."""
        sync_client._last_synced_version = 2
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/wait?known_version=2",
            method="GET",
            status_code=500,
        )
        async with httpx.AsyncClient() as client:
            result = await sync_client._wait_for_version(client)
        assert result is False
