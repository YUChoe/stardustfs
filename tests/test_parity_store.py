"""패리티 스토어(호스트 역할) + P2P replica op 인가 테스트."""
from __future__ import annotations

import base64

import pytest

from stardustlib.parity_store import ParityStore, QuotaExceededError


# --- ParityStore 단위 ---

def test_store_fetch_roundtrip(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"))
    ps.store("chunk-1", "owner-a", b"ciphertext-bytes")
    assert ps.exists("chunk-1")
    assert ps.owner_of("chunk-1") == "owner-a"
    assert ps.fetch("chunk-1", "owner-a") == b"ciphertext-bytes"
    assert ps.used_bytes() == len(b"ciphertext-bytes")


def test_fetch_only_owner(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"))
    ps.store("c", "owner-a", b"data")
    with pytest.raises(PermissionError):
        ps.fetch("c", "intruder-b")


def test_fetch_missing(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"))
    with pytest.raises(FileNotFoundError):
        ps.fetch("nope", "owner-a")


def test_delete_only_owner_then_idempotent(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"))
    ps.store("c", "owner-a", b"data")
    with pytest.raises(PermissionError):
        ps.delete("c", "intruder-b")
    ps.delete("c", "owner-a")
    assert not ps.exists("c")
    # 멱등: 없는 청크 삭제는 무동작
    ps.delete("c", "owner-a")


def test_store_rejects_other_owner_overwrite(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"))
    ps.store("c", "owner-a", b"data")
    with pytest.raises(PermissionError):
        ps.store("c", "owner-b", b"hijack")


def test_quota_enforced(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"), max_bytes=10)
    ps.store("c1", "owner-a", b"12345")  # 5 bytes
    with pytest.raises(QuotaExceededError):
        ps.store("c2", "owner-a", b"123456")  # 5 + 6 = 11 > 10
    # 한도 내 재저장(덮어쓰기로 용량 감소)은 허용
    ps.store("c1", "owner-a", b"1")
    ps.store("c2", "owner-a", b"123456")
    assert ps.used_bytes() == 7


def test_invalid_chunk_id_rejected(tmp_path):
    ps = ParityStore(str(tmp_path / "parity"))
    for bad in ("", "a/b", "a\\b", "../escape"):
        with pytest.raises(ValueError):
            ps.store(bad, "owner-a", b"x")


def test_index_persists_across_reopen(tmp_path):
    base = str(tmp_path / "parity")
    ps = ParityStore(base)
    ps.store("c", "owner-a", b"persisted")
    ps2 = ParityStore(base)
    assert ps2.owner_of("c") == "owner-a"
    assert ps2.fetch("c", "owner-a") == b"persisted"


# --- P2P replica op 인가 로직 ---

def _server_with_parity(tmp_path):
    from unittest.mock import MagicMock

    from stardustlib.auth_client import AuthClient
    from stardustlib.p2p_server import P2PServer

    auth = MagicMock(spec=AuthClient)
    auth.user_id = "host-self"
    storage_pool = MagicMock()
    ps = ParityStore(str(tmp_path / "parity"), max_bytes=20)
    return P2PServer(storage_pool, auth, 9999, "http://localhost:8000", parity_store=ps)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_op_replica_store_fetch_delete(tmp_path):
    srv = _server_with_parity(tmp_path)
    body = {"chunk_id": "k", "data": _b64(b"cipher")}
    assert srv._op_replica_store(body, "owner-x")[0] == 200

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
