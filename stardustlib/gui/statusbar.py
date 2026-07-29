"""하단 상태바 — daemon 점, 스토리지·디바이스 요약, 진행 표시, 자동 새로고침.

StardustApp에 믹스인으로 결합한다. VSCode 풍 한 줄 요약이며, 여기서 메타데이터
변경을 폴링해 목록 자동 새로고침과 복제 진행 표시를 함께 처리한다.
"""

from __future__ import annotations

import logging
from tkinter import ttk

from stardustlib.gui import actions

logger = logging.getLogger(__name__)

# 메타데이터 변경 감지 + 복제 진행 조회 주기(ms).
_META_POLL_MS = 3000


class StatusBarMixin:
    """상태바 구성 + 상태 문구 갱신 + 메타데이터 폴링."""

    def _build_statusbar(self, parent) -> None:
        """● daemon · 스토리지 · 디바이스 · (전송 상태) · 백업 요약."""
        t = self.t
        statusbar = ttk.Frame(parent, padding=(10, 4))
        statusbar.pack(fill="x", side="bottom")
        self.daemon_label = ttk.Label(statusbar, text=t["daemon_unknown"])
        self.daemon_label.pack(side="left")
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
        # 왼쪽 구간과 같은 방식으로 구분한다(구분선이 없으면 진행 문구와 백업 요약이
        # 한 문장처럼 붙어 읽힌다).
        ttk.Separator(statusbar, orient="vertical").pack(
            side="right", fill="y", padx=8)
        self.status = ttk.Label(statusbar, text="", anchor="w")
        self.status.pack(side="right")
        ttk.Separator(parent, orient="horizontal").pack(fill="x", side="bottom")

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    def _show_device_summary(self, s: dict) -> None:
        """디바이스 온라인/전체 요약을 상태바에 표시한다."""
        if not s:
            return
        self.device_label.config(text=self.t["device_status"].format(
            online=s.get("online", 0), total=s.get("total", 0),
        ))

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

        daemon 미실행·조회 실패(payload=None)면 아무것도 하지 않는다.
        """
        if not ok or not payload or not payload.get("active"):
            # 진행이 끝났으면 상태바를 기본 문구로 되돌리고 최종 용량을 반영한다.
            if self._showing_progress:
                self._showing_progress = False
                self._set_status(self.t["ready"])
                self._refresh_mgmt()
            return
        name = payload.get("path", "").rsplit("/", 1)[-1]
        key = (
            "backup_progress_reading"
            if payload.get("stage") == "reading" else "backup_progress"
        )
        if not self._showing_progress:
            # 백업 시작 — 짧은 주기 갱신으로 전환되기 전에 한 번 당겨 온다.
            self._refresh_mgmt()
        self._showing_progress = True
        self._set_status(self.t[key].format(
            name=name, done=payload.get("done", 0),
            total=payload.get("total", 0),
        ))
