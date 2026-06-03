"""상주 daemon 라이프사이클 (start/status/stop) — 크로스플랫폼.

Windows에서는 graceful 종료를 POSIX 시그널로 보장할 수 없으므로(os.kill이
TerminateProcess로 매핑되어 핸들러가 실행되지 않음) 제어 파일 기반으로 동작한다.

- 제어 파일 ``{metadata_db}.daemon.json``: daemon이 ``{pid, started_at,
  heartbeat_at}``를 기록하고 주기적으로 heartbeat_at을 갱신한다. graceful 종료 시
  삭제한다.
- 정지 센티넬 ``{metadata_db}.daemon.stop``: ``daemon stop``이 생성하면 daemon이
  루프에서 감지해 graceful 종료한다.

status는 heartbeat_at 신선도로 생존을 판정한다(시그널/os.kill 미사용).
포그라운드 Ctrl+C(SIGINT)도 핸들러로 받아 같은 graceful 경로로 종료한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_TICK_SECONDS = 1.0          # 정지 신호 확인 주기
_HEARTBEAT_EVERY_TICKS = 5   # heartbeat 기록 주기(틱 단위) → 약 5초
_STALE_SECONDS = 15.0        # 이 시간 이상 heartbeat 없으면 stale(사망 추정)
_STOP_WAIT_SECONDS = 10.0    # stop 요청 후 종료 대기 한계


def _control_path(metadata_db: str) -> str:
    return metadata_db + ".daemon.json"


def _stop_path(metadata_db: str) -> str:
    return metadata_db + ".daemon.stop"


def _write_control(path: str, started_at: float, heartbeat_at: float) -> None:
    """제어 파일을 원자적으로 기록한다(tmp + replace)."""
    tmp = path + ".tmp"
    payload = {
        "pid": os.getpid(),
        "started_at": started_at,
        "heartbeat_at": heartbeat_at,
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def read_status(metadata_db: str) -> dict:
    """daemon 생존 상태를 반환한다.

    반환: {running: bool, stale: bool, pid, started_at, heartbeat_age}.
    제어 파일이 없으면 running=False, stale=False.
    """
    path = _control_path(metadata_db)
    if not os.path.exists(path):
        return {"running": False, "stale": False}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"running": False, "stale": True}

    age = time.time() - float(data.get("heartbeat_at", 0))
    running = age < _STALE_SECONDS
    return {
        "running": running,
        "stale": not running,
        "pid": data.get("pid"),
        "started_at": data.get("started_at"),
        "heartbeat_age": age,
    }


def request_stop(metadata_db: str) -> dict:
    """정지 센티넬을 만들고 daemon이 제어 파일을 지울 때까지 대기한다.

    반환: {stopped: bool, reason?: str}.
    """
    if not os.path.exists(_control_path(metadata_db)):
        return {"stopped": False, "reason": "not_running"}

    with open(_stop_path(metadata_db), "w", encoding="utf-8") as f:
        f.write("stop")

    deadline = time.time() + _STOP_WAIT_SECONDS
    while time.time() < deadline:
        if not os.path.exists(_control_path(metadata_db)):
            return {"stopped": True}
        time.sleep(0.3)
    return {"stopped": False, "reason": "timeout"}


class _StopState:
    """시그널 핸들러가 설정하는 정지 플래그."""

    def __init__(self) -> None:
        self.requested = False


async def serve(
    metadata_db: str, cleanup: Callable[[], Awaitable[None]]
) -> None:
    """제어 파일을 쓰고 정지 신호까지 heartbeat 루프를 돈 뒤 cleanup을 실행한다.

    정지 신호: SIGINT/SIGTERM(가능한 플랫폼) 또는 정지 센티넬 파일.
    종료 시 cleanup 코루틴 실행 후 제어/센티넬 파일을 정리한다.
    """
    control = _control_path(metadata_db)
    stop = _stop_path(metadata_db)

    # 이전 실행의 잔존 센티넬 제거
    if os.path.exists(stop):
        try:
            os.remove(stop)
        except OSError:
            pass

    state = _StopState()

    def _handler(signum, frame) -> None:  # noqa: ANN001
        state.requested = True

    restore: list = []
    sigterm = getattr(signal, "SIGTERM", None)
    for sig in (signal.SIGINT, sigterm):
        if sig is None:
            continue
        try:
            restore.append((sig, signal.signal(sig, _handler)))
        except (ValueError, OSError):
            # 메인 스레드가 아니거나 미지원 — 센티넬 기반 정지로 대체
            pass

    started = time.time()
    _write_control(control, started, started)
    logger.info(
        "daemon 시작: pid=%d (정지: Ctrl+C 또는 'daemon stop')", os.getpid()
    )

    try:
        tick = 0
        while not state.requested and not os.path.exists(stop):
            await asyncio.sleep(_TICK_SECONDS)
            tick += 1
            if tick % _HEARTBEAT_EVERY_TICKS == 0:
                _write_control(control, started, time.time())
    finally:
        logger.info("daemon 종료 중...")
        try:
            await cleanup()
        except Exception as e:  # noqa: BLE001
            logger.error("daemon cleanup 중 예외: %s", e, exc_info=True)
        for path in (control, stop):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        for sig, prev in restore:
            try:
                signal.signal(sig, prev)
            except (ValueError, OSError):
                pass
        logger.info("daemon 종료 완료")
