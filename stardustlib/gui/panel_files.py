"""파일 목록 패널 — 경로 바, 목록 Treeview, 컨텍스트 메뉴, 탐색.

StardustApp에 믹스인으로 결합한다. 파일에 대한 실제 동작(업로드/삭제/백업 등)은
file_ops.py가 담당하고, 이 모듈은 무엇을 보여주고 무엇이 선택됐는지를 다룬다.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from stardustlib.gui import action_defs, actions, theme
from stardustlib.gui.format import human_bytes

try:  # Accent 버튼 스타일 제공(미설치 시 기본 버튼).
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None

logger = logging.getLogger(__name__)


class FilesPanelMixin:
    """파일 목록 위젯 구성 + 탐색 + 선택."""

    # --- 위젯 구성 ---

    def _build_path_bar(self, parent) -> None:
        """경로 입력·이동 + 로그인/로그아웃 바.

        설정 진입은 '파일' 메뉴로 옮겼고, 설정 파일명은 창 제목에 표시한다.
        """
        t = self.t
        pframe = ttk.Frame(parent, padding=(10, 8))
        pframe.pack(fill="x")
        self.login_btn = ttk.Button(pframe, text=t["login"], command=self._login)
        self.login_btn.pack(side="right")
        self.logout_btn = ttk.Button(pframe, text=t["logout"], command=self._logout)
        self.logout_btn.pack(side="right", padx=(0, 6))
        ttk.Button(pframe, text=t["up"], command=self._up).pack(side="left")
        self.path_var = tk.StringVar(value=self.vpath)
        entry = ttk.Entry(pframe, textvariable=self.path_var)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda _e: self._go())
        ttk.Button(pframe, text=t["go"], command=self._go).pack(side="left")
        ttk.Button(pframe, text=t["refresh"], command=self.refresh).pack(
            side="left", padx=(6, 10))

    def _build_file_tree(self, parent) -> None:
        """파일 목록 Treeview + 세로 스크롤바."""
        t = self.t
        cols = ("name", "size", "backup")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings",
                                 selectmode="extended")
        for c, head, w, anchor in (
            ("name", t["col_name"], 440, "w"),
            ("size", t["col_size"], 110, "e"),
            ("backup", t["col_backup"], 140, "w"),
        ):
            # 헤딩은 Tk 기본(가운데)을 유지한다. 데이터 정렬에 맞추면 오른쪽 정렬한
            # '크기'와 왼쪽 정렬한 '백업'이 경계에서 붙어 한 단어로 읽힌다
            # (다크 테마에서 컬럼 구분선이 거의 보이지 않는다).
            self.tree.heading(c, text=head)
            self.tree.column(c, width=w, anchor=anchor)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0))
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewSelect>>", self._sync_action_states)
        self._build_context_menu()
        self._sync_action_states()

    def _build_action_toolbar(self, parent) -> None:
        """액션 툴바 — action_defs.TOOLBAR_GROUPS 정의대로 만든다.

        파일 목록보다 먼저 호출한다 — 좁은 창·높은 DPI 배율에서도 업로드·백업
        버튼이 잘리지 않도록 고정 높이 영역을 먼저 확보한다.
        """
        tb = ttk.Frame(parent, padding=(8, 6))
        tb.pack(fill="x", side="bottom")
        self._action_buttons: dict[str, ttk.Button] = {}

        for gi, group in enumerate(action_defs.TOOLBAR_GROUPS):
            if gi:
                ttk.Separator(tb, orient="vertical").pack(
                    side="left", fill="y", padx=10)
            for ai, action in enumerate(group):
                style = ("Accent.TButton"
                         if (action.accent and sv_ttk is not None) else "TButton")
                btn = ttk.Button(
                    tb, text=self.t[action.key], style=style,
                    command=getattr(self, action.method),
                )
                btn.pack(side="left", padx=(0 if not ai else 6, 0))
                self._action_buttons[action.key] = btn
        # 업로드 버튼은 잘림 회귀 테스트에서 참조하므로 이름을 남긴다.
        self.upload_btn = self._action_buttons["upload"]
        self._sync_action_states()

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
        if not self.tree.selection():
            return
        rows = self._selected_rows()
        has_config = bool(self.config_path)
        for index, action in self._ctx_entries:
            enabled = action_defs.is_enabled(action, rows, has_config=has_config)
            self._ctx_menu.entryconfigure(
                index, state="normal" if enabled else "disabled")
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    def _sync_action_states(self, _e=None) -> None:
        """선택 상태에 맞춰 툴바 버튼 활성/비활성을 맞춘다.

        선택 없이 누른 뒤 안내 창으로 돌려보내는 대신, 누를 수 없음을 미리 보여준다.
        """
        buttons = getattr(self, "_action_buttons", None)
        if not buttons:
            return
        # 툴바는 목록보다 먼저 만들어지므로(하단 공간 선점) 첫 호출에는 tree가 없다.
        rows = self._selected_rows() if getattr(self, "tree", None) else []
        has_config = bool(self.config_path)
        for group in action_defs.TOOLBAR_GROUPS:
            for action in group:
                btn = buttons.get(action.key)
                if btn is not None:
                    self._enable(btn, action_defs.is_enabled(
                        action, rows, has_config=has_config))

    # --- 탐색 ---

    def refresh(self, counts: bool = True) -> None:
        self.vpath = self.path_var.get() or "/"
        if not self.config_path:
            self._populate([])
            self._set_status(self.t["select_config_hint"])
            return
        # 로그인하지 않았으면 파일 목록을 보여주지 않는다(코어 초기화도 하지 않음).
        if not self._logged_in():
            self._populate([])
            self._set_status(self.t["login_required"])
            return
        cfg = self.config_path
        vp = self.vpath
        self._submit(lambda: actions.browse(cfg, vp),
                     lambda d: self._show_browse(d, counts),
                     self.t["busy_browse"])

    def _after_write(self) -> None:
        """쓰기 작업 후: 캐시 세션 무효화(용량 갱신) + 새로고침."""
        cfg = self.config_path
        if cfg:
            self.worker.submit(lambda: actions.invalidate(cfg), lambda *_a: None)
        self.refresh()
        self._refresh_mgmt()  # 스토리지 사용량 변동을 하단 패널에도 반영

    def _on_double(self, _e) -> None:
        row = self._selected()
        if row and row["type"] == "dir":
            self.path_var.set(self._join(row["name"]))
            self.refresh()

    def _up(self) -> None:
        parent = self.vpath.rstrip("/").rsplit("/", 1)[0] or "/"
        self.path_var.set(parent)
        self.refresh()

    def _go(self) -> None:
        self.refresh()

    # --- 목록 채우기 ---

    def _backup_label(self, status: str) -> str:
        """로컬 replication_status를 표시 라벨로 변환한다."""
        if status == "replicated":
            return self.t["bk_done"]
        if status == "pending":
            return self.t["bk_pending"]
        return self.t["bk_none"]

    def _populate(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        for r in rows:
            is_file = r["type"] == "file"
            size = human_bytes(r["size"]) if is_file else ""
            backup = self._backup_label(r.get("backup", "")) if is_file else ""
            icon = "📄" if is_file else "📁"
            iid = self.tree.insert(
                "", "end",
                values=(f"{icon}  {r['name']}", size, backup),
            )
            self._rows[iid] = r
        # 목록을 다시 채우면 선택이 사라지므로 버튼 상태도 함께 되돌린다.
        self._sync_action_states()

    def _show_browse(self, d: dict, counts: bool = True) -> None:
        self._populate(d["rows"])
        # 스토리지 상태: 소스 수 + 사용/총 용량
        self.storage_label.config(text=self.t["storage_status"].format(
            sources=d.get("sources", 0), used=human_bytes(d["used"]),
            total=human_bytes(d["total"]),
        ))
        bs = d.get("backup_summary")
        if bs:
            self.backup_status.config(text=self.t["backup_summary"].format(
                replicated=bs["replicated"], pending=bs["pending"],
                none=bs["none"],
            ))
        # 방금 본 메타데이터 시점을 기록(자동 새로고침이 즉시 재발동하지 않도록).
        self._mark_meta_seen()
        if not counts:
            return
        # 디바이스 온라인/전체는 하단 패널 갱신(_populate_mgmt)에서 같은 응답으로
        # 계산한다 — 파일 목록을 다시 읽을 때마다 /devices를 또 부르지 않는다.
        # 로컬 상태가 pending/replicated인 파일만 실제 복제본 수를 백그라운드 조회해
        # 병기한다. none(미백업) 뿐이면 서버 조회를 생략한다(불필요한 초기화/호출 방지).
        # (조용한 보강 — worker 콜백은 (ok, payload) 시그니처, 실패 시 상태만 유지)
        names = [
            r["name"] for r in d["rows"]
            if r["type"] == "file" and r.get("backup") in ("pending", "replicated")
        ]
        if names and self._logged_in():
            cfg, vp = self.config_path, self.vpath
            self.worker.submit(
                lambda: actions.replica_counts(cfg, vp, names),
                lambda ok, counts: self._apply_counts(vp, counts) if ok else None,
            )

    def _apply_counts(self, vp: str, counts: dict) -> None:
        """replica_counts 결과를 백업 컬럼에 '상태 (online/min)'로 병기한다."""
        if vp != self.vpath or not counts:
            return  # 폴더가 바뀌었거나 조회 결과 없음
        for iid, row in self._rows.items():
            if row["type"] != "file":
                continue
            info = counts.get(row["name"])
            if not info:
                continue
            label = self._backup_label(row.get("backup", ""))
            text = f"{label} ({info['online']}/{info['min']})"
            self.tree.set(iid, "backup", text)

    # --- 선택 ---

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _selected_rows(self) -> list[dict]:
        """다중 선택된 행 목록(없으면 빈 리스트)."""
        return [self._rows[i] for i in self.tree.selection() if i in self._rows]

    def _join(self, name: str) -> str:
        return self.vpath.rstrip("/") + "/" + name
