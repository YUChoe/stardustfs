"""지연 표시 툴팁.

문구는 표시 시점에 콜백으로 만든다 — 언어를 바꾸거나 버튼이 비활성으로 바뀌어도
따라가야 하기 때문이다. 빈 문자열을 반환하면 표시하지 않는다.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from stardustlib.gui import theme

_DELAY_MS = 600


class _Tip:
    """위젯 하나에 붙는 툴팁 상태."""

    def __init__(self, widget: tk.Widget, text_fn: Callable[[], str],
                 delay_ms: int) -> None:
        self.widget = widget
        self.text_fn = text_fn
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        try:
            text = self.text_fn()
        except Exception:  # noqa: BLE001 — 표시용 문구 생성 실패는 무시
            return
        if not text or self._window is not None:
            return
        try:
            # 커서 기준으로 띄운다 — 위젯 기준으로 두면 Treeview처럼 큰 위젯에서
            # 툴팁이 위젯 아래(다른 영역 위)에 뜬다.
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 20
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)  # 제목표시줄 없는 떠 있는 상자
            win.wm_geometry(f"+{x}+{y}")
            tk.Label(
                win, text=text, justify="left", wraplength=320,
                background=theme.PALETTE["surface"],
                foreground=theme.PALETTE["fg_default"],
                relief="solid", borderwidth=1, padx=8, pady=4,
            ).pack()
            self._window = win
        except tk.TclError:  # 위젯이 이미 사라짐
            self._window = None

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


def attach(widget: tk.Widget, text_fn: Callable[[], str], *,
           delay_ms: int = _DELAY_MS) -> None:
    """위젯에 툴팁을 붙인다. text_fn은 표시 직전에 호출된다."""
    _Tip(widget, text_fn, delay_ms)
