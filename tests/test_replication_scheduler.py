"""리플리케이션 스케줄러(자동 백업 루프) + 메타데이터 대상 조회 테스트."""
from __future__ import annotations

import asyncio
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
        self.healthy_paths: set[str] = set()
        self.closed = False

    def replicate(self, vpath: str):
        if vpath in getattr(self, "missing_paths", set()):
            raise FileNotFoundError(vpath)
        if vpath in self.fail_paths:
            raise RuntimeError("boom")
        self.replicated.append(vpath)
        return _FakeResult("replicated")

    def ensure_replicas(self, vpath: str):
        if vpath in self.heal_fail:
            raise RuntimeError("boom")
        self.healed.append(vpath)
        return _FakeResult("replicated")

    def replication_health(self, vpath: str):
        # 기본: 모든 파일 degraded(테스트에서 healthy_paths로 일부 건강 처리)
        return _Health(degraded=vpath not in self.healthy_paths)

    def set_min_replicas(self, n: int) -> None:
        self.min_replicas = n

    def close(self) -> None:
        self.closed = True


class _Health:
    def __init__(self, degraded: bool, min_online: int = 0) -> None:
        self.degraded = degraded
        self.min_online = min_online


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
    assert set(mgr.replicated) == {"/a", "/b", "/c"}  # 병렬이라 순서 무관


@pytest.mark.asyncio
async def test_backup_cycle_includes_pending():
    """backup 루프는 none + pending을 대상으로 한다(pending 즉시 재시도)."""
    seen: list = []

    class _RecMeta:
        def list_virtual_paths_for_replication(self, statuses, owner_device_id=None):
            seen.append(tuple(statuses))
            return ["/p"]

    mgr = _FakeManager()
    sched = ReplicationScheduler(mgr, _RecMeta(), "devA")
    await sched.run_backup_cycle()
    assert ("none", "pending") in seen
    assert mgr.replicated == ["/p"]


@pytest.mark.asyncio
async def test_backup_cycle_isolates_failure():
    mgr = _FakeManager(fail_paths={"/b"})
    sched = ReplicationScheduler(mgr, _FakeMeta(["/a", "/b", "/c"]), "devA")
    n = await sched.run_backup_cycle()
    assert n == 2  # /b 실패해도 /a,/c 진행
    assert set(mgr.replicated) == {"/a", "/c"}  # 병렬이라 순서 무관


@pytest.mark.asyncio
async def test_backup_cycle_skips_missing_local_file_and_caches():
    """로컬에 없는 파일(타 device 소유 NULL 레코드)은 건너뛰고 재시도 안 한다."""
    mgr = _FakeManager()
    mgr.missing_paths = {"/remote-owned"}
    sched = ReplicationScheduler(
        mgr, _FakeMeta(["/local", "/remote-owned"]), "devA"
    )
    n1 = await sched.run_backup_cycle()
    assert n1 == 1                       # /local만 처리
    assert mgr.replicated == ["/local"]
    assert "/remote-owned" in sched._skip_backup
    # 다음 주기엔 missing 파일을 아예 시도하지 않음
    mgr.replicated.clear()
    n2 = await sched.run_backup_cycle()
    assert mgr.replicated == ["/local"]  # /remote-owned 재시도 없음
    assert n2 == 1


@pytest.mark.asyncio
async def test_pending_triggers_short_retry_backoff():
    """복제 미완료(pending)가 남으면 전체 주기 대신 짧은 백오프로 재시도한다."""
    from stardustlib.replication_scheduler import (
        _BACKLOG_DRAIN_DELAY,
        _RETRY_MIN_DELAY,
    )

    class _PendingMgr(_FakeManager):
        def replicate(self, vpath: str):
            self.replicated.append(vpath)
            return _FakeResult("pending")  # 목표 미달

    mgr = _PendingMgr()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(["/p"]), "devA", backup_interval=300.0
    )
    processed = await sched.run_backup_cycle()
    assert processed == 1 and sched._last_pending == 1
    # 첫 재시도: 짧은 최소 지연
    d1 = sched._next_delay(processed)
    assert d1 == _RETRY_MIN_DELAY
    # 지속 pending → 지수 백오프(2배), backup_interval 상한
    d2 = sched._next_delay(processed)
    assert d2 == _RETRY_MIN_DELAY * 2


