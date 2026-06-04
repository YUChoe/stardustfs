"""CredentialStore 단위 테스트."""

import json
import os

import pytest

from stardustlib.credential_store import (
    CredentialStore,
    CredentialStoreError,
    file_lock,
)


def _store(tmp_path) -> CredentialStore:
    return CredentialStore(str(tmp_path / "meta.db"))


def test_load_none_when_absent(tmp_path):
    assert _store(tmp_path).load() is None


def test_save_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    data = {
        "version": 1,
        "server_url": "https://s",
        "access_token": "a",
        "refresh_token": "r",
        "access_expires_at": 123.0,
        "email": "u@example.com",
    }
    store.save(data)
    assert store.exists()
    assert store.load() == data


def test_save_is_atomic_no_tmp_left(tmp_path):
    store = _store(tmp_path)
    store.save({"access_token": "a"})
    assert not os.path.exists(store.path + ".tmp")


def test_save_overwrites(tmp_path):
    store = _store(tmp_path)
    store.save({"access_token": "old"})
    store.save({"access_token": "new"})
    assert store.load()["access_token"] == "new"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 권한 전용")
def test_permissions_owner_only_posix(tmp_path):
    store = _store(tmp_path)
    store.save({"access_token": "a"})
    mode = os.stat(store.path).st_mode & 0o777
    assert mode == 0o600


def test_clear_removes_files(tmp_path):
    store = _store(tmp_path)
    store.save({"access_token": "a"})
    store.clear()
    assert not store.exists()
    assert not os.path.exists(store.lock_path)
    assert not os.path.exists(store.path + ".tmp")


def test_load_corrupt_raises(tmp_path):
    store = _store(tmp_path)
    with open(store.path, "w", encoding="utf-8") as f:
        f.write("{ not valid json ")
    with pytest.raises(CredentialStoreError):
        store.load()


def test_load_non_dict_raises(tmp_path):
    store = _store(tmp_path)
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    with pytest.raises(CredentialStoreError):
        store.load()


def test_file_lock_is_exclusive(tmp_path):
    lock = str(tmp_path / "x.lock")
    with file_lock(lock, timeout=1.0):
        # 보유 중에는 재획득이 타임아웃되어야 한다
        with pytest.raises(TimeoutError):
            with file_lock(lock, timeout=0.3):
                pass
    # 해제 후에는 다시 획득 가능
    with file_lock(lock, timeout=1.0):
        pass
