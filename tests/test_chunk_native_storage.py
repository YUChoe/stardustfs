"""청크 네이티브 저장(Phase 1) 검증.

design.md의 정합성 속성을 확인한다.
- Property 1: 라운드트립 — write_file 후 read_file이 원본과 바이트 단위로 동일
- Property 2: 청크 독립성 — 각 청크가 단독으로 복호화됨(청크별 IV·인증 태그)
- Property 3: 부분 읽기 정확성 — 범위를 덮는 청크만 가져와도 정확한 바이트
- Property 5: 레거시 호환 — 통짜 blob 파일은 기존 단일 경로로 계속 읽힘
추가로 샤드 분산과 orphan GC의 샤드 인식을 확인한다.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from stardustlib import chunker
from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.metadata_store import MetadataStore
from stardustlib.storage_pool import StoragePool
from stardustlib.storage_source import DirectorySource

# 테스트에서 쓰는 작은 청크 크기(실제 4 MiB로는 경계 케이스 검증이 느리다).
SMALL_CHUNK = 1024


@pytest.fixture()
def pool(monkeypatch):
    """암호화가 켜진 로컬 단일 소스 스토리지 풀."""
    d = tempfile.mkdtemp()
    # 청크 경계 검증을 위해 청크 크기를 축소한다.
    monkeypatch.setattr("stardustlib.storage_pool.CHUNK_SIZE", SMALL_CHUNK)
    data_dir = os.path.join(d, "data")
    os.makedirs(data_dir, exist_ok=True)  # DirectorySource는 기존 디렉토리를 요구한다
    src = DirectorySource("src-1", data_dir)
    src.initialize()
    store = MetadataStore(os.path.join(d, "m.db"), b"\x01" * 32)
    store.initialize()
    engine = EncryptionEngine(b"\x02" * 32)
    sp = StoragePool([src], store, encryption_engine=engine, device_id="dev-A")
    try:
        yield sp, store, src
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# Property 1: 라운드트립
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "size",
    [
        0,                  # 빈 파일
        1,                  # 1바이트
        SMALL_CHUNK - 1,    # 청크 경계 직전
        SMALL_CHUNK,        # 정확히 한 청크
        SMALL_CHUNK + 1,    # 경계 직후(부분 청크 발생)
        SMALL_CHUNK * 3,    # 정확히 세 청크
        SMALL_CHUNK * 3 + 7,  # 세 청크 + 부분 청크
    ],
)
def test_chunk_roundtrip_is_byte_identical(pool, size):
    """write_file → read_file이 원본과 바이트 단위로 동일하다 (Property 1)."""
    sp, _store, _src = pool
    data = os.urandom(size)
    sp.write_file("/f.bin", data)
    assert sp.read_file("/f.bin") == data


def test_write_creates_expected_chunk_count(pool):
    """평문 크기에 맞는 수의 청크가 만들어지고 매니페스트에 기록된다."""
    sp, store, _src = pool
    data = os.urandom(SMALL_CHUNK * 2 + 10)
    sp.write_file("/f.bin", data)

    chunks = store.get_chunks("/f.bin")
    assert len(chunks) == chunker.chunk_count(len(data), SMALL_CHUNK) == 3
    assert [c.index for c in chunks] == [0, 1, 2]
    # 마지막 청크만 부분 청크다(암호문 = 평문 + 헤더).
    overhead = EncryptionEngine.HEADER_SIZE
    assert chunks[0].size == SMALL_CHUNK + overhead
    assert chunks[1].size == SMALL_CHUNK + overhead
    assert chunks[2].size == 10 + overhead


def test_empty_file_is_single_chunk(pool):
    """빈 파일도 청크 1개로 표현해 읽기 경로가 단일하다."""
    sp, store, _src = pool
    sp.write_file("/empty.bin", b"")
    assert len(store.get_chunks("/empty.bin")) == 1
    assert sp.read_file("/empty.bin") == b""


# ------------------------------------------------------------------
# Property 2: 청크 독립성
# ------------------------------------------------------------------

def test_each_chunk_decrypts_independently(pool):
    """각 청크는 다른 청크 없이 단독으로 복호화된다 (Property 2)."""
    sp, store, src = pool
    parts = [os.urandom(SMALL_CHUNK), os.urandom(SMALL_CHUNK), os.urandom(32)]
    sp.write_file("/f.bin", b"".join(parts))

    engine = EncryptionEngine(b"\x02" * 32)
    for chunk, expected in zip(store.get_chunks("/f.bin"), parts):
        cipher = src.read(chunk.chunk_ref)
        # 청크 하나만 읽어 단독 복호화가 성립한다.
        assert engine.decrypt(cipher) == expected


def test_chunks_have_distinct_ivs(pool):
    """청크마다 IV가 달라야 한다(같은 평문이 반복되어도 암호문이 겹치지 않음)."""
    sp, store, src = pool
    # 동일한 평문 블록을 3개 청크로 저장
    sp.write_file("/same.bin", b"A" * (SMALL_CHUNK * 3))

    ivs = set()
    for chunk in store.get_chunks("/same.bin"):
        cipher = src.read(chunk.chunk_ref)
        ivs.add(cipher[6:22])  # 헤더의 IV 구간
    assert len(ivs) == 3


def test_chunk_hash_recorded_and_verified(pool):
    """청크 해시가 기록되고, 손상 시 어느 청크인지 밝히며 실패한다."""
    sp, store, src = pool
    sp.write_file("/f.bin", os.urandom(SMALL_CHUNK * 2))

    chunks = store.get_chunks("/f.bin")
    for chunk in chunks:
        assert chunk.hash == chunker.chunk_hash(src.read(chunk.chunk_ref))

    # 두 번째 청크를 훼손하면 규격 에러로 그 인덱스를 알린다.
    target = chunks[1]
    src.write(target.chunk_ref, b"corrupted" * 8)
    with pytest.raises(OSError) as exc:
        sp.read_file("/f.bin")
    assert "chunk_index=1" in str(exc.value)


def test_missing_chunk_reports_index(pool):
    """청크가 사라지면 어느 인덱스가 없는지 명시한 규격 에러를 낸다."""
    sp, store, src = pool
    sp.write_file("/f.bin", os.urandom(SMALL_CHUNK * 2))

    target = store.get_chunks("/f.bin")[0]
    src.delete(target.chunk_ref)
    with pytest.raises(OSError) as exc:
        sp.read_file("/f.bin")
    assert "chunk_index=0" in str(exc.value)


# ------------------------------------------------------------------
# Property 3: 부분 읽기
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "offset,length",
    [
        (0, 10),                        # 첫 청크 내부
        (5, 20),                        # 첫 청크 내부, 오프셋 있음
        (SMALL_CHUNK - 5, 10),          # 청크 경계 걸침
        (SMALL_CHUNK, SMALL_CHUNK),     # 두 번째 청크 전체
        (SMALL_CHUNK + 3, SMALL_CHUNK * 2),  # 여러 청크 걸침
        (0, SMALL_CHUNK * 3 + 7),       # 전체 범위
        (SMALL_CHUNK * 3, 7),           # 마지막 부분 청크
    ],
)
def test_read_range_matches_full_read(pool, offset, length):
    """read_range는 전체를 읽고 자른 것과 동일한 바이트를 반환한다 (Property 3)."""
    sp, _store, _src = pool
    data = os.urandom(SMALL_CHUNK * 3 + 7)
    sp.write_file("/f.bin", data)
    assert sp.read_range("/f.bin", offset, length) == data[offset:offset + length]


def test_read_range_fetches_only_covering_chunks(pool, monkeypatch):
    """범위를 덮는 청크만 소스에서 읽는다(불필요한 청크 미조회)."""
    sp, store, src = pool
    data = os.urandom(SMALL_CHUNK * 4)
    sp.write_file("/f.bin", data)

    chunks = store.get_chunks("/f.bin")
    assert len(chunks) == 4
    read_refs: list[str] = []
    original = src.read

    def spy(path):
        read_refs.append(path)
        return original(path)

    monkeypatch.setattr(src, "read", spy)
    # 두 번째 청크 내부만 요청 → 청크 1개만 읽어야 한다.
    sp.read_range("/f.bin", SMALL_CHUNK + 1, 10)
    assert read_refs == [chunks[1].chunk_ref]


def test_read_range_beyond_eof_returns_available(pool):
    """파일 끝을 넘는 범위는 존재하는 부분까지만 반환한다."""
    sp, _store, _src = pool
    data = os.urandom(100)
    sp.write_file("/f.bin", data)
    assert sp.read_range("/f.bin", 90, 1000) == data[90:]
    assert sp.read_range("/f.bin", 5000, 10) == b""


def test_read_range_zero_length_and_negative(pool):
    """length=0은 빈 바이트, 음수 인자는 규격 에러."""
    sp, _store, _src = pool
    sp.write_file("/f.bin", b"abcdef")
    assert sp.read_range("/f.bin", 2, 0) == b""
    with pytest.raises(ValueError):
        sp.read_range("/f.bin", -1, 5)


# ------------------------------------------------------------------
# Property 5: 레거시 호환
# ------------------------------------------------------------------

def test_legacy_blob_still_readable(pool):
    """통짜 blob으로 저장된 기존 파일은 계속 읽힌다 (Property 5)."""
    sp, store, src = pool
    engine = EncryptionEngine(b"\x02" * 32)
    plain = os.urandom(SMALL_CHUNK * 2 + 5)
    phys = "a" * 32 + "_legacy.bin"
    src.write(phys, engine.encrypt(plain))
    store.insert("/legacy.bin", "src-1", phys, len(plain), 1.0, 1.0,
                 device_id="dev-A")

    assert not store.get_chunks("/legacy.bin")   # 매니페스트 없음
    assert sp.read_file("/legacy.bin") == plain
    # 부분 읽기도 레거시 경로로 동작한다.
    assert sp.read_range("/legacy.bin", 10, 50) == plain[10:60]


def test_legacy_overwrite_keeps_blob_representation(pool):
    """레거시 blob 덮어쓰기는 표현을 유지한다(전환은 마이그레이션 단계)."""
    sp, store, src = pool
    engine = EncryptionEngine(b"\x02" * 32)
    phys = "b" * 32 + "_legacy.bin"
    src.write(phys, engine.encrypt(b"old"))
    store.insert("/legacy.bin", "src-1", phys, 3, 1.0, 1.0, device_id="dev-A")

    sp.write_file("/legacy.bin", b"new content")

    rec = store.lookup("/legacy.bin")
    assert rec.physical_path == phys          # 같은 물리 위치
    assert not store.get_chunks("/legacy.bin")  # 여전히 레거시
    assert sp.read_file("/legacy.bin") == b"new content"


def test_new_file_is_chunked(pool):
    """신규 파일은 청크 표현으로 저장되고 chunked=1로 표시된다."""
    sp, store, _src = pool
    sp.write_file("/new.bin", os.urandom(SMALL_CHUNK + 1))
    assert len(store.get_chunks("/new.bin")) == 2


# ------------------------------------------------------------------
# 샤드 분산 · orphan GC
# ------------------------------------------------------------------

def test_chunks_are_sharded_by_hash_prefix(pool):
    """청크는 암호문 해시 앞 2hex 서브디렉토리에 배치된다."""
    sp, store, _src = pool
    sp.write_file("/f.bin", os.urandom(SMALL_CHUNK * 3))

    for chunk in store.get_chunks("/f.bin"):
        shard, _, name = chunk.chunk_ref.partition("/")
        assert shard == chunk.hash[:chunker.SHARD_HEX_LEN]
        assert name.endswith(f"_c{chunk.index:04d}")


def test_same_file_chunks_spread_across_shards(pool):
    """한 파일의 청크가 한 디렉토리로 몰리지 않는다(해시 기반 분산).

    파일 단위 UUID를 샤드 키로 쓰면 모든 청크가 같은 디렉토리에 쌓여 엔트리 폭증이
    재현된다. 해시 기반이면 청크마다 샤드가 달라진다.
    """
    sp, store, _src = pool
    sp.write_file("/big.bin", os.urandom(SMALL_CHUNK * 24))

    shards = {c.chunk_ref.split("/")[0] for c in store.get_chunks("/big.bin")}
    # 24개 청크가 256개 샤드에 흩어지므로 대부분 서로 다른 디렉토리가 된다.
    assert len(shards) > 1


def test_orphan_gc_preserves_chunks(pool):
    """orphan GC가 활성 파일의 청크를 샤드 디렉토리에서 인식해 보존한다."""
    sp, store, src = pool
    sp.write_file("/keep.bin", os.urandom(SMALL_CHUNK * 3))
    chunks = store.get_chunks("/keep.bin")

    # 매니페스트가 참조하지 않는 고아 청크(관리 파일 형식, 샤드 아래)
    orphan = "cc/" + "0" * 32 + "_c0000"
    src.write(orphan, b"orphan")

    removed = sp.gc_orphan_files()

    assert removed == 1
    assert not src.exists(orphan)
    for chunk in chunks:
        assert src.exists(chunk.chunk_ref)
    assert sp.read_file("/keep.bin")  # 여전히 읽힌다


def test_delete_file_removes_all_chunks(pool):
    """파일 삭제 시 청크 전부와 매니페스트가 정리된다."""
    sp, store, src = pool
    sp.write_file("/gone.bin", os.urandom(SMALL_CHUNK * 3))
    chunks = store.get_chunks("/gone.bin")

    sp.delete_file("/gone.bin")

    for chunk in chunks:
        assert not src.exists(chunk.chunk_ref)
    assert not store.get_chunks("/gone.bin")


def test_overwrite_discards_old_chunks(pool):
    """덮어쓰기는 새 청크를 커밋한 뒤 옛 청크를 정리한다."""
    sp, store, src = pool
    sp.write_file("/f.bin", os.urandom(SMALL_CHUNK * 3))
    old = store.get_chunks("/f.bin")

    new_data = os.urandom(SMALL_CHUNK + 5)
    sp.write_file("/f.bin", new_data)

    new = store.get_chunks("/f.bin")
    assert {c.chunk_ref for c in new} & {c.chunk_ref for c in old} == set()
    for chunk in old:
        assert not src.exists(chunk.chunk_ref)
    assert sp.read_file("/f.bin") == new_data


def test_move_file_carries_manifest(pool):
    """파일 이동 시 청크 매니페스트도 새 가상 경로로 따라간다."""
    sp, store, _src = pool
    data = os.urandom(SMALL_CHUNK * 2)
    sp.write_file("/a.bin", data)

    sp.move_file("/a.bin", "/b.bin")

    assert not store.get_chunks("/a.bin")
    assert len(store.get_chunks("/b.bin")) == 2
    assert sp.read_file("/b.bin") == data


def test_ciphertext_roundtrip_preserves_chunk_bytes(pool):
    """read_ciphertext → write_ciphertext가 청크 바이트를 그대로 복원한다.

    복제본 복구(recover) 경로다. at-rest 바이트가 동일하게 유지되어야 등록된 청크
    해시가 복구 후에도 유효하다.
    """
    sp, store, src = pool
    data = os.urandom(SMALL_CHUNK * 2 + 9)
    sp.write_file("/f.bin", data)

    at_rest = sp.read_ciphertext("/f.bin")
    original = [src.read(c.chunk_ref) for c in store.get_chunks("/f.bin")]
    assert at_rest == b"".join(original)

    sp.delete_file("/f.bin")
    sp.write_ciphertext("/f.bin", at_rest, len(data))

    restored = [src.read(c.chunk_ref) for c in store.get_chunks("/f.bin")]
    assert restored == original           # 청크 바이트 동일
    assert sp.read_file("/f.bin") == data


# ------------------------------------------------------------------
# 스키마 v7 마이그레이션 · 매니페스트 CRUD
# ------------------------------------------------------------------

def _store(tmp: str) -> MetadataStore:
    s = MetadataStore(os.path.join(tmp, "m.db"), b"\x03" * 32)
    s.initialize()
    return s


def test_v7_migration_is_idempotent():
    """initialize를 여러 번 호출해도 v7 마이그레이션이 안전하다."""
    d = tempfile.mkdtemp()
    try:
        store = _store(d)
        store.initialize()   # 재호출
        conn = store._get_conn()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(files)")]
        assert "chunked" in cols
        version = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert version >= 7
        store.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_existing_files_default_to_legacy():
    """마이그레이션 후 기존 파일은 chunked=0(레거시)이다."""
    d = tempfile.mkdtemp()
    try:
        store = _store(d)
        store.insert("/old.bin", "s1", "phys", 10, 1.0, 1.0, device_id="dev-A")
        row = store._get_conn().execute(
            "SELECT chunked FROM files WHERE virtual_path = ?", ("/old.bin",)
        ).fetchone()
        assert row["chunked"] == 0
        assert store.get_chunks("/old.bin") == []
        store.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_put_chunks_replaces_manifest_and_marks_chunked():
    """put_chunks는 매니페스트를 통째로 교체하고 chunked=1로 표시한다."""
    from stardustlib.models import ChunkRef

    d = tempfile.mkdtemp()
    try:
        store = _store(d)
        store.insert("/f.bin", "s1", "phys", 10, 1.0, 1.0, device_id="dev-A")

        store.put_chunks("/f.bin", [
            ChunkRef(index=0, chunk_ref="aa/x_c0000", source_id="s1",
                     device_id="dev-A", size=100, hash="aa" * 32),
            ChunkRef(index=1, chunk_ref="bb/y_c0001", source_id="s1",
                     device_id="dev-A", size=50, hash="bb" * 32),
        ])
        store.commit()

        chunks = store.get_chunks("/f.bin")
        assert [c.index for c in chunks] == [0, 1]
        row = store._get_conn().execute(
            "SELECT chunked FROM files WHERE virtual_path = ?", ("/f.bin",)
        ).fetchone()
        assert row["chunked"] == 1

        # 더 짧은 매니페스트로 교체 → 옛 행이 남지 않는다.
        store.put_chunks("/f.bin", [
            ChunkRef(index=0, chunk_ref="cc/z_c0000", source_id="s1",
                     device_id="dev-A", size=10, hash="cc" * 32),
        ])
        store.commit()
        chunks = store.get_chunks("/f.bin")
        assert len(chunks) == 1
        assert chunks[0].chunk_ref == "cc/z_c0000"
        store.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_update_chunk_location_touches_single_chunk():
    """청크 하나의 위치만 갱신하고 나머지는 건드리지 않는다 (Requirement 2.3)."""
    from stardustlib.models import ChunkRef

    d = tempfile.mkdtemp()
    try:
        store = _store(d)
        store.insert("/f.bin", "s1", "phys", 10, 1.0, 1.0, device_id="dev-A")
        store.put_chunks("/f.bin", [
            ChunkRef(index=0, chunk_ref="aa/x_c0000", source_id="s1",
                     device_id="dev-A", size=100, hash="aa" * 32),
            ChunkRef(index=1, chunk_ref="bb/y_c0001", source_id="s1",
                     device_id="dev-A", size=50, hash="bb" * 32),
        ])
        store.commit()
        before = store.lookup("/f.bin").version

        store.update_chunk_location("/f.bin", 1, "s2", "dev-B")

        chunks = {c.index: c for c in store.get_chunks("/f.bin")}
        assert chunks[1].source_id == "s2" and chunks[1].device_id == "dev-B"
        assert chunks[0].source_id == "s1" and chunks[0].device_id == "dev-A"
        # 파일 레코드 자체는 재기록되지 않는다.
        assert store.lookup("/f.bin").version == before
        store.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_live_chunk_paths_scopes_to_device():
    """활성 청크 경로 집합은 이 디바이스 소유(또는 NULL)만 포함한다."""
    from stardustlib.models import ChunkRef

    d = tempfile.mkdtemp()
    try:
        store = _store(d)
        store.insert("/mine.bin", "s1", "p1", 10, 1.0, 1.0, device_id="dev-A")
        store.put_chunks("/mine.bin", [
            ChunkRef(index=0, chunk_ref="aa/mine_c0000", source_id="s1",
                     device_id="dev-A", size=10, hash="aa" * 32),
        ])
        store.insert("/theirs.bin", "s1", "p2", 10, 1.0, 1.0, device_id="dev-B")
        store.put_chunks("/theirs.bin", [
            ChunkRef(index=0, chunk_ref="bb/theirs_c0000", source_id="s1",
                     device_id="dev-B", size=10, hash="bb" * 32),
        ])
        store.commit()

        live = store.live_chunk_paths_for_device("dev-A")
        assert ("s1", "aa/mine_c0000") in live
        assert ("s1", "bb/theirs_c0000") not in live
        store.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