@pytest.mark.asyncio
async def test_all_replicated_uses_full_interval():
    """모두 완료되면 정상 주기 간격으로 쉬고 백오프를 초기화한다."""
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(["/a"]), "devA", backup_interval=300.0
    )
    processed = await sched.run_backup_cycle()
    assert sched._last_pending == 0
    assert sched._next_delay(processed) == 300.0
    assert sched._retry_delay is None


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
async def test_heal_cycle_runs_ensure_replicas_after_grace():
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(heal_paths=["/a", "/b"]), "devA", heal_grace_seconds=0,
    )
    n = await sched.run_heal_cycle()
    assert n == 2
    assert mgr.healed == ["/a", "/b"]


@pytest.mark.asyncio
async def test_heal_cycle_waits_for_grace():
    """유예(grace) 동안은 degraded여도 재복제하지 않는다(churn 방지)."""
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(heal_paths=["/a"]), "devA", heal_grace_seconds=10_000,
    )
    n = await sched.run_heal_cycle()
    assert n == 0
    assert mgr.healed == []
    assert "/a" in sched._degraded_since  # 관측은 기록됨


@pytest.mark.asyncio
async def test_heal_cycle_skips_healthy_and_clears_record():
    mgr = _FakeManager()
    mgr.healthy_paths = {"/a"}  # /a는 건강, /b는 degraded
    sched = ReplicationScheduler(
        mgr, _FakeMeta(heal_paths=["/a", "/b"]), "devA", heal_grace_seconds=0,
    )
    n = await sched.run_heal_cycle()
    assert mgr.healed == ["/b"]
    assert n == 1
    assert "/a" not in sched._degraded_since


@pytest.mark.asyncio
async def test_heal_cycle_isolates_failure():
    mgr = _FakeManager(heal_fail={"/b"})
    sched = ReplicationScheduler(
        mgr, _FakeMeta(heal_paths=["/a", "/b", "/c"]), "devA", heal_grace_seconds=0,
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
    assert len(sched._tasks) == 2  # backup + heal (정책 fetcher 없음)
    await sched.stop()
    assert mgr.closed is True
    assert sched._tasks == []


@pytest.mark.asyncio
async def test_policy_loop_applies_policy():
    mgr = _FakeManager()
    applied: list[dict] = []

    async def fetcher():
        return {"reciprocity_fraction": 0.5, "min_replicas": 5}

    sched = ReplicationScheduler(
        mgr, _FakeMeta([]), "devA",
        backup_interval=10_000, heal_interval=10_000, policy_interval=10_000,
        policy_fetcher=fetcher,
        on_policy=lambda p: applied.append(p),
    )
    await sched.start()
    assert len(sched._tasks) == 3  # backup + heal + policy
    # 정책 루프가 시작 즉시 1회 적용할 시간을 준다
    for _ in range(50):
        if applied:
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    assert applied and applied[0]["min_replicas"] == 5


# --- 수정 시 복제 상태 무효화 (낡은 백업이 '완료'로 남는 버그 회귀) ---

def test_update_invalidates_replication_status():
    """파일 수정 시 replication_status가 none으로 되돌아 자동 백업 대상에 다시 잡힌다.

    청크 ID는 경로 기반이라 홀더가 옛 내용을 들고 있어도 건강해 보인다. 무효화하지
    않으면 수정된 파일이 ('none','pending') 대상에서 영구히 빠져 낡은 백업이
    '완료'로 남는다.
    """
    s = _store()
    try:
        s.insert("/a", "src", "p_a", 10, 0.0, 0.0, device_id="devA")
        s.set_replication_status("/a", "replicated")
        assert s.list_virtual_paths_for_replication(("none", "pending"), "devA") == []

        # 내용 수정
        s.update("/a", file_size=20, modified_at=1.0, device_id="devA")

        assert s.get_replication_status("/a") == "none"
        assert s.list_virtual_paths_for_replication(
            ("none", "pending"), "devA"
        ) == ["/a"]
    finally:
        s.close()


def test_takeover_update_invalidates_replication_status():
    """소유권 이전(source/physical 갱신)도 복제 상태를 무효화한다."""
    s = _store()
    try:
        s.insert("/a", "srcOld", "p_old", 10, 0.0, 0.0, device_id="devB")
        s.set_replication_status("/a", "replicated")
        s.update(
            "/a", file_size=30, modified_at=2.0, device_id="devA",
            source_id="srcNew", physical_path="p_new",
        )
        rec = s.lookup("/a")
        assert rec is not None
        assert rec.replication_status == "none"
        assert rec.source_id == "srcNew"
        assert rec.physical_path == "p_new"
    finally:
        s.close()


# --- announce (즉시 백업 요청) ---

@pytest.mark.asyncio
async def test_announce_prioritises_path_in_next_cycle():
    """announce된 경로가 대상 목록 앞으로 와서 상한 안에 먼저 처리된다."""
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(["/a", "/b", "/c"]), "devA", max_files_per_cycle=1,
    )
    sched.announce("/c")
    n = await sched.run_backup_cycle()
    assert n == 1
    assert mgr.replicated == ["/c"]  # 목록 순서상 /a가 아니라 announce된 /c


