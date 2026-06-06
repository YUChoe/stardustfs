"""스토리지 attach/detach(evacuate) 테스트 — 디렉터리 폐지 + 로컬 evacuate."""
from __future__ import annotations

import os
import tempfile

import pytest

from stardustlib.gui import actions
from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.storage_source import LoopbackSource


# --- Phase 1: 디렉터리 타입 폐지 ---

def test_add_source_rejects_directory(tmp_path):
    cfg = actions.create_config(str(tmp_path / "s"), "", "dev", generate_key=True)
    with pytest.raises(ValueError):
        actions.add_source(cfg, "directory", str(tmp_path / "d"))
    # loopback은 허용
    sid = actions.add_source(cfg, "loopback", str(tmp_path / "v.img"),
                             size=10 * 1024 * 1024)
    assert sid.startswith("loopback-")
    actions.invalidate(cfg)


# --- Phase 2: 로컬 evacuate ---

def _loop(tmp, name, size=10 * 1024 * 1024):
    src = LoopbackSource(name, str(tmp / f"{name}.img"), size)
    src.initialize()
    return src


def _jbod(tmp, sources):
    store = MetadataStore(str(tmp / "m.db"), b"\x00" * 32)
    store.initialize()
    return JBODManager(sources, store, encryption_engine=None, device_id="devA"), store


def test_evacuate_moves_files_to_other_local_source(tmp_path):
    a, b = _loop(tmp_path, "src-a"), _loop(tmp_path, "src-b")
    jbod, store = _jbod(tmp_path, [a, b])
    # src-a에 파일 2개 기록(write_file이 select_source로 a 또는 b 선택될 수 있어
    # 직접 a에 쓴다)
    for vp, data in (("/f1", b"hello-1"), ("/f2", b"hello-2"*100)):
        phys = jbod._generate_physical_path(vp)
        a.write(phys, data)
        store.insert(vp, "src-a", phys, len(data), 0.0, 0.0, device_id="devA")

    report = jbod.evacuate_source("src-a")
    assert report["ok"] is True
    assert set(report["moved"]) == {"/f1", "/f2"}
    assert report["unmoved"] == []
    # 메타데이터가 src-b로 갱신되고, src-a 블록은 삭제됨
    for vp, data in (("/f1", b"hello-1"), ("/f2", b"hello-2"*100)):
        meta = store.lookup(vp)
        assert meta.source_id == "src-b"
        assert b.read(meta.physical_path) == data  # 사본 온전
    store.close()


def test_evacuate_blocks_when_no_capacity(tmp_path):
    # 단일 소스(이동 대상 없음) → 파일이 있으면 unmoved
    a = _loop(tmp_path, "only", size=10 * 1024 * 1024)
    jbod, store = _jbod(tmp_path, [a])
    phys = jbod._generate_physical_path("/f")
    a.write(phys, b"data")
    store.insert("/f", "only", phys, 4, 0.0, 0.0, device_id="devA")

    report = jbod.evacuate_source("only")
    assert report["ok"] is False
    assert report["unmoved"] == ["/f"]
    # 원본 보존(미손실)
    assert a.read(phys) == b"data"
    store.close()


def test_build_local_source_inventory(tmp_path):
    from stardustlib.device_manager import build_local_source_inventory

    a, b = _loop(tmp_path, "inv-a"), _loop(tmp_path, "inv-b")
    jbod, store = _jbod(tmp_path, [a, b])
    phys = jbod._generate_physical_path("/f")
    a.write(phys, b"x" * 100)
    inv = {i["source_id"]: i for i in build_local_source_inventory(jbod)}
    assert set(inv) == {"inv-a", "inv-b"}
    assert all(i["type"] == "loopback" for i in inv.values())
    assert inv["inv-a"]["capacity_bytes"] == 10 * 1024 * 1024
    assert inv["inv-a"]["used_bytes"] >= 100  # 기록한 블록 반영
    store.close()


