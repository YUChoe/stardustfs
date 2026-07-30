"""공용 폼 다이얼로그 — 여러 입력을 한 창에서 받는다.

`simpledialog.askstring`을 연달아 띄우면 중간에 취소했을 때 처음부터 다시 해야 하고,
실패 사유를 입력값과 함께 보여줄 자리가 없다. 이 다이얼로그는 필드 목록을 한 창에
세로로 배치하고, 제출이 실패하면 입력값을 유지한 채 창 안에 사유를 표시한다.

제출 처리는 호출자가 맡는다 — `submit(values, dialog)`에서 워커 작업을 시작하고,
결과에 따라 `dialog.error(...)` 또는 `dialog.close()`를 부른다. 네트워크 호출을
메인 스레드에서 하지 않기 위함이다.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from tkinter import filedialog, ttk

from stardustlib.gui import theme

# 필드 종류.
TEXT = "text"
PASSWORD = "password"
DIR = "dir"          # 폴더 선택 버튼이 붙은 입력
SAVE = "save"        # 저장 위치 선택 버튼이 붙은 입력
BOOL = "bool"
INT = "int"


@dataclass(frozen=True)
class Field:
    """폼의 입력 한 줄."""

    key: str
    label: str
    kind: str = TEXT
    initial: str = ""
    required: bool = False
    # DIR/SAVE 선택 다이얼로그 제목, INT 최솟값.
    pick_title: str = ""
    minimum: int = 0


class FormDialog:
    """필드 목록을 한 창에 세로로 배치하는 모달 폼."""

    def __init__(
        self,
        app,
        title: str,
        fields: Sequence[Field],
        submit: Callable[[dict, "FormDialog"], None],
        *,
        ok_label: str | None = None,
    ) -> None:
        self.app = app
        self.t = app.t
        self.fields = list(fields)
        self._submit = submit
        self._vars: dict[str, tk.Variable] = {}
        self._build(title, ok_label or self.t["form_ok"])

    # --- 구성 ---

    def _build(self, title: str, ok_label: str) -> None:
        win = tk.Toplevel(self.app.root)
        self.win = win
        win.title(title)
        win.resizable(False, False)
        self.app.make_modal(win, self.app.root)

        body = ttk.Frame(win, padding=(14, 12))
        body.pack(fill="both", expand=True)

        first: tk.Widget | None = None
        for field in self.fields:
            widget = self._add_field(body, field)
            if first is None and field.kind != BOOL:
                first = widget

        # 오류 표시 자리. 실패해도 창을 닫지 않고 여기에 사유를 남긴다.
        self.error_label = ttk.Label(
            body, text="", wraplength=380, justify="left",
            foreground=theme.text_colour(
                "danger", dark=getattr(self.app, "theme", "dark") == "dark"),
        )
        self.error_label.pack(fill="x", pady=(6, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        self.cancel_btn = ttk.Button(
            btns, text=self.t["form_cancel"], command=self.close
        )
        self.cancel_btn.pack(side="right")
        self.ok_btn = ttk.Button(btns, text=ok_label, command=self._on_ok)
        self.ok_btn.pack(side="right", padx=(0, 6))

        win.bind("<Return>", lambda _e: self._on_ok())
        win.bind("<Escape>", lambda _e: self.close())
        win.protocol("WM_DELETE_WINDOW", self.close)
        if first is not None:
            first.focus_set()

    def _add_field(self, parent, field: Field) -> tk.Widget:
        """필드 한 줄(라벨 + 입력)을 만들고 입력 위젯을 반환한다."""
        if field.kind == BOOL:
            var = tk.BooleanVar(value=bool(field.initial))
            self._vars[field.key] = var
            box = ttk.Checkbutton(parent, text=field.label, variable=var)
            box.pack(anchor="w", pady=(6, 0))
            return box

        ttk.Label(parent, text=field.label).pack(anchor="w", pady=(6, 2))
        row = ttk.Frame(parent)
        row.pack(fill="x")
        var = tk.StringVar(value=field.initial)
        self._vars[field.key] = var
        entry = ttk.Entry(
            row, textvariable=var, width=46,
            show="*" if field.kind == PASSWORD else "",
        )
        entry.pack(side="left", fill="x", expand=True)
        if field.kind in (DIR, SAVE):
            ttk.Button(
                row, text=self.t["form_browse"], width=6,
                command=lambda f=field, v=var: self._pick(f, v),
            ).pack(side="left", padx=(6, 0))
        return entry

    def _pick(self, field: Field, var: tk.StringVar) -> None:
        title = field.pick_title or field.label
        if field.kind == DIR:
            path = filedialog.askdirectory(parent=self.win, title=title)
        else:
            path = filedialog.asksaveasfilename(parent=self.win, title=title)
        if path:
            var.set(path)

    # --- 제출 ---

    def values(self) -> dict:
        """필드 값을 종류에 맞는 타입으로 반환한다(bool, int, str)."""
        out: dict = {}
        for field in self.fields:
            raw = self._vars[field.key].get()
            if field.kind == BOOL:
                out[field.key] = bool(raw)
            elif field.kind == INT:
                out[field.key] = int(str(raw).strip() or 0)
            else:
                out[field.key] = str(raw).strip()
        return out

    def _validate(self) -> str | None:
        """필수 입력과 정수 형식을 확인한다. 문제가 있으면 사유를 반환한다."""
        for field in self.fields:
            raw = str(self._vars[field.key].get()).strip()
            if field.required and not raw:
                return self.t["form_required"].format(label=field.label)
            if field.kind == INT:
                if not raw.isdigit():
                    return self.t["form_int"].format(label=field.label)
                if int(raw) < field.minimum:
                    return self.t["form_min"].format(
                        label=field.label, minimum=field.minimum)
        return None

    def _on_ok(self) -> None:
        if "disabled" in self.ok_btn.state():
            return  # 이미 처리 중
        problem = self._validate()
        if problem:
            self.error(problem)
            return
        self.error("")
        self.busy(True)
        self._submit(self.values(), self)

    # --- 호출자가 쓰는 제어 ---

    def busy(self, on: bool) -> None:
        """처리 중에는 확인 버튼을 잠근다(중복 제출 방지)."""
        state = ["disabled"] if on else ["!disabled"]
        try:
            self.ok_btn.state(state)
        except tk.TclError:  # 이미 닫힘
            pass

    def error(self, message: str) -> None:
        """창 안에 사유를 표시한다(입력값은 그대로 유지)."""
        self.busy(False)
        try:
            self.error_label.config(text=message)
        except tk.TclError:
            pass

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass
