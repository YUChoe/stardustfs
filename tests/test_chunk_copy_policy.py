"""청크 3카피 정책 (스펙 chunk-copy-policy).

카피 위치 다중화(v8 스키마), 위치 CRUD, 카피 수·기기 수 판정을 검증한다.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from stardustlib.chunk_location import (
    ChunkLocation,
    copies,
    distinct_devices,
    has_location,
)
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import ChunkRef

KEY = b"k" * 32


def _store(tmp_path, name: str = "meta.db") -> MetadataStore:
    store = MetadataStore(str(tmp_path / name), KEY)
    store.initialize()
    return store


def _add_file(store: MetadataStore, vpath: str, source_id: str = "s1") -> None:
    store.insert(
        vpath, source_id, f"phys/{vpath.strip('/')}", 10, 1.0, 1.0,
        device_id="devA",
    )


# --- 카피 위치 모델 ---

def test_distinct_devices_counts_devices_not_copies():
    """카피 3개가 한 기기에 몰리면 카피 수 3, 기기 수 1이다(Property 6)."""
    locs = [
        ChunkLocation("devA", "s1"),
        ChunkLocation("devA", "s2"),
        ChunkLocation("devA", "s3"),
    ]
    assert copies(locs) == 3
    assert distinct_devices(locs) == 1

    locs.append(ChunkLocation("devB", "s1"))
    assert copies(locs) == 4
    assert distinct_devices(locs) == 2


def test_has_location_identifies_used_source():
    locs = [ChunkLocation("devA", "s1"), ChunkLocation("devB", "s2")]
    assert has_location(locs, "devA", "s1") is True
    assert has_location(locs, "devA", "s2") is False


def test_legacy_location_counts_as_one_device():
    """device_id를 모르는 레거시 위치도 기기 하나로 센다."""
    locs = [ChunkLocation("", "s1"), ChunkLocation("", "s2")]
    assert distinct_devices(locs) == 1
    assert copies(locs) == 2


# --- v8 스키마: 위치 CRUD ---

def test_add_chunk_location_is_idempotent(tmp_path):
    """같은 위치를 다시 등록해도 카피가 늘지 않는다."""
    store = _store(tmp_path)
    _add_file(store, "/a.bin")
    store.put_chunks("/a.bin", [
        ChunkRef(index=0, chunk_ref="00/abc_c0000", source_id="s1",
                 device_id="devA", size=100, hash="h0"),
    ])
    store._get_conn().commit()

    loc = ChunkLocation("devB", "s9", chunk_ref=None, kind="parity")
    store.add_chunk_location("/a.bin", 0, loc, 100, "h0")
    store.add_chunk_location("/a.bin", 0, loc, 100, "h0")

    locations = store.get_chunk_locations("/a.bin")[0]
    assert copies(locations) == 2
    assert distinct_devices(locations) == 2
    assert {(loc.device_id, loc.source_id) for loc in locations} == {
        ("devA", "s1"), ("devB", "s9"),
    }
    store.close()


def test_remove_chunk_location_keeps_other_copies(tmp_path):
    store = _store(tmp_path)
    _add_file(store, "/b.bin")
    store.put_chunks("/b.bin", [
        ChunkRef(index=0, chunk_ref="00/abc_c0000", source_id="s1",
                 device_id="devA", size=100),
    ])
    store._get_conn().commit()
    store.add_chunk_location(
        "/b.bin", 0, ChunkLocation("devB", "s2", "00/abc_c0000"), 100
    )

    store.remove_chunk_location("/b.bin", 0, "devA", "s1")
    locations = store.get_chunk_locations("/b.bin")[0]
    assert [(loc.device_id, loc.source_id) for loc in locations] == [
        ("devB", "s2")
    ]
    store.close()


def test_get_chunks_returns_manifest_row_per_index(tmp_path):
    """get_chunks는 청크당 한 줄(먼저 등록된 위치)만 돌려준다(기존 호출부 호환)."""
    store = _store(tmp_path)
    _add_file(store, "/c.bin")
    store.put_chunks("/c.bin", [
        ChunkRef(index=0, chunk_ref="00/a_c0000", source_id="s1",
                 device_id="devA", size=100),
        ChunkRef(index=1, chunk_ref="00/a_c0001", source_id="s1",
                 device_id="devA", size=50),
    ])
    store._get_conn().commit()
    store.add_chunk_location(
        "/c.bin", 0, ChunkLocation("devB", "s2", "00/a_c0000"), 100
    )

    chunks = store.get_chunks("/c.bin")
    assert [(c.index, c.source_id, c.device_id) for c in chunks] == [
        (0, "s1", "devA"), (1, "s1", "devA"),
    ]
    store.close()


def test_update_chunk_location_moves_one_copy(tmp_path):
    """카피 하나가 자리를 옮기면 카피 수는 그대로다(스필오버·evacuate)."""
    store = _store(tmp_path)
    _add_file(store, "/d.bin")
    store.put_chunks("/d.bin", [
        ChunkRef(index=0, chunk_ref="00/a_c0000", source_id="s1",
                 device_id="devA", size=100),
    ])
    store._get_conn().commit()
    store.add_chunk_location(
        "/d.bin", 0, ChunkLocation("devB", "s2", "00/a_c0000"), 100
    )

    # devA/s1 카피를 devA/s3로 옮긴다
    store.update_chunk_location(
        "/d.bin", 0, "s3", "devA", chunk_ref="11/a_c0000",
        from_device_id="devA", from_source_id="s1",
    )
    locations = store.get_chunk_locations("/d.bin")[0]
    assert copies(locations) == 2
    assert ("devA", "s3") in {(x.device_id, x.source_id) for x in locations}
    assert ("devA", "s1") not in {(x.device_id, x.source_id) for x in locations}
    store.close()


def test_update_chunk_location_absorbs_duplicate_destination(tmp_path):
    """목적지에 이미 카피가 있으면 합쳐진다(같은 소스에 2카피 금지, Property 2)."""
    store = _store(tmp_path)
    _add_file(store, "/e.bin")
    store.put_chunks("/e.bin", [
        ChunkRef(index=0, chunk_ref="00/a_c0000", source_id="s1",
                 device_id="devA", size=100),
    ])
    store._get_conn().commit()
    store.add_chunk_location(
        "/e.bin", 0, ChunkLocation("devA", "s2", "00/a_c0000"), 100
    )

    store.update_chunk_location(
        "/e.bin", 0, "s2", "devA",
        from_device_id="devA", from_source_id="s1",
    )
    locations = store.get_chunk_locations("/e.bin")[0]
    assert [(x.device_id, x.source_id) for x in locations] == [("devA", "s2")]
    store.close()


# --- v7 → v8 마이그레이션 ---

def _downgrade_to_v7(db_path: str) -> None:
    """v8 DB를 v7 스키마(청크당 위치 1개)로 되돌린다(마이그레이션 입력 생성)."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE file_chunks_v7 (
            virtual_path  TEXT    NOT NULL,
            chunk_index   INTEGER NOT NULL,
            chunk_ref     TEXT    NOT NULL,
            source_id     TEXT    NOT NULL,
            device_id     TEXT,
            size          INTEGER NOT NULL,
            hash          TEXT,
            PRIMARY KEY (virtual_path, chunk_index)
        );
        INSERT INTO file_chunks_v7
            (virtual_path, chunk_index, chunk_ref, source_id, device_id,
             size, hash)
            SELECT virtual_path, chunk_index, chunk_ref, source_id,
                   NULLIF(device_id, ''), size, hash FROM file_chunks;
        DROP TABLE file_chunks;
        ALTER TABLE file_chunks_v7 RENAME TO file_chunks;
        DROP TABLE IF EXISTS hosted_chunks;
        UPDATE schema_version SET version = 7 WHERE id = 1;
    """)
    conn.commit()
    conn.close()


def _make_v7_db(tmp_path) -> str:
    """레거시(NULL device_id) 청크 행을 가진 v7 DB를 만든다."""
    store = _store(tmp_path, "legacy.db")
    _add_file(store, "/legacy.bin")
    store.put_chunks("/legacy.bin", [
        ChunkRef(index=0, chunk_ref="00/a_c0000", source_id="s1",
                 device_id=None, size=100, hash="h0"),
        ChunkRef(index=1, chunk_ref="00/a_c0001", source_id="s1",
                 device_id=None, size=40, hash="h1"),
    ])
    store._get_conn().commit()
    path = store._db_path
    store.close()
    _downgrade_to_v7(path)
    return path


def test_v8_migration_moves_rows_to_single_location(tmp_path):
    """기존 청크 행은 위치 1개로 이관되고 백업(.v7.bak)이 남는다."""
    path = _make_v7_db(tmp_path)

    store = MetadataStore(path, KEY)
    store.initialize()

    assert os.path.exists(path + ".v7.bak")
    locations = store.get_chunk_locations("/legacy.bin")
    assert copies(locations[0]) == 1
    assert copies(locations[1]) == 1
    # NULL device_id는 빈 문자열(이 기기)로 채워지고 조회 시 None으로 되돌아온다
    assert locations[0][0].device_id == ""
    assert locations[0][0].kind == "source"
    assert store.get_chunks("/legacy.bin")[0].device_id is None
    # 이관 후에는 같은 청크에 위치를 더할 수 있다(v7 PK에서는 불가)
    store.add_chunk_location(
        "/legacy.bin", 0, ChunkLocation("devB", "s2", "00/a_c0000"), 100, "h0"
    )
    assert copies(store.get_chunk_locations("/legacy.bin")[0]) == 2
    # hosted_chunks가 함께 생성된다
    row = store._get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='hosted_chunks'"
    ).fetchone()
    assert row is not None
    store.close()


def test_v8_migration_is_idempotent(tmp_path):
    path = _make_v7_db(tmp_path)
    for _ in range(2):
        store = MetadataStore(path, KEY)
        store.initialize()
        assert copies(store.get_chunk_locations("/legacy.bin")[0]) == 1
        store.close()


def test_v8_migration_aborts_when_backup_fails(tmp_path, monkeypatch):
    """백업 실패 시 스키마를 바꾸지 않고 기동을 중단한다(예외)."""
    path = _make_v7_db(tmp_path)

    import stardustlib.metadata_store as ms

    def _boom(src, dst):
        raise OSError("no space")

    monkeypatch.setattr(ms.shutil, "copy2", _boom)
    store = MetadataStore(path, KEY)
    with pytest.raises(OSError):
        store.initialize()
    store.close()

    # 스키마는 v7 그대로다(반쯤 바뀐 상태로 남지 않는다)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(file_chunks)")}
    version = conn.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0]
    conn.close()
    assert "kind" not in cols
    assert version == 7


# --- 3카피 배치 (Phase 3) ---

class _Registry:
    """서버 레지스트리 인메모리 대역(카피 위치를 기기+소스로 센다)."""

    def __init__(self, holders: list[str] | None = None) -> None:
        self.holders = holders or []
        self.chunks: dict[str, list[dict]] = {}
        self.replicas: dict[str, list[tuple[str, str]]] = {}
        self.stored: dict[str, dict[str, bytes]] = {}
        self.store_fail: set[str] = set()
        self.record_fail: set[str] = set()
        self.placement_calls: list[dict] = []

    def attach(self, mgr) -> None:
        reg = self

        async def register_chunk(token, chunk_id, file_ref, idx, size,
                                 chunk_hash=None):
            rows = reg.chunks.setdefault(file_ref, [])
            for row in rows:
                if row["idx"] == idx:
                    row.update(size=size, hash=chunk_hash)
                    return
            rows.append({
                "chunk_id": chunk_id, "idx": idx, "size": size,
                "hash": chunk_hash,
            })

        async def placement(token, size, exclude, exclude_locations=None):
            reg.placement_calls.append({
                "exclude": list(exclude),
                "exclude_locations": list(exclude_locations or []),
            })
            return [
                {"device_id": h, "connection_address": h}
                for h in reg.holders if h not in exclude
            ]

        async def holder_store(device_id, address, chunk_id, data, token):
            if device_id in reg.store_fail:
                return False, ""
            reg.stored.setdefault(device_id, {})[chunk_id] = data
            return True, f"vol-{device_id}"

        async def record_replica(token, chunk_id, device_id, source_id=""):
            if device_id in reg.record_fail:
                return False
            entry = (device_id, source_id)
            if entry not in reg.replicas.setdefault(chunk_id, []):
                reg.replicas[chunk_id].append(entry)
            return True

        async def remove_replica(token, chunk_id, device_id, source_id):
            try:
                reg.replicas.get(chunk_id, []).remove((device_id, source_id))
            except ValueError:
                return False
            return True

        async def list_chunks(token, file_ref):
            return sorted(reg.chunks.get(file_ref, []), key=lambda c: c["idx"])

        async def list_replicas(token, chunk_id):
            return [
                {"device_id": d, "source_id": sid, "connection_address": d,
                 "is_online": True}
                for d, sid in reg.replicas.get(chunk_id, [])
            ]

        async def holder_fetch(device_id, address, chunk_id, token):
            return reg.stored.get(device_id, {}).get(chunk_id)

        mgr._register_chunk = register_chunk
        mgr._placement = placement
        mgr._holder_store = holder_store
        mgr._record_replica = record_replica
        mgr._remove_replica = remove_replica
        mgr._list_chunks = list_chunks
        mgr._list_replicas = list_replicas
        mgr._holder_fetch = holder_fetch

    def only_chunk_id(self) -> str:
        """등록된 청크가 하나인 테스트에서 그 chunk_id를 돌려준다."""
        rows = [row for rows in self.chunks.values() for row in rows]
        assert len(rows) == 1
        return rows[0]["chunk_id"]

    def locations_of(self, chunk_id: str) -> list[tuple[str, str]]:
        return list(self.replicas.get(chunk_id, []))


def _pool(tmp_path, source_ids, device_id="devA"):
    """실제 DirectorySource 기반 StoragePool + MetadataStore."""
    from stardustlib.encryption_engine import EncryptionEngine
    from stardustlib.storage_pool import StoragePool
    from stardustlib.storage_source import DirectorySource

    store = _store(tmp_path)
    sources = []
    for source_id in source_ids:
        path = tmp_path / source_id
        path.mkdir()
        source = DirectorySource(source_id, str(path))
        source.initialize()
        sources.append(source)
    pool = StoragePool(
        sources, store, EncryptionEngine(b"e" * 32), device_id=device_id
    )
    return pool, store


def _manager(pool, store, holders=(), target_copies=3):
    from stardustlib.replication_manager import ReplicationManager

    class _Auth:
        user_id = "user-1"

        async def get_valid_token(self):
            return "tok"

    mgr = ReplicationManager(
        _Auth(), "http://server", store, pool, target_copies=target_copies,
    )
    reg = _Registry(list(holders))
    reg.attach(mgr)
    return mgr, reg


def test_three_copies_go_to_distinct_local_sources(tmp_path):
    """다른 기기 후보가 없으면 같은 기기의 서로 다른 소스 3곳에 둔다(Req 2.3)."""
    pool, store = _pool(tmp_path, ["s1", "s2", "s3"])
    pool.write_file("/f.bin", b"payload" * 10)
    mgr, reg = _manager(pool, store, holders=[])  # 다른 기기 후보 없음
    try:
        result = mgr.replicate("/f.bin")
    finally:
        mgr.close()

    assert result.status == "replicated"
    assert result.copies_per_chunk == [3]
    # 카피 3개가 한 기기에 몰린 상태 → 기기 수 1(Property 6)
    assert result.devices_per_chunk == [1]
    locations = store.get_chunk_locations("/f.bin")[0]
    assert copies(locations) == 3
    assert distinct_devices(locations) == 1
    # 같은 소스에 두 번 두지 않는다(Property 2)
    assert sorted(loc.source_id for loc in locations) == ["s1", "s2", "s3"]


def test_single_source_yields_one_copy_and_pending(tmp_path):
    """소스가 하나뿐이면 카피 1개만 두고 미달(pending)로 남긴다."""
    pool, store = _pool(tmp_path, ["s1"])
    pool.write_file("/f.bin", b"data")
    mgr, _reg = _manager(pool, store, holders=[])
    try:
        result = mgr.replicate("/f.bin")
    finally:
        mgr.close()

    assert result.status == "pending"
    assert result.copies_per_chunk == [1]
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 1


def test_other_devices_preferred_over_local_sources(tmp_path):
    """다른 기기 후보가 있으면 같은 기기의 소스를 쓰지 않는다(Property 3)."""
    pool, store = _pool(tmp_path, ["s1", "s2", "s3"])
    pool.write_file("/f.bin", b"data" * 20)
    mgr, reg = _manager(pool, store, holders=["h1", "h2"])
    try:
        result = mgr.replicate("/f.bin")
    finally:
        mgr.close()

    assert result.status == "replicated"
    assert result.copies_per_chunk == [3]
    # 로컬 카피 1개 + 다른 기기 2곳 → 기기 수 3
    assert result.devices_per_chunk == [3]
    # 로컬에는 카피가 하나뿐이다(추가 로컬 카피를 만들지 않았다)
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 1
    assert sorted(reg.locations_of(reg.only_chunk_id())) == [
        ("devA", "s1"), ("h1", "vol-h1"), ("h2", "vol-h2"),
    ]


def test_target_locations_skips_used_locations(tmp_path):
    """이미 카피가 있는 소스·기기는 다시 고르지 않는다."""
    pool, store = _pool(tmp_path, ["s1", "s2"])
    mgr, _reg = _manager(pool, store, holders=[])
    try:
        known = [ChunkLocation("devA", "s1", kind="source")]
        targets = mgr._target_locations(0, known, [], self_dev="devA")
        assert [t["source_id"] for t in targets] == ["s2"]

        # 다른 기기 후보가 있으면 로컬을 쓰지 않는다
        targets = mgr._target_locations(
            0, known,
            [{"device_id": "h1", "connection_address": "h1"}], self_dev="devA",
        )
        assert targets == [
            {"device_id": "h1", "connection_address": "h1", "local": False}
        ]

        # 이미 그 기기에 카피가 있으면 후보에서 빠진다 → 로컬로 내려온다
        targets = mgr._target_locations(
            0, known + [ChunkLocation("h1", "vol-h1", kind="parity")],
            [{"device_id": "h1", "connection_address": "h1"}], self_dev="devA",
        )
        assert [t.get("source_id") for t in targets] == ["s2"]
    finally:
        mgr.close()


def test_target_locations_returns_nothing_when_target_met(tmp_path):
    pool, store = _pool(tmp_path, ["s1", "s2", "s3"])
    mgr, _reg = _manager(pool, store, holders=["h1"])
    try:
        known = [
            ChunkLocation("devA", "s1"),
            ChunkLocation("h1", "vol-h1", kind="parity"),
            ChunkLocation("h2", "vol-h2", kind="parity"),
        ]
        assert mgr._target_locations(0, known, [], self_dev="devA") == []
    finally:
        mgr.close()


# --- 카피 이전 (Phase 4) ---

def test_heal_relocates_copy_to_new_device(tmp_path):
    """기기를 추가하면 몰려 있던 로컬 카피 하나가 그 기기로 옮겨진다(Req 3)."""
    pool, store = _pool(tmp_path, ["s1", "s2", "s3"])
    pool.write_file("/f.bin", b"relocate" * 10)
    mgr, reg = _manager(pool, store, holders=[])
    try:
        assert mgr.replicate("/f.bin").devices_per_chunk == [1]
        chunk_id = reg.only_chunk_id()
        assert len(reg.locations_of(chunk_id)) == 3

        # 새 기기가 등장 → heal이 카피 하나를 옮긴다
        reg.holders = ["h1"]
        report = mgr.ensure_replicas("/f.bin")
    finally:
        mgr.close()

    assert report.relocated == 1
    locations = reg.locations_of(chunk_id)
    # 총 카피 수는 3을 유지한다(Property 5·Req 3.5)
    assert len(locations) == 3
    assert ("h1", "vol-h1") in locations
    # 로컬 카피 하나가 사라졌다(소스 2곳만 남는다)
    local = [sid for dev, sid in locations if dev == "devA"]
    assert len(local) == 2
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 2


def test_relocate_keeps_local_copy_when_store_fails(tmp_path):
    """이전 중 새 위치 저장이 실패하면 로컬 카피를 지우지 않는다(Property 5)."""
    pool, store = _pool(tmp_path, ["s1", "s2", "s3"])
    pool.write_file("/f.bin", b"keep" * 10)
    mgr, reg = _manager(pool, store, holders=[])
    try:
        mgr.replicate("/f.bin")
        chunk_id = reg.only_chunk_id()
        reg.holders = ["h1"]
        reg.store_fail = {"h1"}
        report = mgr.ensure_replicas("/f.bin")
    finally:
        mgr.close()

    assert report.relocated == 0
    assert len(reg.locations_of(chunk_id)) == 3
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 3


def test_relocate_keeps_local_copy_when_registry_fails(tmp_path):
    """서버 등록이 실패하면 로컬 카피를 남기고 다음 주기에 재시도한다."""
    pool, store = _pool(tmp_path, ["s1", "s2", "s3"])
    pool.write_file("/f.bin", b"keep" * 10)
    mgr, reg = _manager(pool, store, holders=[])
    try:
        mgr.replicate("/f.bin")
        chunk_id = reg.only_chunk_id()
        reg.holders = ["h1"]
        reg.record_fail = {"h1"}
        report = mgr.ensure_replicas("/f.bin")
    finally:
        mgr.close()

    assert report.relocated == 0
    # 등록이 안 됐으므로 레지스트리는 로컬 3카피 그대로
    assert len(reg.locations_of(chunk_id)) == 3
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 3


def test_heal_reads_local_copy_without_remote_roundtrip(tmp_path):
    """로컬 카피가 있으면 원격에서 받아오지 않는다(Phase 4.1)."""
    pool, store = _pool(tmp_path, ["s1", "s2"])
    pool.write_file("/f.bin", b"local-first" * 5)
    mgr, reg = _manager(pool, store, holders=["h1"])
    fetches: list[str] = []

    async def _fetch(device_id, address, chunk_id, token):
        fetches.append(device_id)
        return reg.stored.get(device_id, {}).get(chunk_id)

    mgr._holder_fetch = _fetch
    try:
        mgr.replicate("/f.bin")   # 로컬 1 + h1 1 = 2카피(목표 3 미달)
        reg.holders = ["h1", "h2"]
        mgr.ensure_replicas("/f.bin")
    finally:
        mgr.close()

    assert fetches == []  # 원격 왕복 없음


# --- 읽기 경로 (Phase 5) ---

def test_read_falls_back_to_other_copy(tmp_path):
    """로컬 카피가 사라져도 다른 카피로 읽기가 성공한다(Property 7)."""
    pool, store = _pool(tmp_path, ["s1", "s2"])
    content = b"read-me" * 100
    pool.write_file("/f.bin", content)
    mgr, _reg = _manager(pool, store, holders=[])
    try:
        mgr.replicate("/f.bin")  # s1 + s2 두 곳에 카피
    finally:
        mgr.close()

    locations = store.get_chunk_locations("/f.bin")[0]
    assert copies(locations) == 2
    # 매니페스트가 가리키는 카피(s1)를 소스에서 지운다
    manifest = store.get_chunks("/f.bin")[0]
    pool._get_source_by_id(manifest.source_id).delete(manifest.chunk_ref)

    assert pool.read_file("/f.bin") == content


def test_read_raises_when_no_copy_is_reachable(tmp_path):
    """도달 가능한 카피가 없으면 청크를 명시한 오류를 낸다."""
    pool, store = _pool(tmp_path, ["s1", "s2"])
    pool.write_file("/f.bin", b"gone")
    mgr, _reg = _manager(pool, store, holders=[])
    try:
        mgr.replicate("/f.bin")
    finally:
        mgr.close()

    for loc in store.get_chunk_locations("/f.bin")[0]:
        pool._get_source_by_id(loc.source_id).delete(loc.chunk_ref)

    with pytest.raises(OSError) as ei:
        pool.read_file("/f.bin")
    assert "chunk_index=0" in str(ei.value)


def test_store_and_delete_chunk_copy_roundtrip(tmp_path):
    """카피 추가 기록·읽기·삭제가 위치 등록과 함께 동작한다."""
    pool, store = _pool(tmp_path, ["s1", "s2"])
    pool.write_file("/f.bin", b"copy-me")
    manifest = store.get_chunks("/f.bin")[0]
    data = pool.read_chunk_copy("/f.bin", 0)

    written = pool.store_chunk_copy(
        "/f.bin", 0, data, manifest.chunk_ref, manifest.hash
    )
    assert written == "s2"
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 2

    # 이미 카피가 있는 소스는 다시 쓰지 않는다(Property 2)
    assert pool.store_chunk_copy(
        "/f.bin", 0, data, manifest.chunk_ref, manifest.hash
    ) is None

    pool.delete_chunk_copy("/f.bin", 0, "s2")
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 1
    # 마지막 카피는 지우지 않는다
    pool.delete_chunk_copy("/f.bin", 0, "s1")
    assert copies(store.get_chunk_locations("/f.bin")[0]) == 1


def test_hosted_chunks_count_toward_available_space(tmp_path):
    """보관 청크가 소스 용량에 집계된다(Property 8)."""
    from stardustlib.parity_store import ParityStore

    pool, store = _pool(tmp_path, ["s1"])
    parity = ParityStore(pool, store, None)
    before = pool.get_available_space()
    parity.store("h" * 64, "owner-b", b"x" * 200_000)

    assert store.hosted_bytes() == 200_000
    assert parity.used_bytes() == 200_000
    # 소스에 실제로 기록되므로 남은 공간이 줄어든다(별도 디렉토리가 아니다)
    assert pool.get_available_space() < before


# --- 축출 판정 (Phase 4b) ---

class _HealthStub:
    def __init__(self, min_copies: int, min_devices: int) -> None:
        self.degraded = False
        self.chunk_count = 1
        self.min_copies = min_copies
        self.min_devices = min_devices


class _MgrStub:
    def __init__(self, health, target_copies: int = 3) -> None:
        self.target_copies = target_copies
        self._health = health

    def replication_health(self, virtual_path: str):
        if isinstance(self._health, Exception):
            raise self._health
        return self._health


def test_eviction_skips_chunks_whose_copies_are_all_local():
    """카피 3개가 모두 이 기기에 있으면 축출하지 않는다(Property 4)."""
    from stardustfs import _eviction_safe

    mgr = _MgrStub(_HealthStub(min_copies=3, min_devices=1))
    assert _eviction_safe(mgr, "/f") is False


def test_eviction_allows_when_copies_spread_across_devices():
    """카피가 서로 다른 기기에 목표 수만큼 있으면 로컬을 비울 수 있다."""
    from stardustfs import _eviction_safe

    mgr = _MgrStub(_HealthStub(min_copies=3, min_devices=3))
    assert _eviction_safe(mgr, "/f") is True


def test_eviction_skips_when_devices_below_target():
    """기기 수가 목표 미만이면 보존한다(비우면 내구성이 떨어진다)."""
    from stardustfs import _eviction_safe

    mgr = _MgrStub(_HealthStub(min_copies=3, min_devices=2))
    assert _eviction_safe(mgr, "/f") is False


def test_eviction_preserves_when_health_unknown():
    """건강성을 확인할 수 없으면(오프라인 등) 보존한다."""
    from stardustfs import _eviction_safe

    mgr = _MgrStub(RuntimeError("offline"))
    assert _eviction_safe(mgr, "/f") is False


def test_read_falls_back_to_other_device_copy(tmp_path):
    """로컬 카피가 모두 사라져도 다른 기기의 카피로 읽는다(Property 7)."""
    pool, store = _pool(tmp_path, ["s1"])
    content = b"cross-device" * 50
    pool.write_file("/f.bin", content)
    manifest = store.get_chunks("/f.bin")[0]
    payload = pool.read_chunk_copy("/f.bin", 0)

    # 다른 기기(devB)에 카피가 있다고 등록한다
    store.add_chunk_location(
        "/f.bin", 0,
        ChunkLocation("devB", "vol-b", manifest.chunk_ref, kind="source"),
        manifest.size, manifest.hash,
    )

    class _Remote:
        is_active = True

        def read_from_source(self, chunk_ref, source_id):
            assert (chunk_ref, source_id) == (manifest.chunk_ref, "vol-b")
            return payload

    pool.register_remote_device("devB", _Remote())
    # 로컬 카피를 지운다 → devB 카피로 읽혀야 한다
    pool._get_source_by_id("s1").delete(manifest.chunk_ref)

    assert pool.read_file("/f.bin") == content