def test_inventory_excludes_remote(tmp_path):
    from stardustlib.device_manager import build_local_source_inventory

    a = _loop(tmp_path, "inv-local")
    jbod, store = _jbod(tmp_path, [a])
    jbod.register_remote_device("dev-b", _FakeRemote())
    # 원격 디바이스는 jbod.sources에 없으면 자연히 제외되지만, 마운트된 원격
    # 소스가 sources에 있어도 is_remote로 걸러짐을 가정. 여기서는 로컬만 신고됨.
    inv = build_local_source_inventory(jbod)
    assert [i["source_id"] for i in inv] == ["inv-local"]
    store.close()


def test_write_spills_over_to_remote_when_local_full(tmp_path):
    # 로컬 소스가 없으면(만석 등가) 신규 쓰기는 온라인 리모트로 스필오버된다.
    jbod, store = _jbod(tmp_path, [])
    remote = _FakeRemote(source_id="dev-b-src")
    jbod.register_remote_device("dev-b", remote)
    jbod.write_file("/big", b"hello-remote")
    meta = store.lookup("/big")
    assert meta.device_id == "dev-b"       # 리모트 소유로 기록
    assert meta.source_id == "dev-b-src"
    assert b"hello-remote" in remote.stored.values()
    store.close()


def test_write_raises_when_local_full_and_no_reachable_remote(tmp_path):
    from stardustlib.exceptions import InsufficientStorageError

    jbod, store = _jbod(tmp_path, [])
    jbod.register_remote_device("dev-b", _FakeRemote(active=False))  # 오프라인
    with pytest.raises(InsufficientStorageError):
        jbod.write_file("/x", b"data")
    store.close()


def test_write_prefers_local_when_space_available(tmp_path):
    # 로컬 여유가 있으면 리모트가 있어도 로컬에 기록한다(로컬 우선 유지).
    a = _loop(tmp_path, "loc")
    jbod, store = _jbod(tmp_path, [a])
    remote = _FakeRemote()
    jbod.register_remote_device("dev-b", remote)
    jbod.write_file("/s", b"small")
    meta = store.lookup("/s")
    assert meta.device_id == "devA" and meta.source_id == "loc"
    assert remote.stored == {}  # 리모트 미사용
    store.close()


def test_evict_cold_deletes_local_only_when_safe(tmp_path):
    # replicated 파일을 안전(복제본 충분)일 때만 로컬 삭제 + evicted 표시.
    a = _loop(tmp_path, "ev")
    jbod, store = _jbod(tmp_path, [a])
    for vp in ("/safe", "/unsafe"):
        phys = jbod._generate_physical_path(vp)
        a.write(phys, b"blob-" + vp.encode())
        store.insert(vp, "ev", phys, 5, 1.0, 1.0, device_id="devA")
        store.set_replication_status(vp, "replicated")

    safe = {"/safe"}
    report = jbod.evict_cold(lambda vp: vp in safe, bytes_to_free=10**9)
    assert report["evicted"] == ["/safe"]
    assert store.lookup("/safe").evicted is True
    assert store.lookup("/unsafe").evicted is False  # 미안전 → 보존
    # 로컬 블록: safe는 삭제, unsafe는 보존
    safe_meta = store.lookup("/safe")
    assert not a.exists(safe_meta.physical_path)
    store.close()


def test_read_evicted_triggers_recover(tmp_path):
    # 축출 파일 읽기 시 _recover_fn으로 재구체화 후 읽는다.
    a = _loop(tmp_path, "rv")
    jbod, store = _jbod(tmp_path, [a])
    phys = jbod._generate_physical_path("/f")
    store.insert("/f", "rv", phys, 4, 1.0, 1.0, device_id="devA")
    store.set_replication_status("/f", "replicated")
    store.mark_evicted("/f")  # 로컬 블록 없음 + evicted

    def fake_recover(vp):
        # 복구 모사: 로컬에 재기록(write_file이 evicted 해제)
        jbod.write_file(vp, b"data")

    jbod._recover_fn = fake_recover
    assert jbod.read_file("/f") == b"data"
    assert store.lookup("/f").evicted is False
    store.close()


def test_read_evicted_without_recover_raises(tmp_path):
    a = _loop(tmp_path, "rv2")
    jbod, store = _jbod(tmp_path, [a])
    phys = jbod._generate_physical_path("/f")
    store.insert("/f", "rv2", phys, 4, 1.0, 1.0, device_id="devA")
    store.mark_evicted("/f")
    with pytest.raises(OSError):
        jbod.read_file("/f")  # 복구 콜백 없음 → 온라인 필요 에러
    store.close()


