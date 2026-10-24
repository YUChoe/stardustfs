"""GUI UX 개선 회귀 — 작업 상태, 업로드 큐 정리·충돌, 상태바 줄임, 재구성 유지.

세션 공용 창(conftest.gui_app)을 쓴다(Tk root를 여러 번 만들면 실패한다).
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture()
def app(gui_app):
    gui_app.config_path = None
    gui_app._uploads.clear()
    gui_app._uploading = False
    gui_app._busy.clear()
    gui_app._set_vpath("/")
    yield gui_app
    gui_app._uploads.clear()
    gui_app._uploading = False
    gui_app._busy.clear()
    gui_app._clear_banner()


def _item(name: str, state: str, dest: str = "/", local: str | None = None):
    from stardustlib.gui.uploads import UploadItem

    return UploadItem(local=local or f"C:/tmp/{name}", name=name, dest=dest,
                      state=state)


# --- 진행 중 작업과 상태 문구 ---

def test_status_stays_busy_until_every_job_ends(app):
    """한 작업이 끝나도 다른 작업이 남아 있으면 '준비됨'으로 바뀌지 않는다."""
    app._job_begin("a", "작업 A")
    app._job_begin("b", "작업 B")
    app._job_end("b")
    assert app._status_full == "작업 A"
    app._job_end("a")
    assert app._status_full == app.t["ready"]


def test_status_returns_to_backup_progress_when_idle(app):
    """데몬 백업이 도는 중이면 작업이 끝난 뒤 그 진행 문구로 돌아간다."""
    app._showing_progress = True
    app._progress_text = "백업 중: x 1/2 청크"
    try:
        app._job_begin("a", "조회 중")
        app._job_end("a")
        assert app._status_full == "백업 중: x 1/2 청크"
    finally:
        app._showing_progress = False
        app._progress_text = ""


def test_activity_indicator_follows_jobs(app):
    """진행률을 모르는 작업 중에는 무한 막대가, 끝나면 사라진다."""
    app._job_begin("a", "다운로드 중")
    app._sync_busy_indicator()
    app.root.update()
    assert app._progress_mode == "indeterminate"
    assert app.progress.winfo_ismapped()
    app._job_end("a")
    app.root.update()
    assert app._progress_mode is None
    assert not app.progress.winfo_ismapped()


def test_determinate_progress_wins_over_activity(app):
    app._set_progress(1, 4)
    app._job_begin("a", "조회 중")
    app._sync_busy_indicator()
    assert app._progress_mode == "determinate"
    app._job_end("a")
    app._set_progress(0, 0)
    assert app._progress_mode is None


# --- 업로드 큐 ---

def test_reenqueue_failed_file_replaces_old_row(app, tmp_path):
    """실패 행이 남은 파일을 다시 올리면 그 행을 지우고 새로 넣는다(재시도)."""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    local = str(path.resolve())
    app._uploads.append(_item("a.bin", "failed", local=local))
    app._uploading = True  # 실제 업로드는 시작하지 않는다
    app._enqueue_uploads([str(path)])
    assert [(i.name, i.state) for i in app._uploads] == [("a.bin", "queued")]


def test_enqueue_skips_files_already_queued(app, tmp_path):
    path = tmp_path / "b.bin"
    path.write_bytes(b"x")
    app._uploading = True
    app._enqueue_uploads([str(path)])
    app._enqueue_uploads([str(path)])
    assert len(app._uploads) == 1


def test_clear_removes_only_finished_rows(app):
    running, queued = _item("r", "running"), _item("q", "queued")
    app._uploads.extend([running, queued, _item("f", "failed"),
                         _item("e", "exists")])
    app._clear_uploads()
    assert app._uploads == [running, queued]


def test_upload_controls_follow_queue(app):
    """대기 항목이 있으면 '취소', 끝난 표시가 있으면 '정리' 버튼이 보인다."""
    buttons = app._upload_ctl_buttons

    def shown() -> set[str]:
        app._sync_upload_controls()
        app.root.update()
        return {k for k, b in buttons.items() if b.winfo_ismapped()}

    assert shown() == set()
    app._uploads.append(_item("q", "queued"))
    assert shown() == {"upload_cancel"}
    app._uploads.append(_item("f", "failed"))
    assert shown() == {"upload_cancel", "upload_clear"}
    app._uploads[:] = [i for i in app._uploads if i.state == "failed"]
    assert shown() == {"upload_clear"}


@pytest.mark.parametrize("choice", ["overwrite", "rename"])
def test_conflict_choice_requeues_with_mode(app, monkeypatch, choice):
    """같은 이름 충돌에서 덮어쓰기/둘 다 유지를 고르면 그 방식으로 다시 올린다."""
    from stardustlib.gui import uploads

    conflicts = [_item("x.txt", "exists"), _item("y.txt", "exists")]
    app._uploads.extend(conflicts)
    seen: list = []

    class _Pick:
        def __init__(self, _app, _title, message, choices, on_choose, **_kw):
            seen.append((message, [k for k, _ in choices]))
            on_choose(choice)

    monkeypatch.setattr(uploads, "ChoiceDialog", _Pick)
    monkeypatch.setattr(app, "_upload_next", lambda: None)
    app._ask_conflicts(conflicts)

    assert seen and seen[0][1] == ["rename", "overwrite", "skip"]
    assert [(i.state, i.mode) for i in app._uploads] == [("queued", choice)] * 2


def test_conflict_skip_drops_rows(app, monkeypatch):
    from stardustlib.gui import uploads

    conflicts = [_item("x.txt", "exists")]
    app._uploads.extend(conflicts)

    class _Skip:
        def __init__(self, _app, _t, _m, _c, on_choose, **_kw):
            on_choose("skip")

    monkeypatch.setattr(uploads, "ChoiceDialog", _Skip)
    monkeypatch.setattr(app, "_upload_next", lambda: None)
    before = app._upload_skip
    app._ask_conflicts(conflicts)
    assert app._uploads == []
    assert app._upload_skip == before + 1
    app._upload_skip = 0


def test_choice_dialog_returns_key_once(app):
    from stardustlib.gui.widgets.choice_dialog import ChoiceDialog

    picked: list[str] = []
    dlg = ChoiceDialog(app, "t", "m", (("a", "A"), ("b", "B")),
                       picked.append, cancel_key="b")
    dlg.choose("a")
    dlg.choose("b")  # 이미 확정됨 — 무시
    assert picked == ["a"]
    assert not dlg.win.winfo_exists()


def test_drop_of_folder_warns(app, tmp_path):
    """폴더를 끌어다 놓으면 건너뛰고 알린다."""
    app.config_path = str(tmp_path / "config.json")
    app._on_drop([str(tmp_path)])
    assert app._banner_message == app.t["upload_drop_dirs"]


# --- 키보드 ---

def test_enter_opens_folder(app):
    assert app.tree.bind("<Return>")
    app._populate([{"type": "dir", "name": "sub", "size": 0}])
    app.tree.selection_set(app.tree.get_children()[0])
    navigated: list[str] = []
    original = app._navigate
    app._navigate = navigated.append
    try:
        app._on_double(None)
    finally:
        app._navigate = original
    assert navigated == ["/sub"]


# --- 상태바 ---

def test_long_status_is_ellipsised_with_full_text_kept(app):
    """좁은 창에서 긴 상태 문구는 '…'로 줄이고 전체는 툴팁용으로 남긴다."""
    app.root.geometry("760x480")
    app.root.update()
    full = "업로드 완료: 성공 120 / 실패 3 / 건너뜀 7 — " + "아주 긴 파일 이름 " * 8
    app._set_status(full)
    app.root.update()
    app._fit_status()
    shown = app.status.cget("text")
    assert app._status_full == full
    assert shown.endswith("…") and len(shown) < len(full)
    app._set_status("준비됨")
    app.root.update()
    assert app.status.cget("text") == "준비됨"


def test_banner_shows_level_icon(app):
    app._show_banner("경고 문구", level="warning")
    assert app._banner_icon.cget("text") == "⚠"
    assert app._banner_label.cget("text") == "경고 문구"
    app._show_banner("안내", level="info")
    assert app._banner_icon.cget("text") == "ℹ"


# --- 재구성(테마 전환) ---

def test_theme_switch_keeps_view_and_banner(app):
    before = app.theme
    other = "light" if before == "dark" else "dark"
    app._set_vpath("/movies")
    app._show_banner("연결 실패")
    try:
        app._set_theme(other)
        app.root.update()
        assert app.theme == other
        assert app.vpath == "/movies"
        assert app._banner.winfo_ismapped()
        assert app._banner_label.cget("text") == "연결 실패"
    finally:
        app._set_theme(before)
        app._set_vpath("/")
        app.root.update()


# --- 관리 패널 빈 상태 ---

def test_mgmt_empty_states(app):
    label = app._mgmt_empty_label
    app._populate_mgmt(True, {"online": False, "devices": []})
    app.root.update()
    assert label.winfo_ismapped()
    assert label.cget("text") == app.t["mgmt_empty_none"]

    app._populate_mgmt(False, RuntimeError("boom"))
    assert label.cget("text") == app.t["mgmt_empty_failed"]

    app._populate_mgmt(True, {"online": False, "devices": [
        {"name": "A", "online": True, "self": True, "sources": []}]})
    app.root.update()
    assert not label.winfo_ismapped()


# --- 분할선 저장 ---

def test_sash_ratio_saved_on_release(app):
    """분할선을 놓으면 비율이 prefs에 저장된다(언어 전환 때만 저장되던 결함)."""
    from stardustlib.gui import prefs

    app.root.geometry("900x620")
    app.root.update()
    paned = app._paned
    target = int(paned.winfo_height() * 0.5)
    paned.sashpos(0, target)
    app.root.update()
    paned.event_generate("<ButtonRelease-1>", x=5, y=target)
    app.root.update()
    saved = prefs.load().get("sash_ratio")
    assert saved is not None and abs(saved - paned.sashpos(0)
                                     / paned.winfo_height()) < 0.02


# --- 툴바 넘침 ---

def test_toolbar_folds_last_group_when_narrow(app):
    """좁은 창(영어 UI 760px)에서는 마지막 그룹을 '더보기'로 접어 잘리지 않게 한다."""
    before = app.lang
    try:
        app._set_language("en")
        root = app.root
        root.geometry("760x480")
        root.update()
        app._fit_toolbar()
        root.update()
        tb = app._toolbar
        mapped = [w for w in tb.pack_slaves()]
        right_edge = max(w.winfo_x() + w.winfo_width() for w in mapped)
        assert right_edge <= tb.winfo_width(), "툴바 버튼이 잘림"
        if app._toolbar_needed() > tb.winfo_width():
            assert app._tb_more.winfo_ismapped()
            assert not app._action_buttons["restore_now"].winfo_ismapped()

        root.geometry("1200x620")
        root.update()
        app._fit_toolbar()
        root.update()
        assert not app._tb_more.winfo_ismapped()
        assert app._action_buttons["restore_now"].winfo_ismapped()
    finally:
        app._set_language(before)
        app.root.geometry("900x620")
        app.root.update()


# --- 로그인 상태 캐시 ---

def _drain(app, cond, timeout=3.0):
    import time

    end = time.time() + timeout
    while time.time() < end and not cond():
        app.io_worker.poll()
        app.root.update()
        time.sleep(0.02)


def test_login_cache_follows_external_logout(app, tmp_path, monkeypatch):
    """창 밖에서 토큰이 사라지면(401 갱신 거부 등) 다음 폴링에 화면이 따라온다."""
    from stardustlib.gui import actions

    cfg = str(tmp_path / "config.json")
    app.config_path = cfg
    app._login_cache[cfg] = True
    calls: list[str] = []
    monkeypatch.setattr(actions, "is_logged_in", lambda _c: False)
    monkeypatch.setattr(app, "_refresh_login_state", lambda: calls.append("ui"))
    monkeypatch.setattr(app, "refresh", lambda *a, **k: calls.append("list"))
    monkeypatch.setattr(app, "_refresh_mgmt", lambda: calls.append("mgmt"))
    try:
        app._poll_login()
        _drain(app, lambda: app._login_cache.get(cfg) is False)
        assert app._login_cache[cfg] is False
        assert calls == ["ui", "list", "mgmt"]
        app._poll_login()  # 바뀌지 않았으면 다시 그리지 않는다
        _drain(app, lambda: "login" not in app._inflight)
        assert calls == ["ui", "list", "mgmt"]
    finally:
        app._login_cache.clear()
