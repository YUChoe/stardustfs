"""리플리케이션 스케줄러(자동 백업 루프) + 메타데이터 대상 조회 테스트."""
from __future__ import annotations

import os
import tempfile

import pytest

from stardustlib.metadata_store import MetadataStore
from stardustlib.replication_scheduler import ReplicationScheduler


# --- metadata 대상 조회 ---

def _store() -> MetadataStore:
    path = os.path.join(tempfile.mkdtemp(), "m.db")
    s = MetadataStore(path, b"\x00" * 32)
    s.initialize()
    return s


def test_list_for_replication_filters_status_owner_and_tombstone():
    s = _store()
    try:
        # 로컬 소유(devA), 미복제
        s.insert("/a", "src", "p_a", 10, 0.0, 0.0, device_id="devA")
        # 로컬 소유, 이미 replicated → 제외
        s.insert("/b", "src", "p_b", 10, 0.0, 0.0, device_id="devA")
        s.set_replication_status("/b", "replicated")
        # 다른 device 소유 → 제외
        s.insert("/c", "src", "p_c", 10, 0.0, 0.0, device_id="devB")
        # 레거시 NULL 소유(로컬로 간주) → 포함
        s.insert("/d", "src", "p_d", 10, 0.0, 0.0)
        # tombstone → 제외
        s.insert("/e", "src", "p_e", 10, 0.0, 0.0, device_id="devA")
        s.delete("/e")

        none_local = s.list_virtual_paths_for_replication(("none",), "devA")
        assert set(none_local) == {"/a", "/d"}

        repl = s.list_virtual_paths_for_replication(("replicated", "pending"), "devA")
        assert repl == ["/b"]
    finally:
        s.close()


# --- 스케줄러 backup 주기 ---

class _FakeResult:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeManager:
    def __init__(
        self, fail_paths: set[str] | None = None,
        heal_fail: set[str] | None = None,
    ) -> None:
        self.replicated: list[str] = []
        self.healed: list[str] = []
        self.fail_paths = fail_paths or set()
        self.heal_fail = heal_fail or set()
        self.closed = False

    def replicate(self, vpath: str):
        if vpath in self.fail_paths:
            raise RuntimeError("boom")
        self.replicated.append(vpath)
        return _FakeResult("replicated")

    def ensure_replicas(self, vpath: str):
        if vpath in self.heal_fail:
            raise RuntimeError("boom")
        self.healed.append(vpath)
        return _FakeResult("replicated")

    def close(self) -> None:
        self.closed = True


class _FakeMeta:
    def __init__(
        self, none_paths: list[str] | None = None,
        heal_paths: list[str] | None = None,
    ) -> None:
        self._none = none_paths or []
        self._heal = heal_paths or []

    def list_virtual_paths_for_replication(self, statuses, owner_device_id=None):
        return list(self._none) if "none" in statuses else list(self._heal)


@pytest.mark.asyncio
async def test_backup_cycle_replicates_all_targets():
    mgr = _FakeManager()
    sched = ReplicationScheduler(mgr, _FakeMeta(["/a", "/b", "/c"]), "devA")
    n = await sched.run_backup_cycle()
    assert n == 3
    assert mgr.replicated == ["/a", "/b", "/c"]


@pytest.mark.asyncio
async def test_backup_cycle_isolates_failure():
    mgr = _FakeManager(fail_paths={"/b"})
    sched = ReplicationScheduler(mgr, _FakeMeta(["/a", "/b", "/c"]), "devA")
    n = await sched.run_backup_cycle()
    assert n == 2  # /b 실패해도 /a,/c 진행
    assert mgr.replicated == ["/a", "/c"]


@pytest.mark.asyncio
async def test_backup_cycle_respects_max():
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta([f"/{i}" for i in range(10)]), "devA",
        max_files_per_cycle=3,
    )
    n = await sched.run_backup_cycle()
    assert n == 3
    assert len(mgr.replicated) == 3


@pytest.mark.asyncio
async def test_heal_cycle_runs_ensure_replicas():
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(heal_paths=["/a", "/b"]), "devA"
    )
    n = await sched.run_heal_cycle()
    assert n == 2
    assert mgr.healed == ["/a", "/b"]


@pytest.mark.asyncio
async def test_heal_cycle_isolates_failure():
    mgr = _FakeManager(heal_fail={"/b"})
    sched = ReplicationScheduler(
        mgr, _FakeMeta(heal_paths=["/a", "/b", "/c"]), "devA"
    )
    n = await sched.run_heal_cycle()
    assert n == 2
    assert mgr.healed == ["/a", "/c"]


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta([]), "devA", backup_interval=0.05, heal_interval=0.05
    )
    await sched.start()
    assert len(sched._tasks) == 2  # backup + heal
    await sched.stop()
    assert mgr.closed is True
    assert sched._tasks == []
