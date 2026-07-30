"""GUI UX 회귀 — 목록 표시, 상위 이동 행, 정렬, 업로드 진행 행, 폼 다이얼로그.

실제 Tk 창을 하나 띄워 모듈 전체가 공유한다(root를 여러 번 만들면 테마·이미지
리소스가 이전 root에 묶여 실패한다).
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

_ROWS = [
    {"type": "file", "name": "b.txt", "size": 300, "backup": "none"},
    {"type": "dir", "name": "zeta", "size": 0},
    {"type": "file", "name": "A.bin", "size": 100, "backup": "replicated"},
    {"type": "dir", "name": "alpha", "size": 0},
    {"type": "file", "name": "c.log", "size": 200, "backup": "pending"},
]


@pytest.fixture()
def app(gui_app):
    """세션 공용 창(conftest.gui_app)을 쓰되 테스트마다 상태를 되돌린다."""
    gui_app.config_path = None
    gui_app._uploads.clear()
    gui_app._sort = ("name", False)
    gui_app._set_vpath("/")
    yield gui_app
    gui_app._clear_banner()


def _names(app) -> list[str]:
    """목록에 그려진 이름(아이콘 접두 제거)."""
    out = []
    for iid in app.tree.get_children():
        value = app.tree.set(iid, "name")
        out.append(value.split("  ", 1)[-1])
    return out


# --- 상위 이동 행 ---

def test_parent_row_only_below_root(app):
    """루트에서는 '..'가 없고, 하위 폴더에서는 첫 행에 있다."""
    app._set_vpath("/")
    app._populate(_ROWS)
    assert ".." not in _names(app)

    app._set_vpath("/sub")
    app._populate(_ROWS)
    assert _names(app)[0] == ".."


def test_parent_row_is_not_an_action_target(app):
    """'..'만 선택하면 선택을 요구하는 액션이 전부 비활성이다."""
    app._set_vpath("/sub")
    app._populate(_ROWS)
    app.tree.selection_set(app.tree.get_children()[0])  # '..'
    app.root.update()

    assert app._selected_rows() == []
    assert app._selected() is None
    buttons = app._action_buttons
    for key in ("delete", "move", "rename", "copy", "download", "backup_now"):
        assert "disabled" in buttons[key].state(), key


def test_parent_row_double_click_goes_up(app):
    """'..' 더블클릭은 상위 폴더로 이동한다."""
    app._set_vpath("/a/b")
    app._populate(_ROWS)
    app.tree.selection_set(app.tree.get_children()[0])
    app._on_double(None)
    assert app.vpath == "/a"


# --- 정렬 ---

def test_sort_puts_directories_first_by_name(app):
    app._set_vpath("/")
    app._sort = ("name", False)
    app._populate(_ROWS)
    assert _names(app) == ["alpha", "zeta", "A.bin", "b.txt", "c.log"]


def test_sort_by_size_and_direction(app):
    """크기 정렬에서도 폴더가 먼저 오고, 방향만 뒤집힌다."""
    app._set_vpath("/")
    app._sort = ("size", False)
    app._populate(_ROWS)
    assert _names(app) == ["alpha", "zeta", "A.bin", "c.log", "b.txt"]

    app._sort = ("size", True)
    app._populate(_ROWS)
    assert _names(app) == ["zeta", "alpha", "b.txt", "c.log", "A.bin"]


def test_sort_keeps_parent_row_first(app):
    """정렬 방향과 무관하게 '..'는 항상 첫 행이다."""
    app._set_vpath("/sub")
    for direction in (False, True):
        app._sort = ("name", direction)
        app._populate(_ROWS)
        assert _names(app)[0] == ".."


def test_heading_shows_sort_direction(app):
    app._set_vpath("/")
    app._sort = ("size", True)
    app._populate(_ROWS)
    assert app.tree.heading("size")["text"].endswith("▼")
    app._sort = ("size", False)
    app._populate(_ROWS)
    assert app.tree.heading("size")["text"].endswith("▲")


# --- 빈 상태 ---

def test_empty_state_message_depends_on_reason(app):
    """설정이 없을 때와 폴더가 빈 때의 안내가 다르다."""
    app._set_vpath("/")
    app.config_path = None
    app._populate([])
    app.root.update()
    assert app._empty_label.winfo_ismapped()
    assert app._empty_label.cget("text") == app.t["select_config_hint"]

    app._populate(_ROWS)
    app.root.update()
    assert not app._empty_label.winfo_ismapped()


# --- 업로드 진행 행 ---

def test_upload_rows_survive_refresh(app):
    """진행 중인 업로드는 목록을 다시 그려도 남는다."""
    from stardustlib.gui.file_ops import UploadItem

    app._set_vpath("/")
    app._uploads.append(
        UploadItem(local="C:/tmp/x.bin", name="x.bin", dest="/"))
    try:
        app._populate(_ROWS)
        assert "x.bin" in _names(app)
        app._populate(_ROWS)  # 폴링 새로고침을 흉내
        assert "x.bin" in _names(app)
    finally:
        app._uploads.clear()


def test_upload_rows_hidden_in_other_folders(app):
    """대상 폴더가 아니면 진행 행을 그리지 않는다(업로드는 계속된다)."""
    from stardustlib.gui.file_ops import UploadItem

    app._uploads.append(
        UploadItem(local="C:/tmp/y.bin", name="y.bin", dest="/other"))
    try:
        app._set_vpath("/")
        app._populate(_ROWS)
        assert "y.bin" not in _names(app)
        app._set_vpath("/other")
        app._populate(_ROWS)
        assert "y.bin" in _names(app)
    finally:
        app._uploads.clear()
        app._set_vpath("/")


def test_upload_row_is_not_an_action_target(app):
    """업로드 진행 행은 삭제·백업 등의 대상이 되지 않는다."""
    from stardustlib.gui.file_ops import UploadItem

    app._uploads.append(
        UploadItem(local="C:/tmp/z.bin", name="z.bin", dest="/"))
    try:
        app._set_vpath("/")
        app._populate([])
        app.tree.selection_set(app.tree.get_children()[0])
        app.root.update()
        assert app._selected_rows() == []
    finally:
        app._uploads.clear()


def test_cancel_uploads_drops_only_queued(app):
    """취소는 대기 항목만 버리고 진행 중인 파일은 남긴다."""
    from stardustlib.gui.file_ops import UploadItem

    app._set_vpath("/")
    running = UploadItem(local="C:/tmp/r.bin", name="r.bin", dest="/",
                         state="running")
    queued = UploadItem(local="C:/tmp/q.bin", name="q.bin", dest="/")
    app._uploads.extend([running, queued])
    try:
        app._cancel_uploads()
        assert app._uploads == [running]
    finally:
        app._uploads.clear()


# --- 브레드크럼 ---

def test_breadcrumb_segments():
    from stardustlib.gui.widgets.breadcrumb import split_path

    assert split_path("/") == [("/", "/")]
    assert split_path("/a/b") == [("/", "/"), ("a", "/a"), ("b", "/a/b")]


def test_breadcrumb_follows_vpath(app):
    app._set_vpath("/movies/2026")
    assert app.breadcrumb._vpath == "/movies/2026"


# --- 액션 정의 ---

def test_every_action_method_exists():
    from stardustlib.gui import action_defs
    from stardustlib.gui.app import StardustApp

    actions = [a for g in action_defs.TOOLBAR_GROUPS for a in g]
    actions += [a for a in action_defs.CONTEXT_MENU if a is not None]
    for action in actions:
        assert hasattr(StardustApp, action.method), action.method


def test_tooltip_keys_exist_in_all_languages():
    """툴팁·비활성 사유 문구가 ko/en 양쪽에 있다."""
    from stardustlib.gui import action_defs, i18n

    actions = [a for g in action_defs.TOOLBAR_GROUPS for a in g]
    actions += [a for a in action_defs.CONTEXT_MENU if a is not None]
    for lang, texts in i18n.TRANSLATIONS.items():
        for action in actions:
            assert f"tip_{action.key}" in texts, (lang, action.key)
        for need in action_defs.Need:
            if need is not action_defs.Need.NOTHING:
                assert f"need_{need.value}" in texts, (lang, need)


def test_tooltip_adds_hint_when_disabled():
    from stardustlib.gui import action_defs, i18n

    t = i18n.get_text("ko")
    upload = action_defs.TOOLBAR_GROUPS[0][0]
    download = action_defs.TOOLBAR_GROUPS[0][1]
    # 선택이 필요 없는 동작은 비활성이어도 조건 문구를 붙이지 않는다.
    assert "\n" not in action_defs.tooltip(upload, t, enabled=False)
    assert action_defs.tooltip(download, t, enabled=False).endswith(
        t["need_one_file"])
    assert "\n" not in action_defs.tooltip(download, t, enabled=True)


# --- 관리 패널 ---

def test_source_label_prefers_readable_name(app):
    """소스는 종류 + 파일명으로 보여 주고, 경로가 없으면 ID를 쓴다."""
    kind = app.t["src_kind_loopback"]
    assert app._source_label(
        {"source_id": "loopback-2ce2ec", "type": "loopback",
         "path": "/data/dev-a.img"}
    ) == f"{kind} · dev-a.img"
    assert app._source_label({"source_id": "loopback-2ce2ec"}) == "loopback-2ce2ec"


def test_capacity_includes_percentage(app):
    assert app._cap_str({"used": 500, "total": 1000}).endswith("(50%)")
    assert app._is_full({"used": 950, "total": 1000})
    assert not app._is_full({"used": 500, "total": 1000})


def test_mgmt_collapse_keeps_action_bar(app):
    """접었다 펴도 액션 바가 붕괴하지 않는다(pack 순서 회귀)."""
    app.root.geometry("900x620")
    app.root.update()
    for _ in range(2):
        app._toggle_mgmt()
        app.root.update()
        assert app.mgmt_actionbar.winfo_height() >= 24
    assert app._mgmt_collapsed is False


# --- 폼 다이얼로그 ---

def test_form_dialog_validates_before_submit(app):
    from stardustlib.gui.widgets.form_dialog import INT, Field, FormDialog

    called = []
    fields = (
        Field("name", "이름", required=True),
        Field("size", "크기", INT, initial="5", minimum=10),
    )
    dlg = FormDialog(app, "테스트", fields, lambda v, d: called.append(v))
    try:
        dlg._on_ok()  # 이름이 비어 있다
        assert not called
        assert dlg.error_label.cget("text")

        dlg._vars["name"].set("a")
        dlg._on_ok()  # 크기가 최솟값 미만
        assert not called

        dlg._vars["size"].set("20")
        dlg._on_ok()
        assert called == [{"name": "a", "size": 20}]
    finally:
        dlg.close()


def test_form_dialog_keeps_values_on_error(app):
    """제출이 실패해도 창을 닫지 않고 입력값을 유지한다."""
    from stardustlib.gui.widgets.form_dialog import Field, FormDialog

    def submit(values, dlg):
        dlg.error("서버가 거부했습니다")

    dlg = FormDialog(app, "테스트", (Field("email", "이메일", required=True),),
                     submit)
    try:
        dlg._vars["email"].set("a@b.c")
        dlg._on_ok()
        assert dlg.win.winfo_exists()
        assert dlg._vars["email"].get() == "a@b.c"
        assert dlg.error_label.cget("text") == "서버가 거부했습니다"
    finally:
        dlg.close()


# --- 언어 전환 ---

def test_language_switch_preserves_view_state(app):
    """언어를 바꿔도 보고 있던 폴더·정렬·관리 패널 접힘이 유지된다."""
    app._set_vpath("/movies")
    app._sort = ("size", True)
    app._mgmt_collapsed = True
    before_lang = app.lang
    try:
        app._set_language("en" if before_lang != "en" else "ko")
        app.root.update()
        assert app.vpath == "/movies"
        assert app._sort == ("size", True)
        assert app._mgmt_collapsed is True
        assert app.breadcrumb._vpath == "/movies"
        # 라벨은 새 언어로 바뀐다
        assert app._action_buttons["upload"].cget("text") == app.t["upload"]
    finally:
        app._mgmt_collapsed = False
        app._set_language(before_lang)
        app._set_vpath("/")
        app.root.update()


# --- 배너 ---

def test_banner_counts_repeats_instead_of_stacking(app):
    app._show_banner("연결 실패")
    first = app._banner_label.cget("text")
    app._show_banner("연결 실패")
    second = app._banner_label.cget("text")
    assert first == "연결 실패"
    assert second == "연결 실패" + app.t["banner_repeat"].format(count=2)

    app._show_banner("다른 오류")
    assert app._banner_label.cget("text") == "다른 오류"
    app._clear_banner()
    app.root.update()
    assert not app._banner.winfo_ismapped()
