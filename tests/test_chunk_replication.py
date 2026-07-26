"""청크 표현 파일의 복제 정합(Phase 4) 검증.

청크 표현 파일은 at-rest 청크를 재분할 없이 그대로 복제 청크로 쓰고, 복구도 청크를
그대로 되돌려 기록한다. 그래야 복구 후 at-rest 바이트가 복제 시점과 동일해 등록된
청크 해시가 계속 유효하다. 백업은 이 device가 보관한 청크만 올린다.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from stardustlib import chunker
from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import ChunkRef
from stardustlib.replication_manager import ReplicationManager
from stardustlib.storage_pool import StoragePool
from stardustlib.storage_source import DirectorySource

SMALL_CHUNK = 256
_KEY = b"\x07" * 32


@pytest.fixture()
def pool(monkeypatch):
    monkeypatch.setattr("stardustlib.storage_pool.CHUNK_SIZE", SMALL_CHUNK)
    d = tempfile.mkdtemp()
    data_dir = os.path.join(d, "data")
    os.makedirs(data_dir, exist_ok=True)
    src = DirectorySource("local-1", data_dir)
    src.initialize()
    store = MetadataStore(os.path.join(d, "m.db"), _KEY)
    store.initialize()
    sp = StoragePool(
        [src], store, encryption_engine=EncryptionEngine(_KEY),
        device_id="dev-A",
    )
    try:
        yield sp, store, src
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def _manager(pool_obj, chunk_size=4 * 1024 * 1024):
    """네트워크를 타지 않는 ReplicationManager(청크 선택 로직만 검증)."""
    mgr = ReplicationManager.__new__(ReplicationManager)
    mgr._storage_pool = pool_obj
    mgr._chunk_size = chunk_size
    mgr._engine = EncryptionEngine(_KEY)
    mgr._progress = None      # 진행 추적 미주입(no-op)
    mgr._meta = pool_obj.metadata_store
    return mgr


# ------------------------------------------------------------------
# at-rest 청크 재사용
# ------------------------------------------------------------------

def test_read_chunks_returns_at_rest_boundaries(pool):
    """read_chunks는 저장된 청크 경계를 그대로 돌려준다."""
    sp, store, src = pool
    data = os.urandom(SMALL_CHUNK * 3 + 11)
    sp.write_file("/f.bin", data)

    parts = sp.read_chunks("/f.bin")
    manifest = store.get_chunks("/f.bin")
    assert [idx for idx, _d in parts] == [c.index for c in manifest]
    for (_idx, blob), chunk in zip(parts, manifest):
        assert blob == src.read(chunk.chunk_ref)      # 저장된 바이트 그대로
        assert chunker.chunk_hash(blob) == chunk.hash


def test_read_chunks_empty_for_legacy_blob(pool):
    """청크 레코드가 없는 파일은 빈 목록을 반환한다."""
    sp, store, src = pool
    engine = EncryptionEngine(_KEY)
    phys = "a" * 32 + "_legacy.bin"
    src.write(phys, engine.encrypt(b"legacy payload"))
    store.insert("/legacy.bin", "local-1", phys, 14, 1.0, 1.0,
                 device_id="dev-A")

    assert sp.read_chunks("/legacy.bin") == []


def test_replicate_reuses_at_rest_chunks_without_resplit(pool):
    """청크 표현 파일은 at-rest 청크가 그대로 복제 청크가 된다(재분할 없음)."""
    sp, store, src = pool
    data = os.urandom(SMALL_CHUNK * 3)
    sp.write_file("/f.bin", data)
    mgr = _manager(sp)

    chunks = mgr._chunks_to_replicate("/f.bin")

    manifest = store.get_chunks("/f.bin")
    assert len(chunks) == len(manifest) == 3
    for (idx, blob), chunk in zip(chunks, manifest):
        assert idx == chunk.index
        assert blob == src.read(chunk.chunk_ref)
        # 복제 청크 해시가 at-rest 청크 해시와 같다(재계산 불필요).
        assert chunker.chunk_hash(blob) == chunk.hash


def test_replicate_skips_file_without_chunks(pool):
    """청크 레코드가 없으면 올릴 로컬 데이터가 없으므로 빈 목록이다.

    원격 전체 읽기(read_ciphertext 폴백)로 데이터를 당겨오지 않는다.
    """
    sp, store, src = pool
    engine = EncryptionEngine(_KEY)
    plain = os.urandom(300)
    phys = "b" * 32 + "_nochunks.bin"
    src.write(phys, engine.encrypt(plain))
    store.insert("/nochunks.bin", "local-1", phys, len(plain), 1.0, 1.0,
                 device_id="dev-A")

    mgr = _manager(sp, chunk_size=128)
    assert mgr._chunks_to_replicate("/nochunks.bin") == []


def test_read_chunks_local_only_skips_remote_chunks(pool):
    """local_only=True면 다른 device가 보관한 청크는 읽지 않는다.

    데이터를 갖지 않은 기기가 원본을 릴레이로 당겨오는 왕복을 막는 경로다.
    """
    sp, store, _src = pool
    sp.write_file("/f.bin", os.urandom(SMALL_CHUNK * 3))
    manifest = store.get_chunks("/f.bin")
    assert len(manifest) == 3
    # 가운데 청크만 다른 기기 보관으로 바꾼다
    moved = [
        ChunkRef(index=c.index, chunk_ref=c.chunk_ref, source_id=c.source_id,
                 device_id=("dev-B" if c.index == 1 else None),
                 size=c.size, hash=c.hash)
        for c in manifest
    ]
    store.put_chunks("/f.bin", moved)

    local = sp.read_chunks("/f.bin", local_only=True)
    assert [idx for idx, _d in local] == [0, 2]
    # local_only=False면 전부(원격 포함) 대상이다
    assert len(store.get_chunks("/f.bin")) == 3


def test_at_rest_chunks_differ_from_naive_resplit(pool):
    """at-rest 청크 경계는 암호문을 고정 크기로 나눈 것과 다르다.

    청크마다 38B 헤더가 붙으므로 이어붙인 암호문을 CHUNK_SIZE로 다시 나누면 경계가
    어긋난다. 재분할을 없앤 이유다.
    """
    sp, _store, _src = pool
    data = os.urandom(SMALL_CHUNK * 2)
    sp.write_file("/f.bin", data)
    mgr = _manager(sp)

    at_rest = mgr._chunks_to_replicate("/f.bin")
    naive = chunker.split(sp.read_ciphertext("/f.bin"), SMALL_CHUNK)

    assert [len(d) for _i, d in at_rest] != [len(d) for _i, d in naive]


# ------------------------------------------------------------------
# 복구: 청크를 그대로 되돌려 기록
# ------------------------------------------------------------------

def test_recover_restores_chunk_bytes_exactly(pool):
    """청크 표현으로 복구하면 at-rest 바이트가 복제 시점과 동일하다."""
    sp, store, src = pool
    data = os.urandom(SMALL_CHUNK * 3 + 5)
    sp.write_file("/f.bin", data)
    mgr = _manager(sp)

    replicated = mgr._chunks_to_replicate("/f.bin")
    before = [src.read(c.chunk_ref) for c in store.get_chunks("/f.bin")]

    # 로컬 블록을 훼손한 뒤 복제본(청크)으로 되돌린다.
    sp.delete_file("/f.bin")
    assert mgr._is_chunked_set(replicated) is True
    ciphers = [blob for _idx, blob in replicated]
    plain_size = sum(len(mgr._engine.decrypt(c)) for c in ciphers)
    sp.write_chunks("/f.bin", ciphers, plain_size)

    after = [src.read(c.chunk_ref) for c in store.get_chunks("/f.bin")]
    assert after == before                 # 청크 바이트 동일
    assert sp.read_file("/f.bin") == data   # 평문도 복원
    # 청크 해시가 복구 후에도 유효하다.
    for chunk in store.get_chunks("/f.bin"):
        assert chunker.chunk_hash(src.read(chunk.chunk_ref)) == chunk.hash


def test_chunked_set_detection(pool):
    """청크 표현과 레거시 분할 조각을 구분한다."""
    sp, _store, _src = pool
    mgr = _manager(sp)
    engine = EncryptionEngine(_KEY)

    # 청크 표현: 조각마다 암호문 헤더로 시작
    chunked = [(0, engine.encrypt(b"a" * 10)), (1, engine.encrypt(b"b" * 10))]
    assert mgr._is_chunked_set(chunked) is True

    # 레거시 분할: 첫 조각만 헤더로 시작
    blob = engine.encrypt(b"x" * 200)
    legacy = chunker.split(blob, 100)
    assert mgr._is_chunked_set(legacy) is False

    # 조각이 하나면 두 표현이 같으므로 단일 blob 경로로 처리
    assert mgr._is_chunked_set([(0, engine.encrypt(b"solo"))]) is False


def test_recover_legacy_blob_path(pool):
    """레거시 분할 조각은 이어붙여 단일 블록으로 복원한다."""
    sp, store, _src = pool
    mgr = _manager(sp)
    engine = EncryptionEngine(_KEY)

    plain = os.urandom(500)
    blob = engine.encrypt(plain)
    parts = chunker.split(blob, 128)
    assert mgr._is_chunked_set(parts) is False

    joined = chunker.join(parts)
    sp.write_ciphertext("/restored.bin", joined, len(plain))

    assert sp.read_file("/restored.bin") == plain
    assert store.lookup("/restored.bin").file_size == len(plain)


# ------------------------------------------------------------------
# 백업 대상 선정: 로컬 청크 보유 기준 (소유는 사용자, device는 보관 위치)
# ------------------------------------------------------------------

def test_list_paths_with_local_chunks_selects_by_storage(pool):
    """청크를 실제로 들고 있는 device만 그 파일을 백업 대상으로 삼는다."""
    sp, store, _src = pool
    sp.write_file("/mine.bin", os.urandom(SMALL_CHUNK))
    sp.write_file("/theirs.bin", os.urandom(SMALL_CHUNK))
    # /theirs.bin의 청크를 전부 다른 기기 보관으로 바꾼다
    store.put_chunks("/theirs.bin", [
        ChunkRef(index=c.index, chunk_ref=c.chunk_ref, source_id=c.source_id,
                 device_id="dev-B", size=c.size, hash=c.hash)
        for c in store.get_chunks("/theirs.bin")
    ])

    paths = store.list_paths_with_local_chunks(("none", "pending"), "dev-A")
    assert "/mine.bin" in paths
    assert "/theirs.bin" not in paths

    # 그 기기에서 보면 반대다
    other = store.list_paths_with_local_chunks(("none", "pending"), "dev-B")
    assert other == ["/theirs.bin"]


def test_list_paths_with_local_chunks_excludes_chunkless(pool):
    """청크 레코드가 없는 파일은 올릴 물리 데이터가 없으므로 제외한다."""
    sp, store, src = pool
    engine = EncryptionEngine(_KEY)
    phys = "c" * 32 + "_none.bin"
    src.write(phys, engine.encrypt(b"payload"))
    store.insert("/nochunk.bin", "local-1", phys, 7, 1.0, 1.0,
                 device_id="dev-A")

    paths = store.list_paths_with_local_chunks(("none", "pending"), "dev-A")
    assert "/nochunk.bin" not in paths


def test_list_paths_with_local_chunks_filters_status(pool):
    """복제 상태 필터가 적용된다."""
    sp, store, _src = pool
    sp.write_file("/f.bin", os.urandom(SMALL_CHUNK))
    store.set_replication_status("/f.bin", "replicated")

    assert store.list_paths_with_local_chunks(("none",), "dev-A") == []
    assert store.list_paths_with_local_chunks(
        ("replicated",), "dev-A"
    ) == ["/f.bin"]
