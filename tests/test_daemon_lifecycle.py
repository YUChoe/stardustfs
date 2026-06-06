"""daemon 라이프사이클(start/status/stop) 테스트.

제어 파일 기반 동작을 시그널 없이 결정적으로 검증한다.
serve()는 워커 스레드에서 실행하고(메인 스레드가 아니라 signal.signal은 건너뜀)
정지 센티넬로 graceful 종료를 확인한다.
"""

import asyncio
import os
import threading
import time

from stardustlib import daemon


def test_status_no_control_file(tmp_path):
    db = str(tmp_path / "meta.db")
    status = daemon.read_status(db)
    assert status["running"] is False
    assert status["stale"] is False


def test_status_fresh_control_file(tmp_path):
    db = str(tmp_path / "meta.db")
    now = time.time()
    daemon._write_control(daemon._control_path(db), now, now)
    status = daemon.read_status(db)
    assert status["running"] is True
    assert status["stale"] is False
    assert status["pid"] == os.getpid()


def test_status_stale_control_file(tmp_path):
    db = str(tmp_path / "meta.db")
    now = time.time()
    # heartbeat가 STALE 임계값보다 오래됨 → 사망 추정
    daemon._write_control(daemon._control_path(db), now - 100, now - 100)
    status = daemon.read_status(db)
    assert status["running"] is False
    assert status["stale"] is True


def test_request_stop_when_not_running(tmp_path):
    db = str(tmp_path / "meta.db")
    result = daemon.request_stop(db)
    assert result["stopped"] is False
    assert result["reason"] == "not_running"


def test_signal_stop_not_running(tmp_path):
    db = str(tmp_path / "meta.db")
    assert daemon.signal_stop(db) == {"signalled": False, "reason": "not_running"}


def test_signal_stop_creates_sentinel(tmp_path):
    db = str(tmp_path / "meta.db")
    now = time.time()
    daemon._write_control(daemon._control_path(db), now, now)
    res = daemon.signal_stop(db)
    assert res["signalled"] is True
    assert os.path.exists(daemon._stop_path(db))


def test_write_control_tolerates_replace_failure(tmp_path, monkeypatch):
    """os.replace가 PermissionError(WinError 5 등)면 크래시하지 않고 False 반환."""
    db = str(tmp_path / "meta.db")
    calls = {"n": 0}

    def boom(_a, _b):
        calls["n"] += 1
        raise PermissionError("locked")

    monkeypatch.setattr(daemon.os, "replace", boom)
    ok = daemon._write_control(daemon._control_path(db), 1.0, 1.0)
    assert ok is False
    assert calls["n"] == 3  # 재시도 3회 후 포기


def test_write_control_success_returns_true(tmp_path):
    db = str(tmp_path / "meta.db")
    now = time.time()
    assert daemon._write_control(daemon._control_path(db), now, now) is True
    assert daemon.read_status(db)["running"] is True


def test_signal_reload_not_running(tmp_path):
    db = str(tmp_path / "meta.db")
    assert daemon.signal_reload(db) == {"signalled": False, "reason": "not_running"}


def test_signal_reload_creates_sentinel(tmp_path):
    db = str(tmp_path / "meta.db")
    now = time.time()
    daemon._write_control(daemon._control_path(db), now, now)
    res = daemon.signal_reload(db)
    assert res["signalled"] is True
    assert os.path.exists(daemon._reload_path(db))


def test_serve_invokes_on_reload(tmp_path):
    db = str(tmp_path / "meta.db")
    reloaded = {"n": 0}

    async def _cleanup():
        pass

    async def _on_reload():
        reloaded["n"] += 1

    def _run():
        asyncio.run(daemon.serve(db, _cleanup, on_reload=_on_reload))

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    for _ in range(100):
        if daemon.read_status(db).get("running"):
            break
        time.sleep(0.05)

    daemon.signal_reload(db)
    # 다음 틱(~1s)에 on_reload 호출 + 센티넬 제거
    for _ in range(100):
        if reloaded["n"] > 0:
            break
        time.sleep(0.05)
    assert reloaded["n"] >= 1
    assert not os.path.exists(daemon._reload_path(db))  # 센티넬 소비됨

    daemon.request_stop(db)
    worker.join(timeout=5)


def test_serve_lifecycle_start_status_stop(tmp_path):
    db = str(tmp_path / "meta.db")
    cleaned = {"done": False}

    async def _cleanup():
        cleaned["done"] = True

    def _run():
        asyncio.run(daemon.serve(db, _cleanup))

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    # 제어 파일이 생겨 running이 될 때까지 대기
    for _ in range(100):
        if daemon.read_status(db).get("running"):
            break
        time.sleep(0.05)
    assert daemon.read_status(db)["running"] is True

    # 정지 요청 → graceful 종료 + 제어 파일 정리 확인
    result = daemon.request_stop(db)
    assert result["stopped"] is True

    worker.join(timeout=5)
    assert worker.is_alive() is False
    assert cleaned["done"] is True
    assert not os.path.exists(daemon._control_path(db))
    assert not os.path.exists(daemon._stop_path(db))