@pytest.mark.asyncio
async def test_announce_queue_is_drained_once():
    """announce 큐는 한 사이클에서 비워져 다음 사이클에 중복 우선되지 않는다."""
    mgr = _FakeManager()
    sched = ReplicationScheduler(mgr, _FakeMeta(["/a"]), "devA")
    sched.announce("/a")
    await sched.run_backup_cycle()
    assert sched._announced == set()


@pytest.mark.asyncio
async def test_announce_clears_skip_cache():
    """이전에 '로컬에 없음'으로 캐시된 경로도 announce하면 다시 시도한다."""
    mgr = _FakeManager()
    mgr.missing_paths = {"/gone"}
    sched = ReplicationScheduler(mgr, _FakeMeta(["/gone"]), "devA")
    await sched.run_backup_cycle()
    assert "/gone" in sched._skip_backup

    mgr.missing_paths = set()  # 다시 기록됨
    sched.announce("/gone")
    assert "/gone" not in sched._skip_backup
    n = await sched.run_backup_cycle()
    assert n == 1
    assert mgr.replicated == ["/gone"]


@pytest.mark.asyncio
async def test_announce_ignores_already_replicated_path():
    """대상 목록에 없는(이미 복제 완료) 경로를 announce해도 오류 없이 무시된다."""
    mgr = _FakeManager()
    sched = ReplicationScheduler(mgr, _FakeMeta([]), "devA")
    sched.announce("/done")
    n = await sched.run_backup_cycle()
    assert n == 0
    assert mgr.replicated == []


@pytest.mark.asyncio
async def test_announce_wakes_backup_loop_before_interval():
    """announce가 긴 백업 주기 대기를 깨워 즉시 사이클을 돌린다."""
    mgr = _FakeManager()
    sched = ReplicationScheduler(
        mgr, _FakeMeta(["/a"]), "devA", backup_interval=3600.0,
    )
    # 병합 창을 짧게 해 테스트가 오래 걸리지 않도록 한다.
    import stardustlib.replication_scheduler as mod
    original = mod._ANNOUNCE_COALESCE_DELAY
    mod._ANNOUNCE_COALESCE_DELAY = 0.01
    try:
        await sched.start()
        await asyncio.sleep(0.05)  # 최초 사이클 완료(=/a 복제, 이후 3600초 대기)
        assert mgr.replicated == ["/a"]

        sched.announce("/a")
        await asyncio.sleep(0.2)  # 주기(3600초)를 기다리지 않고 재실행되어야 함
        assert mgr.replicated.count("/a") >= 2
    finally:
        mod._ANNOUNCE_COALESCE_DELAY = original
        await sched.stop()
