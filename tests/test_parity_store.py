"""패리티 스토어(호스트 역할) + P2P replica op 인가 테스트.

보관 청크는 별도 디렉토리가 아니라 스토리지 소스에 놓이고, 인덱스는 메타데이터 DB의
hosted_chunks다(스펙 chunk-copy-policy Requirement 6).
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from stardustlib.metadata_store import MetadataStore
from stardustlib.parity_store import ParityStore, QuotaExceededError
from stardustlib.storage_pool import StoragePool
from stardustlib.storage_source import DirectorySource


def _pool(tmp_path, names=("src-1",)):
    """DirectorySource 기반 StoragePool + MetadataStore를 만든다."""
    store = MetadataStore(str(tmp_path / "meta.db"), b"k" * 32)
    store.initialize()
    sources = []
    for name in names:
        path = tmp_path / name
        path.mkdir()
        source = DirectorySource(name, str(path))
        source.initialize()
        sources.append(source)
    return StoragePool(sources, store, None, device_id="dev-self"), store


def _parity(tmp_path, max_bytes: int | None = None, names=("src-1",)):
    pool, store = _pool(tmp_path, names)
    return ParityStore(pool, store, max_bytes), pool, store


# --- ParityStore 단위 ---

def test_store_fetch_roundtrip(tmp_path):
    ps, _pool_, store = _parity(tmp_path)
    source_id = ps.store("chunk-1", "owner-a", b"ciphertext-bytes")
    assert source_id == "src-1"
    assert ps.exists("chunk-1")
    assert ps.owner_of("chunk-1") == "owner-a"
    assert ps.fetch("chunk-1", "owner-a") == b"ciphertext-bytes"
    assert ps.used_bytes() == len(b"ciphertext-bytes")
    # DB 집계와 인덱스가 일치한다(Property 8)
    assert store.hosted_bytes() == len(b"ciphertext-bytes")
    row = store.get_hosted_chunk("chunk-1")
    assert row["source_id"] == "src-1"
    assert row["physical_path"].startswith("p/")


def test_chunk_bytes_live_in_storage_source(tmp_path):
    """청크는 소스 안에 놓이고 소스 용량 집계에 반영된다(Property 8)."""
    ps, pool, _store = _parity(tmp_path)
    ps.store("chunk-1", "owner-a", b"x" * 1000)
    source = pool.sources[0]
    names = source.list_physical_files()
    assert any(name.endswith("chunk-1") for name in names)
    assert source.exists(f"p/{names[0].split('/')[1]}/chunk-1") or names


def test_fetch_only_owner(tmp_path):
    ps, _p, _s = _parity(tmp_path)
    ps.store("c", "owner-a", b"data")
    with pytest.raises(PermissionError):
        ps.fetch("c", "intruder-b")


def test_fetch_missing(tmp_path):
    ps, _p, _s = _parity(tmp_path)
    with pytest.raises(FileNotFoundError):
        ps.fetch("nope", "owner-a")


def test_delete_only_owner_then_idempotent(tmp_path):
    ps, pool, _s = _parity(tmp_path)
    ps.store("c", "owner-a", b"data")
    with pytest.raises(PermissionError):
        ps.delete("c", "intruder-b")
    ps.delete("c", "owner-a")
    assert not ps.exists("c")
    assert ps.used_bytes() == 0
    # 물리 바이트도 소스에서 사라진다
    assert not any(
        name.endswith("c") for name in pool.sources[0].list_physical_files()
    )
    # 멱등: 없는 청크 삭제는 무동작
    ps.delete("c", "owner-a")


def test_store_rejects_other_owner_overwrite(tmp_path):
    ps, _p, _s = _parity(tmp_path)
    ps.store("c", "owner-a", b"data")
    with pytest.raises(PermissionError):
        ps.store("c", "owner-b", b"hijack")


def test_quota_enforced(tmp_path):
    ps, _p, _s = _parity(tmp_path, max_bytes=10)
    ps.store("c1", "owner-a", b"12345")  # 5 bytes
    with pytest.raises(QuotaExceededError):
        ps.store("c2", "owner-a", b"123456")  # 5 + 6 = 11 > 10
    # 한도 내 재저장(덮어쓰기로 용량 감소)은 허용
    ps.store("c1", "owner-a", b"1")
    ps.store("c2", "owner-a", b"123456")
    assert ps.used_bytes() == 7


def test_source_space_shortage_is_quota_error(tmp_path):
    """소스에 놓을 공간이 없으면 쿼터 초과와 같은 오류다(→ p2p 507)."""
    ps, pool, _s = _parity(tmp_path, max_bytes=None)
    for source in pool.sources:
        source.get_available_space = lambda: 10  # noqa: B023 — 테스트 스텁
    with pytest.raises(QuotaExceededError):
        ps.store("c", "owner-a", b"x" * 100)


def test_store_picks_source_with_most_space(tmp_path):
    """여유가 가장 많은 소스를 고른다."""
    ps, pool, _s = _parity(tmp_path, names=("src-1", "src-2"))
    pool.sources[0].get_available_space = lambda: 100
    pool.sources[1].get_available_space = lambda: 10_000
    assert ps.store("c", "owner-a", b"x" * 50) == "src-2"


def test_invalid_chunk_id_rejected(tmp_path):
    ps, _p, _s = _parity(tmp_path)
    for bad in ("", "a/b", "a\\b", "../escape"):
        with pytest.raises(ValueError):
            ps.store(bad, "owner-a", b"x")


def test_index_persists_across_reopen(tmp_path):
    """인덱스는 DB에 있으므로 새 ParityStore가 그대로 이어받는다."""
    ps, pool, store = _parity(tmp_path)
    ps.store("c", "owner-a", b"persisted")
    ps2 = ParityStore(pool, store, None)
    assert ps2.owner_of("c") == "owner-a"
    assert ps2.fetch("c", "owner-a") == b"persisted"


# --- 레거시 `.parity/` 이관 ---

def _legacy_dir(tmp_path, entries: dict) -> str:
    """구 버전 형식의 `.parity/` 디렉토리를 만든다."""
    base = tmp_path / "meta.db.parity"
    base.mkdir()
    index = {}
    for chunk_id, (owner, data) in entries.items():
        (base / f"{chunk_id}.bin").write_bytes(data)
        index[chunk_id] = {"owner": owner, "size": len(data)}
    (base / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return str(base)


def test_migrate_legacy_dir_moves_chunks_to_source(tmp_path):
    ps, pool, store = _parity(tmp_path)
    legacy = _legacy_dir(tmp_path, {
        "aa": ("owner-a", b"first"),
        "bb": ("owner-b", b"second-chunk"),
    })

    report = ps.migrate_legacy_dir(legacy)

    assert report["moved"] == 2
    assert report["left"] == 0
    assert ps.fetch("aa", "owner-a") == b"first"
    assert ps.fetch("bb", "owner-b") == b"second-chunk"
    assert store.hosted_bytes() == len(b"first") + len(b"second-chunk")
    # 옮긴 파일과 인덱스 항목은 정리된다
    assert not os.path.exists(os.path.join(legacy, "aa.bin"))
    assert json.loads(
        open(os.path.join(legacy, "index.json"), encoding="utf-8").read()
    ) == {}


def test_migrate_legacy_dir_leaves_chunks_when_no_space(tmp_path, caplog):
    """공간이 부족하면 옮기지 못한 청크를 남기고 로그로 알린다(무손실 우선)."""
    ps, pool, _s = _parity(tmp_path)
    legacy = _legacy_dir(tmp_path, {"aa": ("owner-a", b"x" * 100)})
    for source in pool.sources:
        source.get_available_space = lambda: 10

    with caplog.at_level("WARNING"):
        report = ps.migrate_legacy_dir(legacy)

    assert report == {"moved": 0, "left": 1, "bytes": 0}
    assert os.path.exists(os.path.join(legacy, "aa.bin"))
    assert not ps.exists("aa")
    assert any("옮기지 못해" in rec.message for rec in caplog.records)


def test_migrate_legacy_dir_is_idempotent(tmp_path):
    ps, _p, _s = _parity(tmp_path)
    legacy = _legacy_dir(tmp_path, {"aa": ("owner-a", b"first")})
    assert ps.migrate_legacy_dir(legacy)["moved"] == 1
    assert ps.migrate_legacy_dir(legacy)["moved"] == 0
    assert ps.fetch("aa", "owner-a") == b"first"


def test_migrate_legacy_dir_without_index_is_noop(tmp_path):
    ps, _p, _s = _parity(tmp_path)
    assert ps.migrate_legacy_dir(str(tmp_path / "absent")) == {
        "moved": 0, "left": 0, "bytes": 0,
    }


# --- P2P replica op 인가 로직 ---

def _server_with_parity(tmp_path, max_bytes: int = 20):
    from unittest.mock import MagicMock

    from stardustlib.auth_client import AuthClient
    from stardustlib.p2p_server import P2PServer

    auth = MagicMock(spec=AuthClient)
    auth.user_id = "host-self"
    ps, pool, _store = _parity(tmp_path, max_bytes=max_bytes)
    return P2PServer(
        pool, auth, 9999, "http://localhost:8000", parity_store=ps
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_op_replica_store_fetch_delete(tmp_path):
    srv = _server_with_parity(tmp_path)
    body = {"chunk_id": "k", "data": _b64(b"cipher")}
    status, res = srv._op_replica_store(body, "owner-x")
    assert status == 200
    # 보관 소스를 알려줘야 요청자가 카피 위치를 서버에 등록할 수 있다
    assert res["source_id"] == "src-1"

    status, res = srv._op_replica_fetch({"chunk_id": "k"}, "owner-x")
    assert status == 200
    assert base64.b64decode(res["data"]) == b"cipher"

    assert srv._op_replica_delete({"chunk_id": "k"}, "owner-x")[0] == 200
    assert srv._op_replica_fetch({"chunk_id": "k"}, "owner-x")[0] == 404


def test_op_replica_fetch_rejects_non_owner(tmp_path):
    srv = _server_with_parity(tmp_path)
    srv._op_replica_store({"chunk_id": "k", "data": _b64(b"cipher")}, "owner-x")
    assert srv._op_replica_fetch({"chunk_id": "k"}, "other")[0] == 403
    assert srv._op_replica_delete({"chunk_id": "k"}, "other")[0] == 403


def test_op_replica_store_quota_exceeded(tmp_path):
    srv = _server_with_parity(tmp_path)  # max_bytes=20
    assert srv._op_replica_store(
        {"chunk_id": "k", "data": _b64(b"x" * 30)}, "owner-x"
    )[0] == 507


def test_op_replica_disabled_when_no_store():
    from unittest.mock import MagicMock

    from stardustlib.auth_client import AuthClient
    from stardustlib.p2p_server import P2PServer

    auth = MagicMock(spec=AuthClient)
    auth.user_id = "host-self"
    srv = P2PServer(MagicMock(), auth, 9999, "http://localhost:8000")
    assert srv._op_replica_store({"chunk_id": "k", "data": ""}, "x")[0] == 503
    assert srv._op_replica_fetch({"chunk_id": "k"}, "x")[0] == 503
    assert srv._op_replica_delete({"chunk_id": "k"}, "x")[0] == 503
