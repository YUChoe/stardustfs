"""브레드크럼 경로 바 — 경로 조각을 눌러 이동한다.

텍스트 입력으로 경로를 받으면 오타가 그대로 조회 시도로 이어지고, 지금 어디에 있는지
읽기도 어렵다. 여기서는 현재 경로를 클릭 가능한 세그먼트로 보여준다.

세그먼트가 바 폭을 넘으면 앞쪽을 '…' 하나로 접고, 클릭하면 접힌 경로를 메뉴로
펼친다(뒤쪽 = 현재 위치에 가까운 쪽을 남긴다).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from stardustlib.gui import theme

# 세그먼트 사이 구분자.
_SEP = "›"


def split_path(vpath: str) -> list[tuple[str, str]]:
    """가상 경로를 (표시 라벨, 이동 대상 경로) 목록으로 나눈다(루트 포함)."""
    parts = [p for p in (vpath or "/").strip("/").split("/") if p]
    out = [("/", "/")]
    acc = ""
    for part in parts:
        acc = acc + "/" + part
        out.append((part, acc))
    return out


class Breadcrumb:
    """클릭 가능한 경로 세그먼트 바."""

    def __init__(self, parent, on_navigate: Callable[[str], None], *,
                 dark: bool = True) -> None:
        self.frame = ttk.Frame(parent)
        self.on_navigate = on_navigate
        self.dark = dark
        self._vpath = "/"
        self._widgets: list[tk.Widget] = []
        self._hidden: list[tuple[str, str]] = []
        # 폭이 바뀌면 몇 개를 접을지 다시 계산한다.
        self.frame.bind("<Configure>", self._on_resize)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def set_path(self, vpath: str) -> None:
        self._vpath = vpath or "/"
        self._render()

    # --- 그리기 ---

    def _clear(self) -> None:
        for w in self._widgets:
            w.destroy()
        self._widgets.clear()

    def _render(self, keep: int | None = None) -> None:
        """세그먼트를 그린다. keep이 주어지면 뒤쪽 keep개만 표시하고 앞은 접는다."""
        self._clear()
        segments = split_path(self._vpath)
        self._hidden = []
        if keep is not None and keep < len(segments) - 1:
            # 루트는 항상 남기고 그 다음부터 접는다.
            cut = len(segments) - keep
            self._hidden = segments[1:cut]
            segments = segments[:1] + segments[cut:]
            if self._hidden:
                self._add_ellipsis(after_root=True)

        for index, (label, target) in enumerate(segments):
            if index and not (self._hidden and index == 1):
                self._add_separator()
            self._add_segment(label, target, last=index == len(segments) - 1)

    def _add_segment(self, label: str, target: str, *, last: bool) -> None:
        btn = ttk.Button(
            self.frame, text=label, style="Toolbutton", takefocus=False,
            command=lambda t=target: self.on_navigate(t),
        )
        btn.pack(side="left")
        if last:
            # 현재 위치는 눌러도 갈 곳이 없다.
            btn.state(["disabled"])
        self._widgets.append(btn)

    def _add_separator(self) -> None:
        lab = ttk.Label(self.frame, text=_SEP,
                        foreground=theme.text_colour("fg_faint",
                                                     dark=self.dark))
        lab.pack(side="left", padx=1)
        self._widgets.append(lab)

    def _add_ellipsis(self, *, after_root: bool) -> None:
        """접힌 앞쪽 경로를 여는 '…' 버튼."""
        self._add_separator()
        btn = ttk.Button(self.frame, text="…", style="Toolbutton", width=3,
                         takefocus=False, command=self._show_hidden)
        btn.pack(side="left")
        self._widgets.append(btn)
        self._ellipsis_btn = btn

    def _show_hidden(self) -> None:
        menu = tk.Menu(self.frame, tearoff=0)
        theme.style_menu(menu, dark=self.dark)
        for label, target in self._hidden:
            menu.add_command(label=label,
                             command=lambda t=target: self.on_navigate(t))
        btn = self._ellipsis_btn
        try:
            menu.tk_popup(btn.winfo_rootx(),
                          btn.winfo_rooty() + btn.winfo_height())
        finally:
            menu.grab_release()

    # --- 폭 맞춤 ---

    def _on_resize(self, _e=None) -> None:
        """세그먼트가 바 폭을 넘으면 앞쪽부터 접는다."""
        self.frame.update_idletasks()
        available = self.frame.winfo_width()
        if available <= 1:
            return
        needed = sum(w.winfo_reqwidth() for w in self._widgets)
        if needed <= available:
            return
        segments = split_path(self._vpath)
        # 뒤쪽부터 하나씩 줄여 가며 들어가는 개수를 찾는다(최소 1개는 남긴다).
        for keep in range(len(segments) - 1, 0, -1):
            self._render(keep=keep)
            self.frame.update_idletasks()
            if sum(w.winfo_reqwidth() for w in self._widgets) <= available:
                return
