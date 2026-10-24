"""하단 상태바 — 데몬 점, 스토리지·디바이스 요약, 진행 표시, 인라인 배너.

StardustApp에 믹스인으로 결합한다. VSCode 풍 한 줄 요약이며, 여기서 메타데이터
변경을 폴링해 목록 자동 새로고침과 복제 진행 표시를 함께 처리한다.

오류는 모달 대신 상태바 위 인라인 배너로 알린다 — 실패가 이어질 때 모달이 쌓이면
사용자가 확인 버튼만 반복해서 눌러야 한다. 같은 메시지가 반복되면 새 배너를 쌓지
않고 횟수만 갱신한다.

한 줄에 항목이 다섯 개라 좁은 창에서는 상태 문구가 모자란다. 요약은 짧게 쓰고 자세한
값은 툴팁으로 돌리며, 상태 문구는 남은 폭에 맞춰 '…'로 줄이고 전체를 툴팁에 둔다.
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from stardustlib.gui import actions, theme
from stardustlib.gui.format import fit_text, shorten
from stardustlib.gui.widgets import tooltip

logger = logging.getLogger(__name__)

# 메타데이터 변경 감지 + 복제 진행 조회 주기(ms).
_META_POLL_MS = 3000

# 배너 레벨별 (색 키, 아이콘).
_BANNER_LEVELS = {
    "error": ("danger", "⚠"),
    "warning": ("attention", "⚠"),
    "info": ("fg_subtle", "ℹ"),
}

# 상태바 좌우 안쪽 여백(px, padding=(10, 4)의 좌+우).
_BAR_PADX = 20


def _pad_total(value) -> int:
    """pack_info의 padx 값(int, 'a b', (a, b))을 좌우 합으로."""
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value * 2
    parts = str(value).replace("{", "").replace("}", "").split() \
        if not isinstance(value, (tuple, list)) else list(value)
    nums = [int(str(p)) for p in parts]
    return nums[0] * 2 if len(nums) == 1 else sum(nums[:2])


class StatusBarMixin:
    """상태바 구성 + 상태 문구 갱신 + 인라인 배너 + 메타데이터 폴링."""

    def _build_statusbar(self, parent) -> None:
        """인라인 배너 + (● 데몬 · 스토리지 · 디바이스 · 진행 · 백업 요약)."""
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
        self._storage_tip = ""
        tooltip.attach(self.storage_label, lambda: self._storage_tip)
        ttk.Separator(statusbar, orient="vertical").pack(
            side="left", fill="y", padx=8)
        self.device_label = ttk.Label(statusbar, text="")
        self.device_label.pack(side="left")
        self.backup_status = ttk.Label(statusbar, text="", anchor="e")
        self.backup_status.pack(side="right")
        self._backup_tip = ""
        tooltip.attach(self.backup_status, lambda: self._backup_tip)
        # 오른쪽 구간도 왼쪽과 같은 방식으로 구분한다(구분선이 없으면 진행 문구와
        # 백업 요약이 한 문장처럼 붙어 읽힌다).
        ttk.Separator(statusbar, orient="vertical").pack(
            side="right", fill="y", padx=8)
        # 상태 문구는 왼쪽 그룹과 맞닿지 않도록 왼쪽 여백을 넉넉히 준다.
        self.status = ttk.Label(statusbar, text="", anchor="w")
        self.status.pack(side="right", padx=(16, 0))
        self._status_full = ""
        # 줄였을 때만 전체 문구를 툴팁으로 보인다.
        tooltip.attach(self.status, lambda: (
            self._status_full
            if self.status.cget("text") != self._status_full else ""))
        self.progress = ttk.Progressbar(
            statusbar, mode="determinate", length=theme.px(self.root, 110))
        self._progress_mode: str | None = None  # None | determinate | indeterminate
        # 진행 중에만 pack한다(_set_progress / _set_activity).
        ttk.Separator(parent, orient="horizontal").pack(fill="x", side="bottom")

        # 이웃 항목의 폭이 바뀌면 상태 문구가 쓸 수 있는 폭도 바뀐다.
        for child in (statusbar, self.daemon_label, self.storage_label,
                      self.device_label, self.backup_status, self.progress):
            child.bind("<Configure>", self._schedule_fit_status, add="+")
        self._fit_pending = False

        # 배너는 상태바 위에 놓인다(있을 때만 pack).
        self._banner = ttk.Frame(parent, padding=(10, 4))
        self._banner_icon = ttk.Label(self._banner, text="")
        self._banner_icon.pack(side="left", padx=(0, 6))
        self._banner_label = ttk.Label(self._banner, text="", anchor="w")
        self._banner_label.pack(side="left", fill="x", expand=True)
        ttk.Button(self._banner, text="✕", width=3,
                   command=self._clear_banner).pack(side="right")
        self._banner_message = ""
        self._banner_count = 0
        self._banner_level = "error"

    # --- 상태 문구 ---

    def _set_status(self, text: str) -> None:
        self._status_full = text
        self._fit_status()

    def _schedule_fit_status(self, _e=None) -> None:
        if self._fit_pending:
            return
        self._fit_pending = True

        def run() -> None:
            self._fit_pending = False
            self._fit_status()

        self.root.after_idle(run)

    def _status_room(self) -> int:
        """상태 문구가 쓸 수 있는 폭(px). 배치 전이면 0(줄이지 않음)."""
        label = self.status
        bar = label.master
        width = bar.winfo_width()
        if width <= 1:
            return 0
        used = 0
        for widget in bar.pack_slaves():
            if widget is label:
                continue
            used += widget.winfo_reqwidth() + _pad_total(
                widget.pack_info().get("padx"))
        used += _pad_total(label.pack_info().get("padx"))
        return width - used - _BAR_PADX

    def _fit_status(self) -> None:
        label = getattr(self, "status", None)
        if label is None:
            return
        full = self._status_full
        try:
            room = self._status_room()
            if room <= 0:
                label.config(text=full)
                return
            font = tkfont.Font(font=ttk.Style().lookup("TLabel", "font")
                               or "TkDefaultFont")
            label.config(text=fit_text(full, room, font.measure))
        except tk.TclError:  # 재구성 중 파괴된 위젯
            pass

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

        같은 message가 이어지면 배너를 새로 쌓지 않고 발생 횟수만 갱신한다. 레벨은
        색과 아이콘 둘 다로 구분한다(색만으로는 구분하지 못하는 사용자가 있다).
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
        self._banner_level = level
        text = message
        if self._banner_count > 1:
            text += self.t["banner_repeat"].format(count=self._banner_count)
        colour_key, icon = _BANNER_LEVELS.get(level, _BANNER_LEVELS["error"])
        colour = theme.text_colour(colour_key, dark=self.theme == "dark")
        self._banner_label.config(text=text, foreground=colour)
        self._banner_icon.config(text=icon, foreground=colour)
        if not banner.winfo_ismapped():
            # 상태바 바로 위. side=bottom은 pack 순서가 늦을수록 위에 놓이므로
            # 상태바 다음 순서로 넣는다(before=면 상태바 아래로 간다).
            banner.pack(fill="x", side="bottom", after=self.status.master)

    def _clear_banner(self) -> None:
        banner = getattr(self, "_banner", None)
        if banner is None:
            return
        self._banner_message = ""
        self._banner_count = 0
        banner.pack_forget()

    # --- 진행 막대 ---

    def _set_progress(self, done: int, total: int) -> None:
        """상태바 진행 막대(진행률). total이 0 이하면 막대를 감춘다."""
        bar = getattr(self, "progress", None)
        if bar is None:
            return
        if total <= 0:
            if self._progress_mode == "determinate":
                bar.pack_forget()
                self._progress_mode = None
            return
        if self._progress_mode == "indeterminate":
            bar.stop()
        bar.configure(mode="determinate", maximum=total, value=done)
        self._progress_mode = "determinate"
        if not bar.winfo_ismapped():
            bar.pack(side="right", padx=(0, 8))

    def _set_activity(self, on: bool) -> None:
        """진행률을 알 수 없는 작업(다운로드·삭제 등) 중에 무한 막대를 보인다.

        진행률 막대(업로드·백업)가 떠 있으면 그쪽이 우선이다.
        """
        bar = getattr(self, "progress", None)
        if bar is None or self._progress_mode == "determinate":
            return
        try:
            if on and self._progress_mode is None:
                bar.configure(mode="indeterminate", maximum=100, value=0)
                bar.start(15)
                bar.pack(side="right", padx=(0, 8))
                self._progress_mode = "indeterminate"
            elif not on and self._progress_mode == "indeterminate":
                bar.stop()
                bar.pack_forget()
                self._progress_mode = None
        except tk.TclError:  # 재구성 중 파괴된 위젯
            self._progress_mode = None

    # --- 메타데이터 변경 감지 → 자동 새로고침 ---

    def _mark_meta_seen(self) -> None:
        """현재 메타데이터 mtime을 '본 것'으로 기록한다(자동 새로고침 기준점).

        파일 stat이지만 설정 로드가 끼어 있어 메인 스레드에서 하지 않는다.
        """
        cfg = self.config_path
        if not cfg:
            return

        def done(ok, mtime) -> None:
            if ok and cfg == self.config_path:
                self._last_meta_mtime = max(self._last_meta_mtime, mtime)

        self.io_worker.submit(lambda: actions.metadata_mtime(cfg), done)

    def _poll_meta(self) -> None:
        """데몬이 메타데이터를 갱신(동기화 등)하면 목록을 자동 새로고침한다.

        목록만 가볍게 갱신(counts=False)해 삭제/추가가 수동 새로고침 없이 반영된다.
        백업 수(온라인 조회)는 수동 새로고침에서만 갱신한다. 같은 주기로 복제 진행
        상태도 읽어 상태바에 표시한다(대용량 백업이 멈춘 것처럼 보이지 않게).
        """
        try:
            self._poll_login()
            if self.config_path and self._logged_in():
                cfg = self.config_path
                self._submit_once(
                    "meta", lambda: actions.metadata_mtime(cfg),
                    lambda ok, m: self._on_meta_mtime(cfg, ok, m), io=True)
                self._submit_once(
                    "progress", lambda: actions.replication_progress(cfg),
                    self._show_progress, io=True)
        except Exception:  # noqa: BLE001 — 폴링 실패는 무시
            logger.debug("메타데이터 폴링 실패", exc_info=True)
        self.root.after(_META_POLL_MS, self._poll_meta)

    def _on_meta_mtime(self, cfg: str, ok: bool, mtime) -> None:
        if not ok or cfg != self.config_path:
            return
        if mtime > self._last_meta_mtime:
            self._last_meta_mtime = mtime
            self.refresh(counts=False)

    def _show_progress(self, ok, payload) -> None:
        """복제 진행 상태를 상태바에 표시한다(없으면 기존 표시 유지).

        데몬 미실행·조회 실패(payload=None)면 아무것도 하지 않는다. 업로드가
        진행 중이면 그 표시를 덮지 않는다 — 사용자가 방금 시작한 작업이 우선이다.
        """
        if getattr(self, "_uploads", None):
            return
        if not ok or not payload or not payload.get("active"):
            # 진행이 끝났으면 상태바를 기본 문구로 되돌리고 최종 용량을 반영한다.
            if self._showing_progress:
                self._showing_progress = False
                self._progress_text = ""
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
        self._progress_text = self.t[key].format(name=name, done=done, total=total)
        self._set_status(self._progress_text)
        self._set_progress(done, total)
