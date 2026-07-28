"""프로세스 단일 인스턴스 가드 (제어 파일 + heartbeat).

GUI가 여러 개 뜨면 같은 스토리지·메타데이터를 다투고, 각자 daemon을 감독하려
들어 서로의 판단을 무너뜨린다. daemon과 같은 방식(제어 파일에 pid·heartbeat를
쓰고 신선도로 생존을 판정)으로 하나만 뜨게 한다 — Windows에서는 시그널·os.kill로
생존을 확인할 수 없기 때문이다.

두 번째 실행은 조용히 죽지 않고 포커스 센티넬을 남긴다. 먼저 뜬 창이 그것을 보고
자기를 앞으로 끌어올리므로, 사용자에게는 "아이콘을 다시 눌렀더니 창이 올라왔다"로
보인다(트레이로 최소화된 경우에도).
"""

from __future__ import annotations

import logging
import os
import time

from stardustlib.daemon import read_control, write_control

logger = logging.getLogger(__name__)

# heartbeat 갱신 간격(초). daemon과 같은 stale 임계(15초)를 쓰므로 그보다 충분히
# 짧아야 한다 — GUI는 Tk 메인루프에서 갱신하므로 창이 얼어붙으면 갱신도 멈추고,
# 그때는 새 인스턴스가 뜨는 것이 맞다.
BEAT_INTERVAL_SECONDS = 5.0


def user_state_dir() -> str:
    """사용자별 상태 디렉토리. 없으면 만든다(실패 시 예외를 올리지 않는다)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, ".stardustfs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.debug("상태 디렉토리를 만들 수 없습니다(%s): %s", path, e)
    return path


def gui_lock_path() -> str:
    """GUI 단일 인스턴스 제어 파일 경로.

    config가 아니라 사용자 단위다 — GUI는 config 없이도 뜨고 실행 중에 config를
    고를 수 있으므로, config별로 잠그면 나중에 같은 config를 여는 경로를 막지 못한다.
    """
    return os.path.join(user_state_dir(), "gui.lock.json")


def _focus_path(lock_path: str) -> str:
    return lock_path + ".focus"


def request_focus(lock_path: str) -> None:
    """먼저 뜬 인스턴스에 '앞으로 나와라' 센티넬을 남긴다(실패는 무시)."""
    try:
        with open(_focus_path(lock_path), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.debug("포커스 요청을 남기지 못했습니다: %s", e)


def consume_focus_request(lock_path: str) -> bool:
    """포커스 센티넬이 있으면 지우고 True. 락 보유자가 주기적으로 호출한다."""
    path = _focus_path(lock_path)
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True


class InstanceLock:
    """제어 파일 하나를 잡는 단일 인스턴스 락.

    heartbeat 갱신은 보유자가 beat()를 주기적으로 불러 수행한다(스레드를 쓰지
    않는다 — GUI는 메인루프의 after()로 부르면 되고, 그래야 창이 멈췄을 때
    heartbeat도 함께 멈춰 stale 판정이 실제 상태와 맞는다).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.started_at = 0.0
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def holder(self) -> dict:
        """현재 제어 파일이 가리키는 프로세스 상태(running/stale/pid...)."""
        return read_control(self.path)

    def acquire(self) -> bool:
        """락을 잡는다. 이미 살아 있는 보유자가 있으면 False.

        제어 파일을 쓸 수 없으면(권한/잠금) False다 — 자기 존재를 알릴 수 없으면
        다음 인스턴스가 중복으로 뜨는 것을 막을 수 없다.
        """
        if read_control(self.path).get("running"):
            return False
        now = time.time()
        if not write_control(self.path, now, now):
            return False
        self.started_at = now
        self._held = True
        return True

    def beat(self) -> None:
        """heartbeat를 갱신한다. 보유 중이 아니면 아무것도 하지 않는다."""
        if self._held:
            write_control(self.path, self.started_at, time.time())

    def release(self) -> None:
        """락을 놓고 제어 파일을 지운다(멱등)."""
        if not self._held:
            return
        self._held = False
        for path in (self.path, _focus_path(self.path)):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logger.debug("제어 파일 정리 실패(%s): %s", path, e)
