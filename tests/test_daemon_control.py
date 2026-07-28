"""데몬 전송 위임 제어 채널(put/get) 테스트 — 로컬 aiohttp 서버 + httpx 클라이언트."""
from __future__ import annotations

import asyncio
import os

import pytest

from stardustlib import daemon_control
from stardustlib.daemon_control import DaemonControlServer, transfer_via_daemon


class _FakeStoragePool:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def write_file(self, vpath: str, data: bytes) -> None:
        self.store[vpath] = data

    def read_file(self, vpath: str) -> bytes:
        if vpath not in self.store:
            raise FileNotFoundError(vpath)
        return self.store[vpath]


class _FakeSync:
    def __init__(self) -> None:
        self.uploaded = 0

    async def upload_metadata(self) -> None:
        self.uploaded += 1


@pytest.mark.asyncio
async def test_put_then_get_via_daemon(tmp_path):
    db = str(tmp_path / "m.db")
    storage_pool, sync = _FakeStoragePool(), _FakeSync()
    server = DaemonControlServer(storage_pool, sync, db)
    await server.start()
    try:
        src = tmp_path / "up.bin"
        src.write_bytes(b"hello-delegated" * 1000)
        # put 위임 (httpx sync는 스레드에서 — 이벤트 루프 블로킹 방지)
        res = await asyncio.to_thread(
            transfer_via_daemon, db, "put", "/f", str(src)
        )
        assert res["ok"] is True and res["bytes"] == len(src.read_bytes())
        assert storage_pool.store["/f"] == src.read_bytes()
        assert sync.uploaded == 1  # 메타데이터 전파

        dst = tmp_path / "down.bin"
        res = await asyncio.to_thread(
            transfer_via_daemon, db, "get", "/f", str(dst)
        )
        assert res["ok"] is True
        assert dst.read_bytes() == src.read_bytes()
    finally:
        await server.stop()
    # 종료 후 제어 파일 제거 확인
    assert daemon_control.read_ctl(db) is None


@pytest.mark.asyncio
async def test_delegate_returns_none_when_no_daemon(tmp_path):
    db = str(tmp_path / "absent.db")
    # 제어 파일 없음 → None(호출자가 직접 수행으로 fallback)
    res = await asyncio.to_thread(transfer_via_daemon, db, "put", "/f", str(tmp_path / "x"))
    assert res is None


@pytest.mark.asyncio
async def test_bad_token_rejected(tmp_path):
    db = str(tmp_path / "m.db")
    server = DaemonControlServer(_FakeStoragePool(), _FakeSync(), db)
    await server.start()
    try:
        ctl = daemon_control.read_ctl(db)
        import httpx
        resp = await asyncio.to_thread(
            lambda: httpx.post(
                f"http://127.0.0.1:{ctl['port']}/ctl/get",
                json={"virtual_path": "/f", "local_path": str(tmp_path / "o")},
                headers={"X-Ctl-Token": "wrong"}, timeout=5,
            )
        )
        assert resp.status_code == 403
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_get_missing_file_500(tmp_path):
    db = str(tmp_path / "m.db")
    server = DaemonControlServer(_FakeStoragePool(), _FakeSync(), db)
    await server.start()
    try:
        with pytest.raises(OSError):
            await asyncio.to_thread(
                transfer_via_daemon, db, "get", "/missing", str(tmp_path / "o")
            )
    finally:
        await server.stop()


# --- announce (즉시 백업 요청) ---

class _FakeScheduler:
    def __init__(self) -> None:
        self.announced: list[str] = []

    def announce(self, vpath: str) -> None:
        self.announced.append(vpath)


