"""SyncClient 레코드(파셜/증분) 동기화 경로 테스트."""

import base64
import time

import pytest
import pytest_asyncio

from stardustlib.auth_client import AuthClient
from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.metadata_records import (
    derive_record_subkey,
    pad_plaintext,
    record_id_for,
    serialize_metadata,
)
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import FileMetadata
from stardustlib.sync_client import SyncClient

_KEY = b"\x00" * 32


def _insert_row(store, fm):
    """files 테이블에 FileMetadata를 원하는 상태 그대로 삽입한다(테스트 셋업용)."""
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO files "
        "(virtual_path, source_id, physical_path, file_size, created_at, "
        "modified_at, version, device_id, sync_status, deleted, evicted, "
        "replication_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (fm.virtual_path, fm.source_id, fm.physical_path, fm.file_size,
         fm.created_at, fm.modified_at, fm.version, fm.device_id,
         fm.sync_status, 1 if fm.deleted else 0, fm.replication_status),
    )
    conn.commit()


@pytest.fixture
def metadata_store(tmp_path):
    store = MetadataStore(str(tmp_path / "metadata.db"), _KEY)
    store.initialize()
    return store


@pytest_asyncio.fixture
async def auth_client():
    client = AuthClient("http://test-server", timeout=5.0)
    client._access_token = "test-token"
    client._refresh_token_value = "test-refresh"
    client._token_expires_at = time.time() + 3600
    client._user_id = "user-123"
    client._offline = False
    return client


@pytest_asyncio.fixture
async def sync_client(auth_client, metadata_store):
    resolver = ConflictResolver(metadata_store, "test-device")
    sc = SyncClient(
        auth_client=auth_client,
        server_url="http://test-server",
        metadata_store=metadata_store,
        conflict_resolver=resolver,
        interval_seconds=1,
        encryption_key=_KEY,
    )
    yield sc
    await sc.stop()


def _encode_server_record(sc, fm, record_version):
    """서버가 돌려줄 레코드(암호문 base64)를 클라이언트 암호화로 생성한다."""
    subkey = derive_record_subkey(_KEY)
    encrypted = sc._encrypt_blob(pad_plaintext(serialize_metadata(fm)))
    return {
        "record_id": record_id_for(subkey, fm.virtual_path),
        "record_version": record_version,
        "encrypted_record": base64.b64encode(encrypted).decode("ascii"),
    }


class TestRecordDownload:
    @pytest.mark.asyncio
    async def test_incremental_download_merges(self, sync_client, httpx_mock):
        """증분 다운로드가 서버 레코드를 로컬에 병합한다."""
        fm = FileMetadata(
            virtual_path="/remote.txt", source_id="loop-001",
            physical_path="a/b.enc", file_size=10,
            created_at=1.0, modified_at=2.0, version=5,
            device_id="other-dev", sync_status="synced",
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records?since=0",
            method="GET",
            json={"current_version": 5,
                  "records": [_encode_server_record(sync_client, fm, 5)]},
        )
        handled = await sync_client._download_records()
        assert handled is True
        assert sync_client._last_synced_version == 5
        local = sync_client._metadata_store.lookup("/remote.txt")
        assert local is not None
        assert local.version == 5

    @pytest.mark.asyncio
    async def test_download_404_returns_false(self, sync_client, httpx_mock):
        """레코드 미지원(404)이면 False를 반환(폴백 신호)."""
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records?since=0",
            method="GET",
            status_code=404,
        )
        assert await sync_client._download_records() is False

    @pytest.mark.asyncio
    async def test_download_and_merge_falls_back_on_404(
        self, sync_client, httpx_mock
    ):
        """_download_and_merge가 404를 받으면 record_mode를 끄고 blob 경로로 폴백."""
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records?since=0",
            method="GET", status_code=404,
        )
        # blob 폴백 경로: status → version None → 빈 서버이므로 force_upload PUT(blob)
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/status",
            method="GET", json={"version": None},
        )
        httpx_mock.add_response(
            url="http://test-server/sync/metadata",
            method="PUT", json={"version": 1},
        )
        await sync_client._download_and_merge()
        assert sync_client._record_mode is False


