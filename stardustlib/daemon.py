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
import threading
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


def _reload_path(metadata_db: str) -> str:
    return metadata_db + ".daemon.reload"


def write_control(path: str, started_at: float, heartbeat_at: float) -> bool:
    """제어 파일을 원자적으로 기록한다(tmp + replace). 성공 여부를 반환한다.

    Windows에서 os.replace는 대상이 다른 프로세스(백신/인덱서/OneDrive/다른 daemon
    인스턴스 등)에 잠겨 있으면 PermissionError(WinError 5)로 실패할 수 있다. heartbeat
    한 번의 기록 실패가 daemon 전체를 죽이면 안 되므로, 짧게 재시도하고 그래도 실패하면
    경고만 남기고 False를 반환한다(다음 heartbeat에서 회복; 상태는 heartbeat 신선도로 판정).
    """
    tmp = path + ".tmp"
    payload = {
        "pid": os.getpid(),
        "started_at": started_at,
        "heartbeat_at": heartbeat_at,
    }
    for attempt in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
            return True
        except OSError as e:
            if attempt < 2:
                time.sleep(0.05)  # 일시적 파일 잠금 — 짧게 재시도
                continue
            logger.warning("제어 파일 기록 실패(무시, 다음 heartbeat 재시도): %s", e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False
    return False


# 하위 호환 별칭(기존 호출부·테스트). 신규 코드는 write_control을 쓴다.
_write_control = write_control


def read_control(path: str) -> dict:
    """제어 파일이 가리키는 프로세스의 생존 상태를 반환한다.

    반환: {running: bool, stale: bool, pid, started_at, heartbeat_age}.
    파일이 없으면 running=False, stale=False. heartbeat 신선도로만 판정한다
    (시그널/os.kill 미사용 — Windows에서 신뢰할 수 없다).
    """
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


def read_status(metadata_db: str) -> dict:
    """daemon 생존 상태를 반환한다(read_control의 daemon 제어 파일 판)."""
    return read_control(_control_path(metadata_db))


def signal_stop(metadata_db: str) -> dict:
    """정지 센티넬만 생성한다(대기 없음). daemon은 다음 틱(~1초)에 감지·종료한다.

    반환: {signalled: bool, reason?: str}. 제어 파일이 없으면 not_running.
    """
    if not os.path.exists(_control_path(metadata_db)):
        return {"signalled": False, "reason": "not_running"}
    with open(_stop_path(metadata_db), "w", encoding="utf-8") as f:
        f.write("stop")
    return {"signalled": True}


def signal_reload(metadata_db: str) -> dict:
    """리로드 센티넬을 생성한다(대기 없음). daemon은 다음 틱에 config의 로컬 소스를
    다시 읽어 remount한다. 제어 파일이 없으면 not_running."""
    if not os.path.exists(_control_path(metadata_db)):
        return {"signalled": False, "reason": "not_running"}
    with open(_reload_path(metadata_db), "w", encoding="utf-8") as f:
        f.write("reload")
    return {"signalled": True}


def request_stop(metadata_db: str) -> dict:
    """정지 센티넬을 만들고 daemon이 제어 파일을 지울 때까지 대기한다.

    반환: {stopped: bool, reason?: str}.
    """
    res = signal_stop(metadata_db)
    if not res.get("signalled"):
        return {"stopped": False, "reason": res.get("reason", "not_running")}

    deadline = time.time() + _STOP_WAIT_SECONDS
    while time.time() < deadline:
        if not os.path.exists(_control_path(metadata_db)):
            return {"stopped": True}
        time.sleep(0.3)
    return {"stopped": False, "reason": "timeout"}


class _StartupBeacon:
    """startup이 끝나기 전까지 제어 파일 heartbeat를 유지하는 백그라운드 스레드."""

    def __init__(self, control: str, started: float) -> None:
        self.control = control
        self.started = started
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="daemon-startup-beacon", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        interval = _TICK_SECONDS * _HEARTBEAT_EVERY_TICKS
        while not self._stop.wait(interval):
            _write_control(self.control, self.started, time.time())

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


# 이 프로세스가 잡은 startup 비콘(프로세스당 daemon은 하나뿐이라 모듈 전역).
_beacon: _StartupBeacon | None = None


def claim(metadata_db: str) -> bool:
    """이 프로세스를 유일한 daemon으로 등록한다. 이미 살아 있으면 False.

    startup(인증·서버 등록·스토리지 마운트)은 서버가 느리거나 응답하지 않으면
    수십 초가 걸린다. 그동안 제어 파일이 없으면 감시자(GUI)가 'daemon 없음'으로
    보고 새 인스턴스를 계속 띄워 중복이 쌓인다 — 여러 daemon이 같은 제어 파일과
    스토리지를 다투게 된다. 기동 즉시 제어 파일을 잡고 heartbeat를 유지해 그
    창(window)을 없앤다. serve()가 비콘을 이어받는다.

    제어 파일을 쓰지 못하면(권한/잠금) False를 반환한다 — 자기 존재를 알릴 수
    없는 daemon은 중복 판정 대상이 되지 않으므로 기동하지 않는다.
    """
    global _beacon
    if read_status(metadata_db).get("running"):
        return False

    started = time.time()
    control = _control_path(metadata_db)
    if not _write_control(control, started, started):
        return False

    _beacon = _StartupBeacon(control, started)
    _beacon.start()
    return True


def release_claim(metadata_db: str) -> None:
    """claim 이후 startup이 실패했을 때 제어 파일을 반납한다."""
    global _beacon
    if _beacon is None:
        return
    _beacon.release()
    _beacon = None
    try:
        os.remove(_control_path(metadata_db))
    except OSError:
        pass


class _StopState:
    """시그널 핸들러가 설정하는 정지 플래그."""

    def __init__(self) -> None:
        self.requested = False


async def serve(
    metadata_db: str,
    cleanup: Callable[[], Awaitable[None]],
    on_reload: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """제어 파일을 쓰고 정지 신호까지 heartbeat 루프를 돈 뒤 cleanup을 실행한다.

    정지 신호: SIGINT/SIGTERM(가능한 플랫폼) 또는 정지 센티넬 파일.
    리로드 신호: 리로드 센티넬 파일 → on_reload 콜백 실행(있으면) 후 센티넬 제거.
    종료 시 cleanup 코루틴 실행 후 제어/센티넬 파일을 정리한다.
    """
    control = _control_path(metadata_db)
    stop = _stop_path(metadata_db)
    reload_sentinel = _reload_path(metadata_db)

    # 이전 실행의 잔존 센티넬 제거
    for sentinel in (stop, reload_sentinel):
        if os.path.exists(sentinel):
            try:
                os.remove(sentinel)
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

    # startup 비콘이 있으면 이어받는다(기동 시각 승계 후 자체 루프로 전환).
    global _beacon
    started = time.time()
    if _beacon is not None:
        started = _beacon.started
        _beacon.release()
        _beacon = None

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
            # 리로드 센티넬: config 로컬 소스를 다시 읽어 remount(무중단)
            if os.path.exists(reload_sentinel):
                try:
                    os.remove(reload_sentinel)
                except OSError:
                    pass
                if on_reload is not None:
                    try:
                        await on_reload()
                        logger.info("daemon config 리로드 완료")
                    except Exception as e:  # noqa: BLE001 — 기존 구성 유지
                        logger.error(
                            "daemon config 리로드 실패(기존 구성 유지): %s",
                            e, exc_info=True,
                        )
    finally:
        logger.info("daemon 종료 중...")
        try:
            await cleanup()
        except Exception as e:  # noqa: BLE001
            logger.error("daemon cleanup 중 예외: %s", e, exc_info=True)
        for path in (control, stop, reload_sentinel):
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