@pytest.mark.asyncio
async def test_put_announces_for_immediate_backup(tmp_path):
    """put 완료 후 스케줄러에 announce해 주기 대기 없이 백업되게 한다."""
    db = str(tmp_path / "m.db")
    sched = _FakeScheduler()
    server = DaemonControlServer(
        _FakeStoragePool(), _FakeSync(), db, repl_scheduler=sched
    )
    await server.start()
    try:
        src = tmp_path / "up.bin"
        src.write_bytes(b"data")
        await asyncio.to_thread(transfer_via_daemon, db, "put", "/f", str(src))
        assert sched.announced == ["/f"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_put_without_scheduler_still_succeeds(tmp_path):
    """리플리케이션 비활성(스케줄러 없음)이어도 put은 정상 동작한다."""
    db = str(tmp_path / "m.db")
    storage_pool = _FakeStoragePool()
    server = DaemonControlServer(storage_pool, _FakeSync(), db)  # repl_scheduler=None
    await server.start()
    try:
        src = tmp_path / "up.bin"
        src.write_bytes(b"data")
        res = await asyncio.to_thread(
            transfer_via_daemon, db, "put", "/f", str(src)
        )
        assert res["ok"] is True
        assert storage_pool.store["/f"] == b"data"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_manual_announce_endpoint(tmp_path):
    """수동 백업 요청(GUI 컨텍스트 메뉴)이 경로들을 스케줄러에 등록한다."""
    db = str(tmp_path / "m.db")
    sched = _FakeScheduler()
    server = DaemonControlServer(
        _FakeStoragePool(), _FakeSync(), db, repl_scheduler=sched
    )
    await server.start()
    try:
        count = await asyncio.to_thread(
            daemon_control.announce_via_daemon, db, ["/a", "/b"]
        )
        assert count == 2
        assert sched.announced == ["/a", "/b"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_manual_announce_without_replication_503(tmp_path):
    """리플리케이션 비활성이면 수동 요청은 규격 오류(503)를 낸다(조용한 무시 금지)."""
    db = str(tmp_path / "m.db")
    server = DaemonControlServer(_FakeStoragePool(), _FakeSync(), db)
    await server.start()
    try:
        with pytest.raises(OSError):
            await asyncio.to_thread(
                daemon_control.announce_via_daemon, db, ["/a"]
            )
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_manual_announce_no_daemon_returns_none(tmp_path):
    """데몬 미실행이면 None을 반환해 호출자가 안내할 수 있게 한다."""
    db = str(tmp_path / "absent.db")
    count = await asyncio.to_thread(
        daemon_control.announce_via_daemon, db, ["/a"]
    )
    assert count is None


# --- 복제 진행 조회 (/ctl/progress) ---

@pytest.mark.asyncio
async def test_progress_reports_active_replication(tmp_path):
    """진행 중이면 경로·단계·수치를 돌려준다(사용자 데이터 없음)."""
    from stardustlib.replication_progress import STAGE_STORING, ProgressTracker

    db = str(tmp_path / "m.db")
    tracker = ProgressTracker()
    tracker.begin("/movies/big.mp4", 188, STAGE_STORING)
    tracker.advance(42, secured=40)

    server = DaemonControlServer(
        _FakeStoragePool(), _FakeSync(), db, repl_progress=tracker
    )
    await server.start()
    try:
        res = await asyncio.to_thread(
            daemon_control.progress_via_daemon, db
        )
        assert res["active"] is True
        assert res["path"] == "/movies/big.mp4"
        assert res["stage"] == STAGE_STORING
        assert res["done"] == 42 and res["total"] == 188
        assert res["secured"] == 40
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_progress_inactive_when_idle(tmp_path):
    from stardustlib.replication_progress import ProgressTracker

    db = str(tmp_path / "m.db")
    server = DaemonControlServer(
        _FakeStoragePool(), _FakeSync(), db,
        repl_progress=ProgressTracker(),
    )
    await server.start()
    try:
        res = await asyncio.to_thread(daemon_control.progress_via_daemon, db)
        assert res == {"active": False}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_progress_inactive_without_tracker(tmp_path):
    """리플리케이션 비활성이면 추적기가 없다 — 조회는 비활성으로 응답한다."""
    db = str(tmp_path / "m.db")
    server = DaemonControlServer(_FakeStoragePool(), _FakeSync(), db)
    await server.start()
    try:
        res = await asyncio.to_thread(daemon_control.progress_via_daemon, db)
        assert res == {"active": False}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_progress_returns_none_without_daemon(tmp_path):
    """데몬 미실행이면 None(GUI는 진행 표시를 생략한다)."""
    db = str(tmp_path / "absent.db")
    res = await asyncio.to_thread(daemon_control.progress_via_daemon, db)
    assert res is None


class _FakeSource:
    """용량을 보고하는 소스 대역(로컬/원격 구분 포함)."""

    def __init__(self, source_id, total, avail, *, remote=False, active=True):
        self.source_id = source_id
        self.is_remote = remote
        self.is_active = active
        self._total = total
        self._avail = avail

    def get_total_space(self):
        return self._total

    def get_available_space(self):
        return self._avail


class _PoolWithSources(_FakeStoragePool):
    def __init__(self, sources):
        super().__init__()
        self.sources = sources


@pytest.mark.asyncio
async def test_storage_reports_live_local_usage(tmp_path):
    """데몬이 마운트한 로컬 소스의 실시간 사용량을 돌려준다.

    서버 레지스트리 값은 소스 재신고 주기만큼 뒤처지므로, 백업 중 용량을 따라가려면
    데몬이 들고 있는 StoragePool을 직접 읽어야 한다.
    """
    db = str(tmp_path / "m.db")
    pool = _PoolWithSources([
        _FakeSource("loop-a", 1000, 400),           # used 600
        _FakeSource("remote-x", 9999, 1, remote=True),  # 원격은 제외
    ])
    server = DaemonControlServer(pool, _FakeSync(), db)
    await server.start()
    try:
        rows = await asyncio.to_thread(daemon_control.storage_via_daemon, db)
        assert [r["source_id"] for r in rows] == ["loop-a"]
        assert rows[0]["total"] == 1000
        assert rows[0]["used"] == 600
        assert rows[0]["state"] == "ready"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_storage_marks_inactive_source_initialising(tmp_path):
    """아직 준비되지 않은 소스는 initializing으로 보고한다."""
    db = str(tmp_path / "m.db")
    pool = _PoolWithSources([_FakeSource("loop-b", 100, 100, active=False)])
    server = DaemonControlServer(pool, _FakeSync(), db)
    await server.start()
    try:
        rows = await asyncio.to_thread(daemon_control.storage_via_daemon, db)
        assert rows[0]["state"] == "initializing"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_storage_survives_source_error(tmp_path):
    """용량 조회가 실패하는 소스가 있어도 나머지를 보고한다(미상은 None)."""
    class _Broken(_FakeSource):
        def get_available_space(self):
            raise OSError("image unreadable")

    db = str(tmp_path / "m.db")
    pool = _PoolWithSources([
        _Broken("loop-bad", 100, 0),
        _FakeSource("loop-ok", 200, 50),
    ])
    server = DaemonControlServer(pool, _FakeSync(), db)
    await server.start()
    try:
        rows = await asyncio.to_thread(daemon_control.storage_via_daemon, db)
        by_id = {r["source_id"]: r for r in rows}
        assert by_id["loop-bad"]["used"] is None
        assert by_id["loop-ok"]["used"] == 150
    finally:
        await server.stop()


def test_storage_returns_none_without_daemon(tmp_path):
    """데몬이 없으면 None — 호출자는 서버 레지스트리 값을 그대로 쓴다."""
    assert daemon_control.storage_via_daemon(str(tmp_path / "none.db")) is None
