"""액션 툴바와 컨텍스트 메뉴 — action_defs 정의로 만들고 선택 상태를 따른다.

툴바 버튼이 창 폭보다 많으면(영어 UI의 최소 폭 760px 등) 마지막 그룹(백업·복제본
보충·복원)을 '더보기' 메뉴 하나로 접는다. pack은 넘친 위젯을 말없이 잘라 버려 버튼이
반쯤 보이거나 사라진다. 접는 판단은 현재 배치가 아니라 모든 버튼의 요구 폭으로 하므로
접었다 폈다를 되풀이하지 않는다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from stardustlib.gui import action_defs, theme
from stardustlib.gui.widgets import tooltip
from stardustlib.gui.window_theme import sv_ttk

# 툴바 안쪽 좌우 여백 합(padding=(8, 6)) · 버튼 간격 · 그룹 구분선 좌우 여백(px).
_TB_PADX = 16
_BTN_GAP = 6
_SEP_PADX = 10


class ToolbarMixin:
    """액션 툴바·컨텍스트 메뉴 구성과 활성 상태 동기화."""

    def _build_action_toolbar(self, parent) -> None:
        """액션 툴바 — action_defs.TOOLBAR_GROUPS 정의대로 만든다.

        파일 목록과 같은 부모(분할 pane)에 목록보다 먼저 pack한다 — 좁은 창·높은
        DPI 배율에서도 업로드·백업 버튼이 잘리지 않도록 고정 높이 영역을 먼저
        확보하고, 분할선을 옮겨도 목록 바로 아래에 붙어 있게 한다.
        """
        tb = ttk.Frame(parent, padding=(8, 6))
        tb.pack(fill="x", side="bottom")
        self._toolbar = tb
        self._action_buttons: dict[str, ttk.Button] = {}
        # 그룹별 (위젯, 좌 여백, 우 여백). 폭 계산과 접기에 쓴다.
        self._tb_groups: list[list[tuple[tk.Widget, int, int]]] = []

        for gi, group in enumerate(action_defs.TOOLBAR_GROUPS):
            widgets: list[tuple[tk.Widget, int, int]] = []
            if gi:
                sep = ttk.Separator(tb, orient="vertical")
                sep.pack(side="left", fill="y", padx=_SEP_PADX)
                widgets.append((sep, _SEP_PADX, _SEP_PADX))
            for ai, action in enumerate(group):
                style = ("Cta.TButton"
                         if (action.accent and sv_ttk is not None) else "TButton")
                btn = ttk.Button(
                    tb, text=self.t[action.key], style=style,
                    command=getattr(self, action.method),
                )
                gap = _BTN_GAP if ai else 0
                btn.pack(side="left", padx=(gap, 0))
                widgets.append((btn, gap, 0))
                self._action_buttons[action.key] = btn
                tooltip.attach(btn, lambda a=action, b=btn: action_defs.tooltip(
                    a, self.t, enabled="disabled" not in b.state()))
            self._tb_groups.append(widgets)
        self._build_overflow(tb)

        # 업로드 큐 정리 버튼 — 해당 항목이 있을 때만 오른쪽에 보인다(우클릭 메뉴에만
        # 두면 찾지 못한다).
        self._upload_ctl_buttons: dict[str, ttk.Button] = {}
        for action in action_defs.UPLOAD_CONTROLS:
            btn = ttk.Button(tb, text=self.t[action.key],
                             command=getattr(self, action.method))
            tooltip.attach(btn, lambda a=action: action_defs.tooltip(
                a, self.t, enabled=True))
            self._upload_ctl_buttons[action.key] = btn
        # 업로드 버튼은 잘림 회귀 테스트에서 참조하므로 이름을 남긴다.
        self.upload_btn = self._action_buttons["upload"]
        tb.bind("<Configure>", lambda _e: self._fit_toolbar(), add="+")
        self._sync_action_states()

    # --- 넘침 처리 ---

    def _build_overflow(self, tb) -> None:
        """마지막 그룹을 대신할 '더보기' 메뉴 버튼(접혔을 때만 보인다)."""
        more = ttk.Menubutton(tb, text=self.t["toolbar_more"], direction="above")
        menu = tk.Menu(more, tearoff=0, postcommand=self._sync_overflow_menu)
        theme.style_menu(menu, dark=self.theme == "dark")
        for action in action_defs.TOOLBAR_GROUPS[-1]:
            menu.add_command(label=self.t[action.key],
                             command=getattr(self, action.method))
        more["menu"] = menu
        self._tb_more, self._tb_more_menu = more, menu
        self._tb_collapsed = False

    def _toolbar_needed(self) -> int:
        """모든 버튼을 펼쳤을 때의 폭(보이는 업로드 정리 버튼 포함)."""
        width = _TB_PADX
        for group in self._tb_groups:
            for widget, left, right in group:
                width += widget.winfo_reqwidth() + left + right
        for btn in self._upload_ctl_buttons.values():
            if btn.winfo_manager():
                width += btn.winfo_reqwidth() + _BTN_GAP
        return width

    def _fit_toolbar(self) -> None:
        """폭이 모자라면 마지막 그룹을 '더보기'로 접고, 넉넉해지면 다시 편다."""
        tb = getattr(self, "_toolbar", None)
        if tb is None:
            return
        try:
            available = tb.winfo_width()
            if available <= 1:
                return  # 아직 배치 전
            collapse = self._toolbar_needed() > available
            if collapse == self._tb_collapsed:
                return
            self._tb_collapsed = collapse
            buttons = [w for w in self._tb_groups[-1]
                       if not isinstance(w[0], ttk.Separator)]
            for widget, _l, _r in buttons:
                widget.pack_forget()
            self._tb_more.pack_forget()
            if collapse:
                self._tb_more.pack(side="left")
            else:
                for widget, left, right in buttons:
                    widget.pack(side="left", padx=(left, right))
        except tk.TclError:  # 재구성 중 파괴된 위젯
            pass

    def _sync_overflow_menu(self) -> None:
        rows = self._selected_rows() if self._tree_alive() else []
        has_config = bool(self.config_path)
        for index, action in enumerate(action_defs.TOOLBAR_GROUPS[-1]):
            enabled = action_defs.is_enabled(action, rows, has_config=has_config)
            self._tb_more_menu.entryconfigure(
                index, state="normal" if enabled else "disabled")

    def _sync_upload_controls(self) -> None:
        """업로드 취소·정리 버튼을 큐 상태에 맞춰 보이거나 감춘다."""
        buttons = getattr(self, "_upload_ctl_buttons", None)
        if not buttons:
            return
        queued, finished = self._upload_counts()
        wanted = {"upload_cancel": queued > 0, "upload_clear": finished > 0}
        # 오른쪽부터 쌓이므로 순서를 고정하려면 감춘 뒤 정해진 순서로 다시 놓는다.
        for btn in buttons.values():
            btn.pack_forget()
        for key in ("upload_clear", "upload_cancel"):
            if wanted[key]:
                buttons[key].pack(side="right", padx=(_BTN_GAP, 0))
        self._fit_toolbar()

    def _sync_action_states(self, _e=None) -> None:
        """선택 상태에 맞춰 툴바 버튼 활성/비활성을 맞춘다.

        선택 없이 누른 뒤 안내 창으로 돌려보내는 대신, 누를 수 없음을 미리 보여준다.
        """
        buttons = getattr(self, "_action_buttons", None)
        if not buttons:
            return
        # 툴바는 목록보다 먼저 만들어지므로(하단 공간 선점) 첫 호출에는 tree가 없다.
        # 언어 전환 재구성 중에는 옛 tree 객체가 남아 있으나 이미 파괴된 상태다.
        rows = self._selected_rows() if self._tree_alive() else []
        has_config = bool(self.config_path)
        for group in action_defs.TOOLBAR_GROUPS:
            for action in group:
                btn = buttons.get(action.key)
                if btn is not None:
                    self._enable(btn, action_defs.is_enabled(
                        action, rows, has_config=has_config))

    # --- 컨텍스트 메뉴 ---

    def _build_context_menu(self) -> None:
        """파일 목록의 오른쪽 클릭 팝업 메뉴 — 툴바와 같은 정의를 쓴다."""
        menu = tk.Menu(self.tree, tearoff=0)
        # 팝업 시 항목 활성 상태를 갱신하려면 인덱스를 알아야 한다(구분선 포함 순번).
        self._ctx_entries: list[tuple[int, action_defs.Action]] = []
        for index, action in enumerate(action_defs.CONTEXT_MENU):
            if action is None:
                menu.add_separator()
                continue
            menu.add_command(label=self.t[action.key],
                             command=getattr(self, action.method))
            self._ctx_entries.append((index, action))
        theme.style_menu(menu, dark=self.theme == "dark")
        self._ctx_menu = menu
        # Windows/X11은 Button-3, macOS는 Button-2
        self.tree.bind("<Button-3>", self._on_context_menu)
        self.tree.bind("<Button-2>", self._on_context_menu)

    def _on_context_menu(self, event) -> None:
        """클릭 위치의 행을 선택(이미 다중 선택된 행이면 유지)하고 메뉴를 띄운다."""
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        rows = self._selected_rows()
        has_config = bool(self.config_path)
        queued, finished = self._upload_counts()
        for index, action in self._ctx_entries:
            enabled = action_defs.is_enabled(action, rows, has_config=has_config)
            if action.key == "upload_cancel":
                enabled = queued > 0
            elif action.key == "upload_clear":
                enabled = finished > 0
            self._ctx_menu.entryconfigure(
                index, state="normal" if enabled else "disabled")
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()
