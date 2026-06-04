"""files.replication_status 마이그레이션(v4) + set/get 테스트."""

import pytest

from stardustlib.metadata_store import MetadataStore


def _store(tmp_path) -> MetadataStore:
    store = MetadataStore(str(tmp_path / "m.db"), b"\x00" * 32)
    store.initialize()
    return store


def test_default_is_none(tmp_path):
    store = _store(tmp_path)
    store.insert("/a.txt", "src", "phys", 10, 1.0, 1.0)
    assert store.get_replication_status("/a.txt") == "none"
    store.close()


def test_set_and_get(tmp_path):
    store = _store(tmp_path)
    store.insert("/a.txt", "src", "phys", 10, 1.0, 1.0)
    store.set_replication_status("/a.txt", "pending")
    assert store.get_replication_status("/a.txt") == "pending"
    store.set_replication_status("/a.txt", "replicated")
    assert store.get_replication_status("/a.txt") == "replicated"
    store.close()


def test_invalid_status_raises(tmp_path):
    store = _store(tmp_path)
    store.insert("/a.txt", "src", "phys", 10, 1.0, 1.0)
    with pytest.raises(ValueError):
        store.set_replication_status("/a.txt", "bogus")
    store.close()


def test_get_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get_replication_status("/nope") is None
    store.close()


def test_migration_idempotent(tmp_path):
    # initialize를 두 번 호출해도 v4 마이그레이션이 안전해야 한다.
    store = _store(tmp_path)
    store.initialize()
    store.insert("/a.txt", "src", "phys", 10, 1.0, 1.0)
    assert store.get_replication_status("/a.txt") == "none"
    store.close()
