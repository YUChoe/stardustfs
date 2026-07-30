"""하단 상태바 — daemon 점, 스토리지·디바이스 요약, 진행 표시, 인라인 배너.

StardustApp에 믹스인으로 결합한다. VSCode 풍 한 줄 요약이며, 여기서 메타데이터
변경을 폴링해 목록 자동 새로고침과 복제 진행 표시를 함께 처리한다.

오류는 모달 대신 상태바 위 인라인 배너로 알린다 — 실패가 이어질 때 모달이 쌓이면
사용자가 확인 버튼만 반복해서 눌러야 한다. 같은 메시지가 반복되면 새 배너를 쌓지
않고 횟수만 갱신한다.
"""

from __future__ import annotations

import logging
from tkinter import ttk

from stardustlib.gui import actions, theme
from stardustlib.gui.format import shorten
from stardustlib.gui.widgets import tooltip

logger = logging.getLogger(__name__)

# 메타데이터 변경 감지 + 복제 진행 조회 주기(ms).
_META_POLL_MS = 3000

# 배너 레벨별 색 키(theme.PALETTE).
_BANNER_COLOURS = {
    "error": "danger",
    "warning": "accent",
    "info": "fg_subtle",
}


class StatusBarMixin:
    """상태바 구성 + 상태 문구 갱신 + 인라인 배너 + 메타데이터 폴링."""

    def _build_statusbar(self, parent) -> None:
        """인라인 배너 + (● daemon · 스토리지 · 디바이스 · 진행 · 백업 요약)."""
        t = self.t
        statusbar = ttk.Frame(parent, padding=(10, 4))
        statusbar.pack(fill="x", side="bottom")
        self.daemon_label = ttk.Label(statusbar, text=t["daemon_unknown"])
        self.daemon_label.pack(side="left")
        self._daemon_detail = t["daemon_unknown"]
        tooltip.attach(self.daemon_label, lambda: self._daemon_detail)
        ttk.Separator(statusbar, orient="vertical").pack(
            side="left", fill="y", padx=8)
        self.storage_label = ttk.Label(statusbar, text="")
        self.storage_label.pack(side="left")
        ttk.Separator(statusbar, orient="vertical").pack(
            side="left", fill="y", padx=8)
        self.device_label = ttk.Label(statusbar, text="")
        self.device_label.pack(side="left")
        self.backup_status = ttk.Label(statusbar, text="", anchor="e")
        self.backup_status.pack(side="right")
        # 오른쪽 구간도 왼쪽과 같은 방식으로 구분한다(구분선이 없으면 진행 문구와
        # 백업 요약이 한 문장처럼 붙어 읽힌다).
        ttk.Separator(statusbar, orient="vertical").pack(
            side="right", fill="y", padx=8)
        # 상태 문구는 왼쪽 그룹과 맞닿지 않도록 왼쪽 여백을 넉넉히 준다(좁은 창에서
        # '디바이스: 1/2 온라인'과 '백업 중: …'이 한 문장처럼 붙어 읽혔다).
        self.status = ttk.Label(statusbar, text="", anchor="w")
        self.status.pack(side="right", padx=(16, 0))
        self.progress = ttk.Progressbar(
            statusbar, mode="determinate", length=110)
        # 진행 중에만 pack한다(_set_progress).
        ttk.Separator(parent, orient="horizontal").pack(fill="x", side="bottom")

        # 배너는 상태바 위에 놓인다(있을 때만 pack).
        self._banner = ttk.Frame(parent, padding=(10, 4))
        self._banner_label = ttk.Label(self._banner, text="", anchor="w")
        self._banner_label.pack(side="left", fill="x", expand=True)
        ttk.Button(self._banner, text="✕", width=3,
                   command=self._clear_banner).pack(side="right")
        self._banner_message = ""
        self._banner_count = 0

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    def _show_device_summary(self, s: dict) -> None:
        """디바이스 온라인/전체 요약을 상태바에 표시한다."""
        if not s:
            return
        self.device_label.config(text=self.t["device_status"].format(
            online=s.get("online", 0), total=s.get("total", 0),
        ))

    # --- 인라인 배너 ---

    def _show_banner(self, message: str, *, level: str = "error") -> None:
        """오류/경고를 상태바 위 배너로 알린다(모달 대체).

        같은 message가 이어지면 배너를 새로 쌓지 않고 발생 횟수만 갱신한다.
        """
        banner = getattr(self, "_banner", None)
        if banner is None:
            logger.warning("배너 표시 불가(상태바 미구성): %s", message)
            return
        if message == self._banner_message:
            self._banner_count += 1
        else:
            self._banner_message = message
            self._banner_count = 1
        text = message
        if self._banner_count > 1:
            text += self.t["banner_repeat"].format(count=self._banner_count)
        self._banner_label.config(
            text=text,
            foreground=theme.text_colour(
                _BANNER_COLOURS.get(level, "danger"),
                dark=self.theme == "dark"),
        )
        if not banner.winfo_ismapped():
            # 상태바 바로 위(상태바보다 나중에 bottom pack → 위쪽).
            banner.pack(fill="x", side="bottom", before=self.status.master)

    def _clear_banner(self) -> None:
        banner = getattr(self, "_banner", None)
        if banner is None:
            return
        self._banner_message = ""
        self._banner_count = 0
        banner.pack_forget()

    # --- 진행 막대 ---

    def _set_progress(self, done: int, total: int) -> None:
        """상태바 진행 막대. total이 0 이하면 막대를 감춘다."""
        bar = getattr(self, "progress", None)
        if bar is None:
            return
        if total <= 0:
            bar.pack_forget()
            return
        bar.configure(maximum=total, value=done)
        if not bar.winfo_ismapped():
            bar.pack(side="right", padx=(0, 8))

    # --- 메타데이터 변경 감지 → 자동 새로고침 ---

    def _mark_meta_seen(self) -> None:
        """현재 메타데이터 mtime을 '본 것'으로 기록한다(자동 새로고침 기준점)."""
        if self.config_path:
            try:
                self._last_meta_mtime = actions.metadata_mtime(self.config_path)
            except Exception:  # noqa: BLE001
                pass

    def _poll_meta(self) -> None:
        """daemon이 메타데이터를 갱신(동기화 등)하면 목록을 자동 새로고침한다.

        목록만 가볍게 갱신(counts=False)해 삭제/추가가 수동 새로고침 없이 반영된다.
        백업 수(온라인 조회)는 수동 새로고침에서만 갱신한다. 같은 주기로 복제 진행
        상태도 읽어 상태바에 표시한다(대용량 백업이 멈춘 것처럼 보이지 않게).
        """
        try:
            if self.config_path and self._logged_in():
                m = actions.metadata_mtime(self.config_path)
                if m > self._last_meta_mtime:
                    self._last_meta_mtime = m
                    self.refresh(counts=False)
                cfg = self.config_path
                self.worker.submit(
                    lambda: actions.replication_progress(cfg),
                    self._show_progress,
                )
        except Exception:  # noqa: BLE001 — 폴링 실패는 무시
            pass
        self.root.after(_META_POLL_MS, self._poll_meta)

    def _show_progress(self, ok, payload) -> None:
        """복제 진행 상태를 상태바에 표시한다(없으면 기존 표시 유지).

        daemon 미실행·조회 실패(payload=None)면 아무것도 하지 않는다. 업로드가
        진행 중이면 그 표시를 덮지 않는다 — 사용자가 방금 시작한 작업이 우선이다.
        """
        if getattr(self, "_uploads", None):
            return
        if not ok or not payload or not payload.get("active"):
            # 진행이 끝났으면 상태바를 기본 문구로 되돌리고 최종 용량을 반영한다.
            if self._showing_progress:
                self._showing_progress = False
                self._set_status(self.t["ready"])
                self._set_progress(0, 0)
                self._refresh_mgmt()
            return
        name = shorten(payload.get("path", "").rsplit("/", 1)[-1])
        key = (
            "backup_progress_reading"
            if payload.get("stage") == "reading" else "backup_progress"
        )
        if not self._showing_progress:
            # 백업 시작 — 짧은 주기 갱신으로 전환되기 전에 한 번 당겨 온다.
            self._refresh_mgmt()
        self._showing_progress = True
        done, total = payload.get("done", 0), payload.get("total", 0)
        self._set_status(self.t[key].format(name=name, done=done, total=total))
        self._set_progress(done, total)
