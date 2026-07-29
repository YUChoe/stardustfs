"""파일 업로드 다이얼로그.

업로드 버튼이 여는 다이얼로그. 파일(들) 선택, 진행바, 올리기/취소 버튼, 상태
로그를 제공한다. 전송은 앱의 워커 스레드에서 파일 단위로 순차 수행하고(put_file),
진행바는 완료 파일 수로 채운다. 한 파일이 끝나면 다음 파일을 큐잉하므로 메인
스레드를 막지 않고, 파일 사이에서 취소 플래그를 확인해 남은 파일을 건너뛴다.

드롭으로 연 경우 initial_files로 선택을 미리 채운다(드롭 연동은 후속).
"""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Iterable
from tkinter import filedialog, ttk

from stardustlib.gui import actions, theme
from stardustlib.gui.format import human_bytes


class UploadDialog:
    """업로드 다이얼로그(파일 선택 + 진행바 + 상태 로그)."""

    def __init__(self, app, initial_files: Iterable[str] | None = None) -> None:
        self.app = app
        self.t = app.t
        # 업로드 대상은 다이얼로그를 연 시점의 현재 경로로 고정한다(탐색 중 변경 무관).
        self.dest = app.vpath
        self.files: list[str] = []
        self._uploading = False
        self._cancel = False
        self._ok = 0
        self._fail = 0
        self._build()
        if initial_files:
            self.add_paths(initial_files)

    # --- UI 구성 ---

    def _build(self) -> None:
        t = self.t
        win = tk.Toplevel(self.app.root)
        self.win = win
        win.title(t["upload_dlg_title"])
        win.geometry("520x480")
        # 모달: 닫기 전까지 메인 창 입력 차단.
        self.app.make_modal(win, self.app.root)

        ttk.Label(win, text=t["upload_dlg_to"].format(path=self.dest)).pack(
            anchor="w", padx=10, pady=(10, 4)
        )

        list_frame = ttk.Frame(win)
        list_frame.pack(fill="both", expand=False, padx=10)
        self.listbox = tk.Listbox(list_frame, height=7, selectmode="extended")
        theme.style_listbox(self.listbox)
        sb = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.listbox.yview
        )
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        sel = ttk.Frame(win)
        sel.pack(fill="x", padx=10, pady=6)
        self.add_btn = ttk.Button(sel, text=t["upload_add"], command=self._pick)
        self.add_btn.pack(side="left")
        self.remove_btn = ttk.Button(
            sel, text=t["upload_remove"], command=self._remove_selected
        )
        self.remove_btn.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(win, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(4, 6))

        log_frame = ttk.Frame(win)
        log_frame.pack(fill="both", expand=True, padx=10)
        self.log = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        theme.style_text(self.log)
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=10)
        self.cancel_btn = ttk.Button(
            btns, text=t["upload_cancel"], command=self._cancel_or_close
        )
        self.cancel_btn.pack(side="right")
        self.start_btn = ttk.Button(
            btns, text=t["upload_start"], command=self._start
        )
        self.start_btn.pack(side="right", padx=6)

        win.protocol("WM_DELETE_WINDOW", self._cancel_or_close)

    # --- 파일 선택 ---

    def _pick(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.win, title=self.t["upload_pick"]
        )
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths: Iterable[str]) -> None:
        """파일 경로들을 목록에 추가한다(존재하는 파일·중복 제외)."""
        for p in paths:
            ap = os.path.abspath(p)
            if os.path.isfile(ap) and ap not in self.files:
                self.files.append(ap)
        self._render_files()

    def _remove_selected(self) -> None:
        if self._uploading:
            return
        for i in sorted(self.listbox.curselection(), reverse=True):
            del self.files[i]
        self._render_files()

    def _render_files(self) -> None:
        self.listbox.delete(0, "end")
        for p in self.files:
            self.listbox.insert("end", os.path.basename(p))

    # --- 상태 로그 ---

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_controls(self, uploading: bool) -> None:
        state = ["disabled"] if uploading else ["!disabled"]
        for btn in (self.add_btn, self.remove_btn, self.start_btn):
            btn.state(state)
        # 취소 버튼은 업로드 중에도 활성(남은 파일 건너뛰기 + 닫기 겸용).

    # --- 업로드 ---

    def _start(self) -> None:
        if self._uploading:
            return
        if not self.files:
            self._log(self.t["upload_none"])
            return
        if not self.app.config_path:
            self._log(self.t["need_config"])
            return
        self._uploading = True
        self._cancel = False
        # 업로드 대상 스냅샷. 성공한 파일은 self.files에서 제거하므로, 진행률은
        # 시작 시점의 스냅샷(_pending/_total/_pos) 기준으로 별도 추적한다.
        self._pending = list(self.files)
        self._total = len(self._pending)
        self._pos = 0
        self._ok = 0
        self._fail = 0
        self._skip = 0
        self.progress.configure(maximum=self._total, value=0)
        self._set_controls(uploading=True)
        self._upload_next()

    def _upload_next(self) -> None:
        if self._cancel or self._pos >= self._total:
            self._finish()
            return
        local = self._pending[self._pos]
        name = os.path.basename(local)
        remote = self.dest.rstrip("/") + "/" + name
        cfg = self.app.config_path
        self._log(self.t["upload_log_start"].format(name=name))

        def done(ok, payload):
            if ok:
                self._ok += 1
                self._log(
                    self.t["upload_log_done"].format(
                        name=name, size=human_bytes(payload)
                    )
                )
                # 성공한 파일은 목록에서 제거(실패는 남겨 재시도 가능).
                if local in self.files:
                    self.files.remove(local)
                    self._render_files()
            elif isinstance(payload, actions.RemotePathExists):
                # 같은 경로에 이미 존재 — 덮어쓰지 않고 건너뜀(목록에 남겨 둠).
                self._skip += 1
                self._log(self.t["upload_log_exists"].format(name=name))
            else:
                self._fail += 1
                self._log(
                    self.t["upload_log_fail"].format(name=name, msg=payload)
                )
            self._pos += 1
            self.progress.configure(value=self._pos)
            self._upload_next()

        self.app.worker.submit(
            lambda: actions.put_file(cfg, local, remote), done
        )

    def _finish(self) -> None:
        self._uploading = False
        self._set_controls(uploading=False)
        if self._cancel:
            self._log(
                self.t["upload_log_cancel"].format(n=self._total - self._pos)
            )
        self._log(
            self.t["upload_log_all"].format(
                ok=self._ok, fail=self._fail, skip=self._skip
            )
        )
        # 업로드된 파일이 있으면 메인 목록을 갱신한다.
        if self._ok:
            self.app._after_write()
        # 취소 버튼을 '닫기'로 전환.
        self.cancel_btn.configure(text=self.t["upload_close"])

    def _cancel_or_close(self) -> None:
        if self._uploading:
            # 진행 중: 남은 파일만 건너뛴다(현재 파일은 완료까지 진행).
            self._cancel = True
            return
        self.win.destroy()
