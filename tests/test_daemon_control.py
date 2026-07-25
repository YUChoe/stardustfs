"""데몬 전송 위임 제어 채널(put/get) 테스트 — 로컬 aiohttp 서버 + httpx 클라이언트."""
from __future__ import annotations

import asyncio
import os

import pytest

from stardustlib import daemon_control
from stardustlib.daemon_control import DaemonControlServer, transfer_via_daemon


class _FakeJbod:
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
    jbod, sync = _FakeJbod(), _FakeSync()
    server = DaemonControlServer(jbod, sync, db)
    await server.start()
    try:
        src = tmp_path / "up.bin"
        src.write_bytes(b"hello-delegated" * 1000)
        # put 위임 (httpx sync는 스레드에서 — 이벤트 루프 블로킹 방지)
        res = await asyncio.to_thread(
            transfer_via_daemon, db, "put", "/f", str(src)
        )
        assert res["ok"] is True and res["bytes"] == len(src.read_bytes())
        assert jbod.store["/f"] == src.read_bytes()
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
    server = DaemonControlServer(_FakeJbod(), _FakeSync(), db)
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
    server = DaemonControlServer(_FakeJbod(), _FakeSync(), db)
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
        _FakeJbod(), _FakeSync(), db, repl_scheduler=sched
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
    jbod = _FakeJbod()
    server = DaemonControlServer(jbod, _FakeSync(), db)  # repl_scheduler=None
    await server.start()
    try:
        src = tmp_path / "up.bin"
        src.write_bytes(b"data")
        res = await asyncio.to_thread(
            transfer_via_daemon, db, "put", "/f", str(src)
        )
        assert res["ok"] is True
        assert jbod.store["/f"] == b"data"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_manual_announce_endpoint(tmp_path):
    """수동 백업 요청(GUI 컨텍스트 메뉴)이 경로들을 스케줄러에 등록한다."""
    db = str(tmp_path / "m.db")
    sched = _FakeScheduler()
    server = DaemonControlServer(
        _FakeJbod(), _FakeSync(), db, repl_scheduler=sched
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
    server = DaemonControlServer(_FakeJbod(), _FakeSync(), db)
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
