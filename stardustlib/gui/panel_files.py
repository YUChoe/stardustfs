"""파일 목록 패널 — 브레드크럼 경로 바, 목록 Treeview, 컨텍스트 메뉴, 탐색.

StardustApp에 믹스인으로 결합한다. 파일에 대한 실제 동작(업로드/삭제/백업 등)은
file_ops.py가 담당하고, 이 모듈은 무엇을 보여주고 무엇이 선택됐는지를 다룬다.

경로는 `self.vpath` 하나만을 원천으로 쓴다. 상위 폴더로는 브레드크럼 세그먼트,
'↑ 상위' 버튼, 목록 첫 행의 `..`로 갈 수 있다.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from stardustlib.gui import action_defs, actions, theme
from stardustlib.gui.format import human_bytes
from stardustlib.gui.widgets import tooltip
from stardustlib.gui.widgets.breadcrumb import Breadcrumb

try:  # Accent 버튼 스타일 제공(미설치 시 기본 버튼).
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None

logger = logging.getLogger(__name__)

# 상위 폴더 행의 이름(파일 탐색기와 같은 표기).
PARENT_NAME = ".."

# 백업 상태 정렬 순위(완료 > 대기 > 미백업).
_BACKUP_RANK = {"replicated": 0, "pending": 1}

# 업로드 진행 행의 상태별 표시 키.
_UPLOAD_STATE_KEYS = {
    "queued": "up_state_queued",
    "running": "up_state_running",
    "failed": "up_state_failed",
    "exists": "up_state_exists",
}


class FilesPanelMixin:
    """파일 목록 위젯 구성 + 탐색 + 선택."""

    # --- 위젯 구성 ---

    def _build_path_bar(self, parent) -> None:
        """브레드크럼 경로 + 상위/새로고침 + 계정 버튼.

        설정 진입은 '파일' 메뉴에 있고, 설정 파일명은 창 제목에 표시한다.
        """
        t = self.t
        pframe = ttk.Frame(parent, padding=(10, 8))
        pframe.pack(fill="x")

        # 계정: 상태에 따라 라벨·동작이 바뀌는 버튼 하나만 둔다.
        self.account_btn = ttk.Button(pframe, text=t["login"],
                                      command=self._login)
        self.account_btn.pack(side="right")
        self.account_label = ttk.Label(
            pframe, text="",
            foreground=theme.text_colour("fg_subtle",
                                         dark=self.theme == "dark"))
        self.account_label.pack(side="right", padx=(0, 8))

        ttk.Button(pframe, text=t["up"], command=self._up).pack(side="left")
        ttk.Button(pframe, text=t["refresh"], command=self.refresh).pack(
            side="left", padx=(6, 10))
        self.breadcrumb = Breadcrumb(pframe, self._navigate,
                                     dark=self.theme == "dark")
        self.breadcrumb.pack(side="left", fill="x", expand=True)
        self.breadcrumb.set_path(self.vpath)

    def _build_file_tree(self, parent) -> None:
        """파일 목록 Treeview + 세로 스크롤바 + 빈 상태 안내."""
        t = self.t
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        cols = ("name", "size", "backup")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings",
                                 selectmode="extended")
        for c, head, w, anchor in (
            ("name", t["col_name"], 440, "w"),
            ("size", t["col_size"], 110, "e"),
            ("backup", t["col_backup"], 140, "w"),
        ):
            # 헤딩은 Tk 기본(가운데)을 유지한다. 데이터 정렬에 맞추면 오른쪽 정렬한
            # '크기'와 왼쪽 정렬한 '백업'이 컬럼 경계에서 붙어 한 단어로 읽힌다
            # (다크 테마에서 컬럼 구분선이 거의 보이지 않는다).
            self.tree.heading(
                c, text=head, command=lambda col=c: self._sort_by(col))
            self.tree.column(c, width=w, anchor=anchor)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0))
        sb.pack(side="right", fill="y")

        self._apply_row_tags()

        # 행이 없을 때 사유를 목록 한가운데 안내한다(상태바 문구만으로는 놓치기 쉽다).
        self._empty_label = ttk.Label(
            holder, text="", anchor="center",
            foreground=theme.text_colour("fg_subtle", dark=self.theme == "dark"))

        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewSelect>>", self._sync_action_states)
        self._build_context_menu()
        self._sync_action_states()

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

        for gi, group in enumerate(action_defs.TOOLBAR_GROUPS):
            if gi:
                ttk.Separator(tb, orient="vertical").pack(
                    side="left", fill="y", padx=10)
            for ai, action in enumerate(group):
                style = ("Cta.TButton"
                         if (action.accent and sv_ttk is not None) else "TButton")
                btn = ttk.Button(
                    tb, text=self.t[action.key], style=style,
                    command=getattr(self, action.method),
                )
                btn.pack(side="left", padx=(0 if not ai else 6, 0))
                self._action_buttons[action.key] = btn
                tooltip.attach(btn, lambda a=action, b=btn: action_defs.tooltip(
                    a, self.t, enabled="disabled" not in b.state()))
        # 업로드 버튼은 잘림 회귀 테스트에서 참조하므로 이름을 남긴다.
        self.upload_btn = self._action_buttons["upload"]
        self._sync_action_states()

    def _apply_row_tags(self) -> None:
        """상태별 행 색을 현재 테마에 맞춰 지정한다.

        Treeview는 셀 단위 색을 지원하지 않아 행 전체가 물들므로, 눈에 띄어야 하는
        상태만 색을 준다 — 대기는 기본색으로 두고 완료(녹색)와 미백업(흐림)만
        구분한다. 대기까지 강조하면 목록 대부분이 물들어 오히려 읽기 어렵다.
        """
        if not self._tree_alive():
            return
        dark = self.theme == "dark"

        def colour(name: str) -> str:
            return theme.text_colour(name, dark=dark)

        for tag, name in (
            ("bk_done", "success"),
            ("bk_pending", "fg_default"),
            ("bk_none", "fg_subtle"),
            ("upload", "accent"),
            ("upload_failed", "danger"),
            ("parent", "fg_subtle"),
        ):
            self.tree.tag_configure(tag, foreground=colour(name))

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
        for index, action in self._ctx_entries:
            enabled = action_defs.is_enabled(action, rows, has_config=has_config)
            if action.key == "upload_cancel":
                enabled = bool(getattr(self, "_uploads", None))
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
        # 언어 전환 재구성 중에는 옛 tree 객체가 남아 있으나 이미 파괴된 상태다.
        rows = self._selected_rows() if self._tree_alive() else []
        has_config = bool(self.config_path)
        for group in action_defs.TOOLBAR_GROUPS:
            for action in group:
                btn = buttons.get(action.key)
                if btn is not None:
                    self._enable(btn, action_defs.is_enabled(
                        action, rows, has_config=has_config))

    # --- 탐색 ---

    def _navigate(self, vpath: str) -> None:
        """브레드크럼 세그먼트 등에서 지정한 경로로 이동한다."""
        self._set_vpath(vpath)
        self.refresh()

    def _set_vpath(self, vpath: str) -> None:
        """현재 경로를 바꾸고 브레드크럼에 반영한다(경로의 단일 원천)."""
        self.vpath = vpath or "/"
        bc = getattr(self, "breadcrumb", None)
        if bc is not None:
            bc.set_path(self.vpath)

    def refresh(self, counts: bool = True) -> None:
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
        row = self._row_at_cursor()
        if not row:
            return
        if row.get("parent"):
            self._up()
            return
        if row["type"] == "dir":
            self._navigate(self._join(row["name"]))

    def _row_at_cursor(self) -> dict | None:
        """선택된 첫 행(상위 이동 행 포함)."""
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _up(self) -> None:
        parent = self.vpath.rstrip("/").rsplit("/", 1)[0] or "/"
        self._navigate(parent)

    # --- 정렬 ---

    def _sort_by(self, column: str) -> None:
        """헤딩 클릭 — 같은 컬럼이면 방향을 뒤집는다."""
        col, desc = self._sort
        self._sort = (column, not desc if col == column else False)
        from stardustlib.gui import prefs

        prefs.save(sort=list(self._sort))
        self._populate(self._last_rows)

    def _sort_key(self, row: dict):
        """이름을 보조 키로 둔다 — 크기·상태가 같은 행(폴더 등)이 임의 순서로
        섞이지 않게 한다."""
        col, _desc = self._sort
        name = str(row.get("name", "")).lower()
        if col == "size":
            return (row.get("size") or 0, name)
        if col == "backup":
            return (_BACKUP_RANK.get(row.get("backup", ""), 2), name)
        return (name,)

    def _apply_sort(self, rows: list[dict]) -> list[dict]:
        """폴더를 먼저 두고 각 그룹 안에서만 정렬한다(파일 탐색기와 같다).

        방향을 뒤집어도 폴더 그룹이 파일 아래로 내려가지 않는다.
        """
        _col, desc = self._sort
        dirs = [r for r in rows if r["type"] == "dir"]
        files = [r for r in rows if r["type"] != "dir"]
        return (sorted(dirs, key=self._sort_key, reverse=desc)
                + sorted(files, key=self._sort_key, reverse=desc))

    def _heading_text(self, column: str, base: str) -> str:
        col, desc = self._sort
        if column != col:
            return base
        return f"{base} {'▼' if desc else '▲'}"

    def _update_headings(self) -> None:
        for column, key in (("name", "col_name"), ("size", "col_size"),
                            ("backup", "col_backup")):
            self.tree.heading(
                column, text=self._heading_text(column, self.t[key]))

    # --- 목록 채우기 ---

    def _backup_label(self, status: str) -> str:
        """로컬 replication_status를 표시 라벨로 변환한다."""
        if status == "replicated":
            return self.t["bk_done"]
        if status == "pending":
            return self.t["bk_pending"]
        return self.t["bk_none"]

    def _backup_tag(self, status: str) -> str:
        if status == "replicated":
            return "bk_done"
        return "bk_pending" if status == "pending" else "bk_none"

    def _populate(self, rows: list[dict]) -> None:
        """서버 목록을 그린다. 상위 이동 행과 업로드 진행 행을 함께 얹는다."""
        self._last_rows = list(rows)
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        self._update_headings()

        # '..'는 정렬과 무관하게 항상 첫 행이다.
        if self.vpath != "/":
            iid = self.tree.insert(
                "", "end", values=(f"📁  {PARENT_NAME}", "", ""),
                tags=("parent",))
            self._rows[iid] = {"type": "dir", "name": PARENT_NAME,
                               "parent": True}

        for r in self._apply_sort(rows):
            is_file = r["type"] == "file"
            size = human_bytes(r["size"]) if is_file else ""
            status = r.get("backup", "")
            backup = self._backup_label(status) if is_file else ""
            icon = "📄" if is_file else "📁"
            iid = self.tree.insert(
                "", "end",
                values=(f"{icon}  {r['name']}", size, backup),
                tags=(self._backup_tag(status),) if is_file else (),
            )
            self._rows[iid] = r
        self._render_uploads()
        self._update_empty_state(rows)
        # 목록을 다시 채우면 선택이 사라지므로 버튼 상태도 함께 되돌린다.
        self._sync_action_states()

    def _update_empty_state(self, rows: list[dict]) -> None:
        """행이 없을 때만 사유별 안내를 목록 위에 겹쳐 보여준다."""
        label = getattr(self, "_empty_label", None)
        if label is None:
            return
        if rows or getattr(self, "_uploads", None):
            label.place_forget()
            return
        if not self.config_path:
            text = self.t["select_config_hint"]
        elif not self._logged_in():
            text = self.t["login_required"]
        else:
            text = self.t["empty_folder"]
        label.config(text=text)
        label.place(relx=0.5, rely=0.4, anchor="center")

    def _render_uploads(self) -> None:
        """현재 폴더가 대상인 업로드 항목을 진행 행으로 얹는다.

        서버 목록과 업로드 큐를 한 자료구조에 섞지 않는다 — 3초 폴링이 목록을 통째로
        다시 그려도 진행 행이 살아남아야 하기 때문이다.
        """
        for item in getattr(self, "_uploads", []):
            if item.dest != self.vpath:
                continue  # 다른 폴더에 올리는 중
            state = self.t[_UPLOAD_STATE_KEYS[item.state]]
            tag = "upload_failed" if item.state == "failed" else "upload"
            iid = self.tree.insert(
                "", 0, values=(f"⬆  {item.name}", "", state), tags=(tag,))
            self._rows[iid] = {"type": "file", "name": item.name,
                               "upload": True}

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
        # 병기한다. none(미백업) 뿐이면 서버 조회를 생략한다.
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
            if row["type"] != "file" or row.get("upload"):
                continue
            info = counts.get(row["name"])
            if not info:
                continue
            label = self._backup_label(row.get("backup", ""))
            text = f"{label} ({info['online']}/{info['min']})"
            self.tree.set(iid, "backup", text)

    # --- 선택 ---

    def _tree_alive(self) -> bool:
        """파일 목록 위젯이 살아 있는지(재구성 중에는 옛 객체가 남아 있다)."""
        tree = getattr(self, "tree", None)
        if tree is None:
            return False
        try:
            return bool(tree.winfo_exists())
        except tk.TclError:
            return False

    def _selected(self) -> dict | None:
        rows = self._selected_rows()
        return rows[0] if rows else None

    def _selected_rows(self) -> list[dict]:
        """액션 대상이 되는 선택 행(상위 이동 행과 업로드 진행 행은 제외)."""
        if not self._tree_alive():
            return []
        return [
            self._rows[i] for i in self.tree.selection()
            if i in self._rows
            and not self._rows[i].get("parent")
            and not self._rows[i].get("upload")
        ]

    def _join(self, name: str) -> str:
        return self.vpath.rstrip("/") + "/" + name
