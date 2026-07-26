"""청크 단위 분산(Phase 3) 검증.

- 로컬 만석 시 청크 단위 원격 스필오버(청크마다 보관 기기가 다를 수 있음)
- 읽기 시 청크별 로컬/원격 라우팅 후 결합
- 청크 단위 evacuate의 원격 폴백
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import InsufficientStorageError
from stardustlib.metadata_store import MetadataStore
from stardustlib.storage_pool import StoragePool
from stardustlib.storage_source import DirectorySource

SMALL_CHUNK = 512
_KEY = b"\x05" * 32


class _FakeRemote:
    """원격 기기 프록시(RemoteSource) 대역.

    push_blob/read_from_source/delete만 흉내내며, 보관한 암호문을 그대로 돌려준다
    (호스트는 복호화하지 않는다).
    """

    is_remote = True

    def __init__(self, source_id="remote-src", active=True, capacity=None):
        self.source_id = source_id
        self.is_active = active
        self.blocks: dict[str, bytes] = {}
        self.refreshed = False
        self._capacity = capacity

    def refresh(self, force=False):
        self.refreshed = True
        self.is_active = True
        return True

    def push_blob(self, physical_path, data):
        if self._capacity is not None and len(self.blocks) >= self._capacity:
            raise OSError("insufficient space (fake remote full)")
        self.blocks[physical_path] = data
        return self.source_id

    def read_from_source(self, physical_path, source_id, file_size=None):
        if physical_path not in self.blocks:
            raise FileNotFoundError(physical_path)
        return self.blocks[physical_path]

    def delete(self, physical_path):
        self.blocks.pop(physical_path, None)


def _make(tmp, size_limit=None, monkeypatch=None):
    """로컬 소스 1개 + 암호화 엔진을 갖춘 풀을 만든다."""
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)
    src = DirectorySource("local-1", data_dir)
    src.initialize()
    if size_limit is not None:
        # 로컬 여유 공간을 강제로 제한해 스필오버를 유발한다.
        src.get_available_space = lambda: size_limit  # type: ignore[method-assign]
    store = MetadataStore(os.path.join(tmp, "m.db"), _KEY)
    store.initialize()
    pool = StoragePool(
        [src], store, encryption_engine=EncryptionEngine(_KEY),
        device_id="dev-A",
    )
    return pool, store, src


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr("stardustlib.storage_pool.CHUNK_SIZE", SMALL_CHUNK)
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# 청크 단위 원격 스필오버
# ------------------------------------------------------------------

def test_all_chunks_spill_to_remote_when_local_full(env):
    """로컬에 공간이 없으면 청크가 원격 기기로 배치된다."""
    pool, store, _src = _make(env, size_limit=0)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    data = os.urandom(SMALL_CHUNK * 3)
    pool.write_file("/f.bin", data)

    chunks = store.get_chunks("/f.bin")
    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.device_id == "dev-B"
        assert chunk.source_id == "remote-src"
        assert chunk.chunk_ref in remote.blocks
    # 원격에 놓인 청크만으로 원본을 복원한다.
    assert pool.read_file("/f.bin") == data
    store.close()


def test_chunks_split_between_local_and_remote(env):
    """로컬 여유가 일부만 있으면 남는 청크만 원격으로 간다(청크별 배치)."""
    pool, store, src = _make(env)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    # 청크 2개 분량만 로컬에 허용한다(암호문 = 평문 + 38B 헤더).
    per_chunk = SMALL_CHUNK + EncryptionEngine.HEADER_SIZE
    budget = {"left": per_chunk * 2}

    def fake_space():
        return budget["left"]

    original_write = src.write

    def counting_write(path, blob):
        original_write(path, blob)
        budget["left"] -= len(blob)

    src.get_available_space = fake_space          # type: ignore[method-assign]
    src.write = counting_write                    # type: ignore[method-assign]

    data = os.urandom(SMALL_CHUNK * 4)
    pool.write_file("/split.bin", data)

    chunks = store.get_chunks("/split.bin")
    assert len(chunks) == 4
    local = [c for c in chunks if c.device_id == "dev-A"]
    remote_chunks = [c for c in chunks if c.device_id == "dev-B"]
    assert len(local) == 2 and len(remote_chunks) == 2
    # 한 파일의 청크가 두 기기에 흩어져 있어도 읽기가 결합해 복원한다.
    src.write = original_write                    # type: ignore[method-assign]
    assert pool.read_file("/split.bin") == data
    store.close()


def test_spillover_raises_when_no_remote_reachable(env):
    """로컬·원격 어디에도 놓을 수 없으면 규격 에러(조용한 실패 금지)."""
    pool, store, _src = _make(env, size_limit=0)
    pool.register_remote_device("dev-B", _FakeRemote(active=False))
    # 비활성 원격은 refresh로 살아나므로, 아예 원격이 없는 상태를 만든다.
    pool._remote_devices.clear()

    with pytest.raises(InsufficientStorageError):
        pool.write_file("/nope.bin", os.urandom(SMALL_CHUNK))
    assert store.lookup("/nope.bin") is None       # 메타데이터 미커밋
    store.close()


def test_partial_placement_failure_cleans_up(env):
    """일부 청크만 놓인 뒤 실패하면 기록한 청크를 정리하고 에러를 낸다."""
    pool, store, _src = _make(env, size_limit=0)
    # 청크 2개만 받는 원격 → 3번째에서 실패
    remote = _FakeRemote(capacity=2)
    pool.register_remote_device("dev-B", remote)

    with pytest.raises(InsufficientStorageError):
        pool.write_file("/big.bin", os.urandom(SMALL_CHUNK * 3))

    assert store.lookup("/big.bin") is None        # 반쯤 저장된 상태 없음
    assert remote.blocks == {}                     # 기록한 청크 정리됨
    store.close()


# ------------------------------------------------------------------
# 청크별 원격 읽기 라우팅
# ------------------------------------------------------------------

def test_read_routes_per_chunk_device(env):
    """매니페스트의 청크별 device_id로 라우팅한다(파일 단위 소유자와 무관)."""
    pool, store, src = _make(env)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    data = os.urandom(SMALL_CHUNK * 2)
    pool.write_file("/f.bin", data)
    chunks = store.get_chunks("/f.bin")

    # 두 번째 청크를 원격으로 옮긴다(로컬 블록 삭제 + 매니페스트 갱신).
    moved = chunks[1]
    remote.blocks[moved.chunk_ref] = src.read(moved.chunk_ref)
    src.delete(moved.chunk_ref)
    store.update_chunk_location("/f.bin", moved.index, "remote-src", "dev-B")

    # 로컬 청크 + 원격 청크를 결합해 원본을 복원한다.
    assert pool.read_file("/f.bin") == data
    store.close()


def test_read_range_routes_only_needed_remote_chunk(env):
    """부분 읽기는 필요한 청크만 원격에서 가져온다."""
    pool, store, src = _make(env)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    data = os.urandom(SMALL_CHUNK * 3)
    pool.write_file("/f.bin", data)
    chunks = store.get_chunks("/f.bin")

    # 모든 청크를 원격으로 이동
    for chunk in chunks:
        remote.blocks[chunk.chunk_ref] = src.read(chunk.chunk_ref)
        src.delete(chunk.chunk_ref)
        store.update_chunk_location("/f.bin", chunk.index, "remote-src", "dev-B")

    fetched: list[str] = []
    original = remote.read_from_source

    def spy(path, source_id, file_size=None):
        fetched.append(path)
        return original(path, source_id, file_size)

    remote.read_from_source = spy                  # type: ignore[method-assign]

    offset = SMALL_CHUNK + 5
    assert pool.read_range("/f.bin", offset, 10) == data[offset:offset + 10]
    assert fetched == [chunks[1].chunk_ref]        # 청크 1개만 전송
    store.close()


def test_read_reports_unreachable_chunk_device(env):
    """청크 보관 기기가 미마운트면 어느 청크인지 밝히며 실패한다."""
    pool, store, src = _make(env)
    data = os.urandom(SMALL_CHUNK * 2)
    pool.write_file("/f.bin", data)
    chunks = store.get_chunks("/f.bin")

    # 원격 소유로 표시하되 프록시는 등록하지 않는다.
    src.delete(chunks[1].chunk_ref)
    store.update_chunk_location("/f.bin", 1, "remote-src", "dev-ghost")

    with pytest.raises(OSError) as exc:
        pool.read_file("/f.bin")
    assert "chunk_index=1" in str(exc.value)
    assert "dev-ghost" in str(exc.value)
    store.close()


def test_offline_chunk_device_is_refreshed(env):
    """비활성 원격 프록시는 읽기 직전 재라우팅을 시도한다."""
    pool, store, src = _make(env)
    remote = _FakeRemote(active=False)
    pool.register_remote_device("dev-B", remote)

    data = os.urandom(SMALL_CHUNK)
    pool.write_file("/f.bin", data)
    chunk = store.get_chunks("/f.bin")[0]
    remote.blocks[chunk.chunk_ref] = src.read(chunk.chunk_ref)
    src.delete(chunk.chunk_ref)
    store.update_chunk_location("/f.bin", 0, "remote-src", "dev-B")

    assert pool.read_file("/f.bin") == data
    assert remote.refreshed is True
    store.close()


# ------------------------------------------------------------------
# evacuate · 삭제
# ------------------------------------------------------------------

def test_evacuate_chunks_falls_back_to_remote(env):
    """남은 로컬 소스가 없으면 청크를 원격으로 옮긴다."""
    pool, store, src = _make(env)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    data = os.urandom(SMALL_CHUNK * 2)
    pool.write_file("/f.bin", data)

    report = pool.evacuate_source("local-1")

    assert report["ok"] is True
    assert report["moved"] == ["/f.bin"]
    for chunk in store.get_chunks("/f.bin"):
        assert chunk.device_id == "dev-B"
        assert chunk.chunk_ref in remote.blocks
        assert not src.exists(chunk.chunk_ref)     # 원본 삭제됨
    assert pool.read_file("/f.bin") == data
    store.close()


def test_delete_file_removes_remote_chunks(env):
    """파일 삭제 시 원격에 있는 청크도 삭제 요청을 보낸다."""
    pool, store, src = _make(env, size_limit=0)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    pool.write_file("/gone.bin", os.urandom(SMALL_CHUNK * 2))
    assert len(remote.blocks) == 2

    pool.delete_file("/gone.bin")

    assert remote.blocks == {}
    assert store.get_chunks("/gone.bin") == []
    store.close()


def test_evacuate_finds_files_whose_head_chunk_moved(env):
    """첫 청크가 다른 소스로 간 파일의 남은 청크도 evacuate 대상에 포함된다.

    files.source_id는 첫 청크 기준이라 그 값만 보면 이런 파일을 놓치고, detach 시
    청크가 소스에 남아 사라진다.
    """
    pool, store, src = _make(env)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    data = os.urandom(SMALL_CHUNK * 2)
    pool.write_file("/f.bin", data)
    chunks = store.get_chunks("/f.bin")

    # 첫 청크만 원격으로 옮겨 files.source_id가 원격을 가리키게 만든다.
    head = chunks[0]
    remote.blocks[head.chunk_ref] = src.read(head.chunk_ref)
    src.delete(head.chunk_ref)
    store.update_chunk_location("/f.bin", head.index, "remote-src", "dev-B")
    store.update("/f.bin", file_size=len(data), modified_at=1.0,
                 device_id="dev-A", source_id="remote-src",
                 physical_path=head.chunk_ref)

    # files.source_id로는 잡히지 않지만 남은 청크가 있으므로 대상이어야 한다.
    assert "/f.bin" not in [
        m.virtual_path for m in store.list_files_in_source("local-1")
    ]
    assert "/f.bin" in [
        m.virtual_path for m in pool._files_to_evacuate("local-1")
    ]

    report = pool.evacuate_source("local-1")

    assert report["ok"] is True
    # 남아 있던 두 번째 청크도 원격으로 옮겨졌다.
    for chunk in store.get_chunks("/f.bin"):
        assert chunk.device_id == "dev-B"
        assert not src.exists(chunk.chunk_ref)
    assert pool.read_file("/f.bin") == data
    store.close()


def test_file_record_owner_stays_local_when_chunks_spill(env):
    """청크가 원격에 놓여도 파일 레코드 소유자는 쓴 기기다.

    소유자가 원격으로 바뀌면 그 파일을 삭제·수정할 주체가 사라져, 삭제가 tombstone만
    남기고 물리 블록을 정리하지 못한다.
    """
    pool, store, _src = _make(env, size_limit=0)
    remote = _FakeRemote()
    pool.register_remote_device("dev-B", remote)

    pool.write_file("/f.bin", os.urandom(SMALL_CHUNK))

    rec = store.lookup("/f.bin")
    assert rec.device_id == "dev-A"                # 파일 소유자는 로컬
    assert store.get_chunks("/f.bin")[0].device_id == "dev-B"  # 청크는 원격
    store.close()
