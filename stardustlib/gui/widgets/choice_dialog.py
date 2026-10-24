"""선택지 다이얼로그 — 메시지 + 동작 이름이 붙은 버튼 몇 개.

`messagebox.askyesnocancel`은 버튼 이름이 예/아니요/취소로 고정이라 "덮어쓰기 /
둘 다 유지 / 건너뛰기" 같은 선택을 정확히 보여줄 수 없다. 버튼마다 동작 이름을 붙이고,
고른 key를 콜백으로 돌려준다(창을 닫거나 Esc면 cancel_key).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk

from stardustlib.gui import theme


class ChoiceDialog:
    """모달 선택 창. choices는 (key, 라벨) 목록이며 첫 항목이 기본(Enter) 동작이다."""

    def __init__(
        self,
        app,
        title: str,
        message: str,
        choices: Sequence[tuple[str, str]],
        on_choose: Callable[[str], None],
        *,
        cancel_key: str,
    ) -> None:
        self.app = app
        self._on_choose = on_choose
        self._cancel_key = cancel_key
        self._done = False

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(title)
        win.resizable(False, False)
        app.make_modal(win, app.root)

        body = ttk.Frame(win, padding=(16, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, wraplength=theme.px(app.root, 380),
                  justify="left").pack(anchor="w")

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(14, 0))
        self.buttons: dict[str, ttk.Button] = {}
        # 오른쪽부터 쌓아 첫 선택지가 가장 오른쪽(주 동작 자리)에 온다.
        for index, (key, label) in enumerate(choices):
            btn = ttk.Button(btns, text=label,
                             command=lambda k=key: self.choose(k))
            btn.pack(side="right", padx=(6 if index else 0, 0))
            self.buttons[key] = btn
        first = choices[0][0]
        self.buttons[first].focus_set()

        win.bind("<Return>", lambda _e: self.choose(first))
        win.bind("<Escape>", lambda _e: self.choose(cancel_key))
        win.protocol("WM_DELETE_WINDOW", lambda: self.choose(cancel_key))

    def choose(self, key: str) -> None:
        """선택을 확정한다(한 번만). 창을 닫고 콜백을 부른다."""
        if self._done:
            return
        self._done = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self._on_choose(key)