class TestRecordUpload:
    @pytest.mark.asyncio
    async def test_upload_pending_records(self, sync_client, httpx_mock):
        """pending 파일이 레코드로 업로드되고 synced로 전환된다."""
        store = sync_client._metadata_store
        store.insert("/new.txt", "loop-001", "a/new.enc", 3, 1.0, 1.0,
                     "test-device")
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records",
            method="PUT", json={"version": 1},
        )
        handled = await sync_client._upload_records()
        assert handled is True
        assert sync_client._last_synced_version == 1
        assert not store.get_pending_files()

    @pytest.mark.asyncio
    async def test_upload_404_returns_false(self, sync_client, httpx_mock):
        """업로드 대상이 레코드 미지원(404)이면 False."""
        store = sync_client._metadata_store
        store.insert("/new.txt", "loop-001", "a/new.enc", 3, 1.0, 1.0,
                     "test-device")
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records",
            method="PUT", status_code=404,
        )
        assert await sync_client._upload_records() is False

    @pytest.mark.asyncio
    async def test_upload_cas_conflict_retries(self, sync_client, httpx_mock):
        """409 충돌 시 증분 재다운로드 후 재시도해 성공한다."""
        store = sync_client._metadata_store
        store.insert("/new.txt", "loop-001", "a/new.enc", 3, 1.0, 1.0,
                     "test-device")
        # 1차 PUT → 409
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records",
            method="PUT", status_code=409, json={"current_version": 2},
        )
        # 재병합용 GET (since=0) → 빈 변경, version 2
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records?since=0",
            method="GET", json={"current_version": 2, "records": []},
        )
        # 2차 PUT → 성공 (base_version=2)
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records",
            method="PUT", json={"version": 3},
        )
        handled = await sync_client._upload_records()
        assert handled is True
        assert sync_client._last_synced_version == 3

    @pytest.mark.asyncio
    async def test_upload_purges_expired_tombstones(
        self, sync_client, httpx_mock
    ):
        """만료된 tombstone이 purge_ids로 전송되고 로컬에서도 제거된다."""
        store = sync_client._metadata_store
        sync_client._retention_seconds = 1.0
        # 만료된 tombstone(과거 modified_at) 직접 삽입
        _insert_row(store, FileMetadata(
            virtual_path="/gone.txt", source_id="loop-001",
            physical_path="a/gone.enc", file_size=0,
            created_at=1.0, modified_at=1.0, version=2,
            device_id="test-device", sync_status="synced", deleted=True,
        ))
        subkey = derive_record_subkey(_KEY)
        expected_purge = record_id_for(subkey, "/gone.txt")

        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records",
            method="PUT", json={"version": 1},
        )
        await sync_client._upload_records()

        # 전송된 PUT 요청 본문에 만료 tombstone의 record_id가 purge_ids로 포함됨
        import json
        req = httpx_mock.get_request(
            url="http://test-server/sync/metadata/records", method="PUT"
        )
        body = json.loads(req.content)
        assert expected_purge in body["purge_ids"]
        # 로컬에서도 tombstone purge됨
        assert store.list_expired_tombstones(1.0) == []


class TestInitialSyncRecords:
    @pytest.mark.asyncio
    async def test_initial_sync_records_empty(self, sync_client, httpx_mock):
        """신규 서버(빈 레코드)로 초기 동기화가 성공하고 record_mode 유지."""
        httpx_mock.add_response(
            url="http://test-server/sync/metadata/records?since=0",
            method="GET", json={"current_version": 0, "records": []},
        )
        await sync_client.initial_sync()
        assert sync_client._record_mode is True
        assert sync_client._last_synced_version == 0