def test_evacuate_empty_source_ok(tmp_path):
    a, b = _loop(tmp_path, "e-a"), _loop(tmp_path, "e-b")
    jbod, store = _jbod(tmp_path, [a, b])
    report = jbod.evacuate_source("e-a")
    assert report["ok"] is True and report["moved"] == [] and report["unmoved"] == []
    store.close()


class _FakeRemote:
    """evacuate 리모트 대상 mock(push_blob → source_id 반환)."""

    def __init__(self, source_id="remote-src", active=True, fail=False):
        self.source_id = source_id
        self.is_active = active
        self.fail = fail
        self.stored: dict[str, bytes] = {}

    def push_blob(self, physical_path: str, data: bytes) -> str:
        if self.fail:
            raise OSError("remote offline")
        self.stored[physical_path] = data
        return self.source_id


def test_evacuate_to_remote_when_local_full(tmp_path):
    # 단일 로컬 소스(이동 대상 없음) + 온라인 리모트 → 리모트로 이동
    a = _loop(tmp_path, "only", size=10 * 1024 * 1024)
    jbod, store = _jbod(tmp_path, [a])
    remote = _FakeRemote(source_id="dev-b-src")
    jbod.register_remote_device("dev-b", remote)
    phys = jbod._generate_physical_path("/f")
    a.write(phys, b"cipher-blob")
    store.insert("/f", "only", phys, 11, 0.0, 0.0, device_id="devA")

    report = jbod.evacuate_source("only")
    assert report["ok"] is True
    assert report["moved"] == ["/f"]
    meta = store.lookup("/f")
    assert meta.device_id == "dev-b"          # 원격 소유로 이전
    assert meta.source_id == "dev-b-src"      # 원격 소스 id 반영
    assert b"cipher-blob" in remote.stored.values()
    store.close()


def test_evacuate_unmoved_when_remote_offline(tmp_path):
    a = _loop(tmp_path, "only2", size=10 * 1024 * 1024)
    jbod, store = _jbod(tmp_path, [a])
    jbod.register_remote_device("dev-b", _FakeRemote(active=False))
    phys = jbod._generate_physical_path("/f")
    a.write(phys, b"x")
    store.insert("/f", "only2", phys, 1, 0.0, 0.0, device_id="devA")

    report = jbod.evacuate_source("only2")
    assert report["ok"] is False and report["unmoved"] == ["/f"]
    assert a.read(phys) == b"x"  # 원본 보존
    store.close()


def test_detach_source_removes_from_config_when_evacuated(tmp_path, monkeypatch):
    # detach_source는 캐시된 오프라인 세션을 쓰므로, 세션의 jbod.evacuate_source를
    # 성공 모킹해 config에서 제거되는지 검증한다.
    cfg = actions.create_config(str(tmp_path / "s"), "", "dev", generate_key=True)
    sid = actions.add_source(cfg, "loopback", str(tmp_path / "v.img"),
                             size=10 * 1024 * 1024)

    class _FakeJbod:
        def evacuate_source(self, source_id):
            return {"ok": True, "moved": ["/x"], "unmoved": []}

    class _FakeSession:
        jbod = _FakeJbod()

    monkeypatch.setattr(actions, "_offline_session", lambda c: _FakeSession())
    report = actions.detach_source(cfg, sid)
    assert report["detached"] is True
    assert all(s.get("id") != sid for s in actions.list_sources(cfg))


def test_detach_source_kept_when_unmoved(tmp_path, monkeypatch):
    cfg = actions.create_config(str(tmp_path / "s2"), "", "dev", generate_key=True)
    sid = actions.add_source(cfg, "loopback", str(tmp_path / "v2.img"),
                             size=10 * 1024 * 1024)

    class _FakeJbod:
        def evacuate_source(self, source_id):
            return {"ok": False, "moved": [], "unmoved": ["/x"]}

    class _FakeSession:
        jbod = _FakeJbod()

    monkeypatch.setattr(actions, "_offline_session", lambda c: _FakeSession())
    report = actions.detach_source(cfg, sid)
    assert report["detached"] is False
    assert any(s.get("id") == sid for s in actions.list_sources(cfg))  # 유지
