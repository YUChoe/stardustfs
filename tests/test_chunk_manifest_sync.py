"""청크 매니페스트 동기화(Phase 2) 검증.

동기화 단위는 여전히 파일이고, 청크 매니페스트는 파일 레코드 페이로드에 실려 간다.
record_id·CAS·롱폴 프로토콜은 바뀌지 않는다. 레거시(chunks 없는) 레코드와 호환된다.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
import pytest_asyncio

from stardustlib.auth_client import AuthClient
from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.metadata_records import (
    derive_record_subkey,
    deserialize_chunks,
    deserialize_metadata,
    pad_plaintext,
    record_id_for,
    serialize_metadata,
    unpad_plaintext,
)
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import ChunkRef, FileMetadata
from stardustlib.sync_client import SyncClient

_KEY = b"\x00" * 32


def _fm(vpath="/f.bin", version=1, device_id="other-dev", deleted=False):
    return FileMetadata(
        virtual_path=vpath, source_id="src-1", physical_path="aa/x_c0000",
        file_size=100, created_at=1.0, modified_at=2.0, version=version,
        device_id=device_id, sync_status="synced", deleted=deleted,
    )


def _chunks(device_id="other-dev"):
    return [
        ChunkRef(index=0, chunk_ref="aa/x_c0000", source_id="src-1",
                 device_id=device_id, size=100, hash="aa" * 32),
        ChunkRef(index=1, chunk_ref="bb/y_c0001", source_id="src-2",
                 device_id=device_id, size=50, hash="bb" * 32),
    ]


# ------------------------------------------------------------------
# 직렬화 라운드트립
# ------------------------------------------------------------------

def test_serialize_includes_chunks():
    """청크 표현 파일은 페이로드에 chunks 배열을 담는다."""
    payload = serialize_metadata(_fm(), _chunks())
    obj = json.loads(payload.decode("utf-8"))
    assert [c["index"] for c in obj["chunks"]] == [0, 1]
    assert obj["chunks"][0]["chunk_ref"] == "aa/x_c0000"
    assert obj["chunks"][1]["source_id"] == "src-2"


def test_serialize_omits_chunks_for_legacy():
    """레거시 통짜 blob 파일은 chunks를 생략한다."""
    assert "chunks" not in json.loads(
        serialize_metadata(_fm(), None).decode("utf-8")
    )
    assert "chunks" not in json.loads(
        serialize_metadata(_fm(), []).decode("utf-8")
    )


def test_chunks_roundtrip_through_payload():
    """serialize → deserialize_chunks가 매니페스트를 그대로 복원한다."""
    original = _chunks()
    restored = deserialize_chunks(serialize_metadata(_fm(), original))
    assert len(restored) == 2
    for before, after in zip(original, restored):
        assert (before.index, before.chunk_ref, before.source_id,
                before.device_id, before.size, before.hash) == (
            after.index, after.chunk_ref, after.source_id,
            after.device_id, after.size, after.hash)


def test_deserialize_chunks_on_legacy_record_is_empty():
    """chunks가 없는 레거시 레코드는 빈 목록을 반환한다(하위 호환)."""
    assert deserialize_chunks(serialize_metadata(_fm())) == []


def test_chunks_sorted_by_index():
    """매니페스트는 인덱스 순으로 직렬화·복원된다."""
    shuffled = list(reversed(_chunks()))
    restored = deserialize_chunks(serialize_metadata(_fm(), shuffled))
    assert [c.index for c in restored] == [0, 1]


def test_metadata_fields_unaffected_by_chunks():
    """chunks 추가가 기존 파일 메타데이터 필드 복원에 영향을 주지 않는다."""
    fm = _fm(version=7)
    restored = deserialize_metadata(serialize_metadata(fm, _chunks()))
    assert restored.virtual_path == fm.virtual_path
    assert restored.version == 7
    assert restored.device_id == "other-dev"


def test_padding_still_quantised_with_chunks():
    """청크를 담아도 평문은 256B 배수로 패딩된다(크기 관측 완화 유지)."""
    padded = pad_plaintext(serialize_metadata(_fm(), _chunks()))
    assert len(padded) % 256 == 0
    assert unpad_plaintext(padded) == serialize_metadata(_fm(), _chunks())


# ------------------------------------------------------------------
# 병합 (SyncClient)
# ------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = MetadataStore(str(tmp_path / "m.db"), _KEY)
    s.initialize()
    yield s
    s.close()


def _seed(store, vpath, version=1, device_id="other-dev", chunks=None):
    """동기화 완료(synced) 상태의 레코드를 심는다.

    MetadataStore.insert는 sync_status='pending'으로 넣기 때문에, 그대로 쓰면 병합이
    로컬 수정으로 보고 충돌 경로를 타 버린다. 여기서는 서버 우선 갱신 경로를 검증하려
    하므로 synced 상태로 직접 삽입한다.
    """
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO files "
        "(virtual_path, source_id, physical_path, file_size, created_at, "
        "modified_at, version, device_id, sync_status, deleted, evicted, "
        "replication_status, chunked) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'synced', 0, 0, 'none', 0)",
        (vpath, "src-1", "old", 100, 1.0, 1.0, version, device_id),
    )
    conn.commit()
    if chunks:
        store.put_chunks(vpath, chunks)
        store.commit()


@pytest_asyncio.fixture
async def sync_client(store):
    auth = AuthClient("http://test-server", timeout=5.0)
    auth._access_token = "test-token"
    auth._refresh_token_value = "test-refresh"
    auth._token_expires_at = time.time() + 3600
    auth._user_id = "user-123"
    auth._offline = False
    sc = SyncClient(
        auth_client=auth,
        server_url="http://test-server",
        metadata_store=store,
        conflict_resolver=ConflictResolver(store, "test-device"),
        interval_seconds=1,
        encryption_key=_KEY,
    )
    yield sc
    await sc.stop()


def _server_record(sc, fm, record_version, chunks=None):
    """서버가 돌려줄 레코드(암호문 base64)를 클라이언트 암호화로 생성한다."""
    subkey = derive_record_subkey(_KEY)
    encrypted = sc._encrypt_blob(pad_plaintext(serialize_metadata(fm, chunks)))
    return {
        "record_id": record_id_for(subkey, fm.virtual_path),
        "record_version": record_version,
        "encrypted_record": base64.b64encode(encrypted).decode("ascii"),
    }


@pytest.mark.asyncio
async def test_download_adopts_chunk_manifest(sync_client, store, httpx_mock):
    """새 파일 수신 시 청크 매니페스트가 함께 채택된다."""
    fm = _fm("/new.bin", version=5)
    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records?since=0",
        method="GET",
        json={"current_version": 5,
              "records": [_server_record(sync_client, fm, 5, _chunks())]},
    )
    assert await sync_client._download_records() is True

    local = store.get_chunks("/new.bin")
    assert [c.index for c in local] == [0, 1]
    # 청크마다 소유 기기·소스가 따로 기록된다(청크별 라우팅의 근거).
    assert local[0].source_id == "src-1"
    assert local[1].source_id == "src-2"
    assert local[1].device_id == "other-dev"
    assert local[1].hash == "bb" * 32


@pytest.mark.asyncio
async def test_download_updates_existing_manifest(
    sync_client, store, httpx_mock
):
    """서버가 더 최신이면 매니페스트를 통째로 교체한다."""
    _seed(store, "/f.bin", chunks=[
        ChunkRef(index=0, chunk_ref="old/z_c0000", source_id="src-1",
                 device_id="other-dev", size=10, hash="cc" * 32),
    ])

    fm = _fm("/f.bin", version=99)
    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records?since=0",
        method="GET",
        json={"current_version": 99,
              "records": [_server_record(sync_client, fm, 99, _chunks())]},
    )
    await sync_client._download_records()

    local = store.get_chunks("/f.bin")
    assert [c.chunk_ref for c in local] == ["aa/x_c0000", "bb/y_c0001"]


@pytest.mark.asyncio
async def test_legacy_record_clears_local_manifest(
    sync_client, store, httpx_mock
):
    """서버 표현이 레거시 blob이면 로컬 매니페스트를 비운다(표현 일치)."""
    _seed(store, "/f.bin", chunks=_chunks())
    assert store.get_chunks("/f.bin")

    fm = _fm("/f.bin", version=50)
    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records?since=0",
        method="GET",
        json={"current_version": 50,
              "records": [_server_record(sync_client, fm, 50, None)]},
    )
    await sync_client._download_records()

    assert store.get_chunks("/f.bin") == []


@pytest.mark.asyncio
async def test_tombstone_does_not_resurrect_manifest(
    sync_client, store, httpx_mock
):
    """tombstone 레코드는 매니페스트를 복원하지 않는다."""
    _seed(store, "/f.bin", chunks=_chunks())

    fm = _fm("/f.bin", version=60, deleted=True)
    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records?since=0",
        method="GET",
        json={"current_version": 60,
              "records": [_server_record(sync_client, fm, 60, _chunks())]},
    )
    await sync_client._download_records()

    assert store.lookup_any("/f.bin").deleted is True
    assert store.get_chunks("/f.bin") == []


@pytest.mark.asyncio
async def test_upload_sends_local_manifest(sync_client, store, httpx_mock):
    """업로드 시 로컬 청크 매니페스트가 레코드 페이로드에 실린다."""
    store.insert("/mine.bin", "src-1", "aa/x_c0000", 150, 1.0, 1.0,
                 device_id="test-device")
    store.put_chunks("/mine.bin", _chunks("test-device"))
    store.commit()

    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records",
        method="PUT",
        json={"version": 1},
    )
    assert await sync_client._upload_records() is True

    request = httpx_mock.get_requests()[-1]
    body = json.loads(request.content)
    assert body["base_version"] == 0          # CAS 프로토콜 불변
    assert len(body["records"]) == 1
    entry = body["records"][0]
    assert "record_id" in entry                # record_id 규칙 불변

    # 레코드 암호문을 풀어 매니페스트가 담겼는지 확인한다.
    padded = sync_client._decrypt_blob(
        base64.b64decode(entry["encrypted_record"])
    )
    plaintext = unpad_plaintext(padded)
    assert [c.index for c in deserialize_chunks(plaintext)] == [0, 1]
    assert deserialize_metadata(plaintext).virtual_path == "/mine.bin"


@pytest.mark.asyncio
async def test_upload_omits_manifest_for_legacy_file(
    sync_client, store, httpx_mock
):
    """레거시 blob 파일은 chunks 없이 업로드된다."""
    store.insert("/legacy.bin", "src-1", "phys.enc", 20, 1.0, 1.0,
                 device_id="test-device")

    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records",
        method="PUT",
        json={"version": 1},
    )
    await sync_client._upload_records()

    body = json.loads(httpx_mock.get_requests()[-1].content)
    padded = sync_client._decrypt_blob(
        base64.b64decode(body["records"][0]["encrypted_record"])
    )
    plaintext = unpad_plaintext(padded)
    assert deserialize_chunks(plaintext) == []
    assert "chunks" not in json.loads(plaintext.decode("utf-8"))


@pytest.mark.asyncio
async def test_blob_fallback_leaves_manifest_untouched(sync_client, store):
    """전체 blob 폴백 경로(chunks 미전달)는 로컬 매니페스트를 건드리지 않는다."""
    _seed(store, "/f.bin", chunks=_chunks())

    # blob 경로의 _merge_record는 chunks 인자를 주지 않는다.
    sync_client._merge_record(_fm("/f.bin", version=42))

    assert [c.index for c in store.get_chunks("/f.bin")] == [0, 1]


@pytest.mark.asyncio
async def test_conflict_path_adopts_manifest(sync_client, store, httpx_mock):
    """충돌 처리도 서버 레코드를 원본 경로에 적용하므로 매니페스트를 채택한다."""
    # sync_status='pending'(로컬 수정) + 서버도 더 최신 → 충돌 경로
    store.insert("/f.bin", "src-1", "old", 100, 1.0, 1.0,
                 device_id="test-device")

    fm = _fm("/f.bin", version=42)
    httpx_mock.add_response(
        url="http://test-server/sync/metadata/records?since=0",
        method="GET",
        json={"current_version": 42,
              "records": [_server_record(sync_client, fm, 42, _chunks())]},
    )
    await sync_client._download_records()

    assert [c.chunk_ref for c in store.get_chunks("/f.bin")] == [
        "aa/x_c0000", "bb/y_c0001"
    ]
