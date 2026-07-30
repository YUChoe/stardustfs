"""가상 폴더 선택 다이얼로그 — 이동·복사 대상을 트리에서 고른다.

경로를 손으로 입력하면 오타가 그대로 조회·이동 시도로 이어진다. 여기서는 실제
존재하는 폴더만 보여주고, 선택한 경로를 콜백으로 돌려준다.

폴더 목록은 워커 스레드에서 `actions.browse`로 읽는다(메인 스레드 비차단).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from stardustlib.gui import actions, theme


class VPathPicker:
    """가상 폴더 트리 모달. 선택하면 on_pick(vpath)를 부른다."""

    def __init__(self, app, start: str, on_pick: Callable[[str], None]) -> None:
        self.app = app
        self.t = app.t
        self.on_pick = on_pick
        self.vpath = start if start.startswith("/") else "/"
        self._build()
        self._load()

    def _build(self) -> None:
        win = tk.Toplevel(self.app.root)
        self.win = win
        win.title(self.t["pick_folder"])
        win.geometry("420x420")
        self.app.make_modal(win, self.app.root)

        self.path_label = ttk.Label(win, text=self.vpath, padding=(12, 10))
        self.path_label.pack(fill="x")

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12)
        self.list = tk.Listbox(frame, selectmode="browse")
        theme.style_listbox(self.list, dark=self.app.theme == "dark")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=sb.set)
        self.list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.list.bind("<Double-1>", lambda _e: self._enter())

        self.error_label = ttk.Label(
            win, text="", padding=(12, 0),
            foreground=theme.text_colour("danger",
                                         dark=self.app.theme == "dark"),
        )
        self.error_label.pack(fill="x")

        btns = ttk.Frame(win, padding=(12, 10))
        btns.pack(fill="x")
        ttk.Button(btns, text=self.t["form_cancel"],
                   command=self.win.destroy).pack(side="right")
        ttk.Button(btns, text=self.t["pick_here"],
                   command=self._choose).pack(side="right", padx=(0, 6))
        ttk.Button(btns, text=self.t["up"], command=self._up).pack(side="left")

    # --- 탐색 ---

    def _load(self) -> None:
        """현재 경로의 하위 폴더만 읽어 목록을 채운다."""
        self.path_label.config(text=self.vpath)
        self.list.delete(0, "end")
        cfg, vp = self.app.config_path, self.vpath

        def done(ok, payload):
            if not self.win.winfo_exists():
                return
            if not ok:
                self.error_label.config(text=str(payload))
                return
            self.error_label.config(text="")
            self._dirs = [r["name"] for r in payload["rows"]
                          if r.get("type") == "dir"]
            for name in self._dirs:
                self.list.insert("end", name)

        self._dirs: list[str] = []
        self.app.worker.submit(lambda: actions.browse(cfg, vp), done)

    def _enter(self) -> None:
        sel = self.list.curselection()
        if not sel:
            return
        self.vpath = self.vpath.rstrip("/") + "/" + self._dirs[sel[0]]
        self._load()

    def _up(self) -> None:
        if self.vpath == "/":
            return
        self.vpath = self.vpath.rstrip("/").rsplit("/", 1)[0] or "/"
        self._load()

    def _choose(self) -> None:
        """선택된 하위 폴더가 있으면 그 경로를, 없으면 현재 경로를 돌려준다."""
        sel = self.list.curselection()
        target = self.vpath
        if sel:
            target = self.vpath.rstrip("/") + "/" + self._dirs[sel[0]]
        self.win.destroy()
        self.on_pick(target)
