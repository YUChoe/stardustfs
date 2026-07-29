"""daemon 감독 — 생존 폴링, 자동 재시작, 리로드 신호.

daemon은 항상 온라인으로 감독한다. 정지·중단(stale)이 보이면 쿨다운을 두고
재시작하고, 정지→실행 전이에서는 조회 세션을 다시 연다.
StardustApp에 믹스인으로 결합한다.
"""

from __future__ import annotations

import logging
import time

from stardustlib.gui import actions

logger = logging.getLogger(__name__)

# daemon 재시작 쿨다운(초): 한 번 시작하면 heartbeat 안정화까지 추가 재시작 보류.
# 상태 폴링 주기(5s)보다 충분히 커서 부팅 중 중복 시작을 막는다.
_DAEMON_RESTART_COOLDOWN = 20.0

# daemon 생존 확인 주기(ms). 제어 파일 읽기라 서버 호출은 없다.
_DAEMON_POLL_MS = 5000


class DaemonControlMixin:
    """daemon 상태 표시 + 감독."""

    def _refresh_daemon(self) -> None:
        """daemon 생존만 주기적으로 확인한다(제어 파일 읽기 — 서버 호출 없음).

        디바이스 온라인 카운트는 하단 패널 갱신(_populate_mgmt)이 같은 응답으로
        계산한다. 여기서 따로 조회하면 주기마다 GET /devices가 한 번 더 나간다.
        """
        if self.config_path:
            cfg = self.config_path
            self.worker.submit(lambda: actions.daemon_status(cfg), self._on_daemon)
        self.root.after(_DAEMON_POLL_MS, self._refresh_daemon)

    def _daemon_dot(self, text: str, color: str) -> None:
        self.daemon_label.config(text="● " + text, foreground=color)

    def _on_daemon(self, ok, payload) -> None:
        grey, green, orange = "#9aa0a6", "#2da44e", "#d29922"
        if not ok:
            self._daemon_dot(self.t["daemon_unknown"], grey)
            return
        if payload.get("running"):
            self._daemon_dot(
                self.t["daemon_running"].format(pid=payload.get("pid")), green
            )
            if self._daemon_was_running is False:
                self._reopen_after_daemon_start()
            self._daemon_was_running = True
            return
        # running이 아니면(정지 또는 stale=중단/행) 항상 온라인 유지를 위해 재시작.
        self._daemon_was_running = False
        if payload.get("stale"):
            self._daemon_dot(self.t["daemon_stale"], orange)
        else:
            self._daemon_dot(self.t["daemon_stopped"], grey)
        self._ensure_daemon()

    def _reopen_after_daemon_start(self) -> None:
        """daemon이 새로 뜬 직후 조회 세션을 버리고 목록을 다시 읽는다.

        daemon이 없을 때 연 세션은 루프백 FAT 이미지가 아직 없어 소스가 비활성으로
        잡힌다(조회 세션은 read_only라 이미지를 만들지 않는다). daemon이 이미지를
        포맷한 뒤 세션을 다시 열어야 스토리지가 정상으로 보인다.
        """
        cfg = self.config_path
        if not cfg:
            return
        # 세션 close는 세션을 만든 워커 스레드에서 해야 한다(sqlite 스레드 제약).
        self.worker.submit(
            lambda: actions.invalidate(cfg),
            lambda *_a: self.refresh(),
        )

    def _reload_daemon(self) -> None:
        """실행 중인 daemon에 config 리로드 신호를 보낸다(무중단 remount).

        소스 추가/분리 후 호출한다 — daemon은 시작 시 config로 소스를 mount하므로,
        리로드해야 변경된 로컬 소스를 다시 읽어 remount하고 서버 레지스트리에 즉시
        재신고한다. 전체 재시작과 달리 P2P/동기화를 중단하지 않는다.
        """
        cfg = self.config_path
        if not cfg:
            return
        self._set_status(self.t["daemon_restart"])
        self.worker.submit(
            lambda: actions.daemon_signal_reload(cfg), lambda *_a: None
        )

    def _ensure_daemon(self) -> None:
        """daemon이 떠 있지 않으면 재시작한다(쿨다운으로 재시작 폭주 방지).

        시작 후 heartbeat가 기록되기까지 시간이 필요하므로, 한 번 시작하면
        쿨다운 동안에는 추가 재시작을 시도하지 않는다.
        """
        if not self.config_path:
            return
        now = time.time()
        if now < self._daemon_restart_until:
            return  # 직전 시작이 진행/안정화 중
        self._daemon_restart_until = now + _DAEMON_RESTART_COOLDOWN
        self._set_status(self.t["daemon_starting"])
        self._daemon_start()

    def _daemon_start(self) -> None:
        cfg = self.config_path
        # 시작 후 pid를 상태바에 쓰지 않는다(daemon 상태는 좌측 점이 폴링으로 표시).
        # 시작 직후 1회 상태 점검으로 점을 갱신한다(별도 폴링 루프 추가 없이).
        self._submit(
            lambda: actions.daemon_start(cfg),
            lambda _pid: self.worker.submit(
                lambda: actions.daemon_status(cfg), self._on_daemon
            ),
            self.t["daemon_start_busy"],
        )
