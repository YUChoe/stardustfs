"""GUI 레이아웃·상태 회귀(실제 Tk 창을 띄워 확인).

- 좁은 창에서도 하단 고정 바가 잘리지 않는다. pack은 선언 순서로 공간을 떼어 가므로
  expand=True인 컨테이너를 먼저 배치하면 나중에 배치된 고정 바가 밀려나거나 높이
  1px로 붕괴한다(업로드 버튼이 보이지 않던 증상, 관리 패널 액션 바가 없던 증상).
- 배치 순서: 경로 바 → 파일 목록 → 액션 툴바 → 스토리지·디바이스 패널 → 상태바.
  액션 툴바는 파일 목록과 같은 pane에 있어 분할선을 옮겨도 목록에 붙어 있다.
- 파일 목록에 세로 스크롤바가 있다.
- 툴바 버튼 활성 상태가 선택을 따라간다.

위젯이 mapped여도 창 밖으로 밀리거나 높이가 붕괴할 수 있으므로, winfo_ismapped가
아니라 좌표와 높이로 확인한다.

Tk root는 한 프로세스에서 여러 번 만들면 테마·이미지 리소스가 이전 root에 묶여
실패하므로(두 번째 이후가 조용히 skip된다), 모듈 전체가 창 하나를 공유한다.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

# 고정 바가 제 역할을 하려면 최소한 이 높이는 있어야 한다(px).
_MIN_BAR_HEIGHT = 24

# 디바이스·소스가 채워진 관리 패널 응답(트리가 커진 상태를 만든다).
_MGMT_DATA = {
    "online": True,
    "devices": [
        {
            "name": f"device-{i}",
            "online": True,
            "self": i == 0,
            "sources": [
                {
                    "source_id": f"loopback-{i}{j}",
                    "online": True,
                    "state": "ready",
                    "total": 1_000_000_000,
                    "used": 100_000_000,
                }
                for j in range(3)
            ],
        }
        for i in range(3)
    ],
}


@pytest.fixture()
def app(gui_app):
    """세션 공용 창(conftest.gui_app). 창 크기는 각 테스트가 직접 지정한다."""
    gui_app.config_path = None
    gui_app._uploads.clear()
    return gui_app


def _bottom_bars(app) -> dict:
    """창 크기와 무관하게 보여야 하는 고정 바들."""
    return {
        "상태바": app.status.master,
        "액션 툴바": app.upload_btn.master,
        "관리 액션 바": app.mgmt_actionbar,
    }


def _assert_visible(app, widget, label: str, context: str) -> None:
    """위젯이 창 안에 온전히 들어와 있고 높이가 붕괴하지 않았는지 확인한다."""
    root = app.root
    top = widget.winfo_rooty() - root.winfo_rooty()
    height = widget.winfo_height()
    assert height >= _MIN_BAR_HEIGHT, f"{label} 높이 붕괴({height}px): {context}"
    assert top + height <= root.winfo_height(), f"{label} 잘림: {context}"


def _fill_mgmt(app) -> None:
    """관리 트리를 채워 트리가 패널 공간을 다 요구하는 상태로 만든다."""
    app._populate_mgmt(True, _MGMT_DATA)
    app.root.update()


def test_bottom_bars_survive_small_windows(app):
    """최소 크기·그보다 짧은 창에서도 하단 고정 바가 모두 온전히 보인다."""
    root = app.root
    for size in ("760x480", "760x400", "900x620"):
        root.geometry(size)
        root.update()
        for label, widget in _bottom_bars(app).items():
            _assert_visible(app, widget, label, size)


def test_bottom_bars_survive_filled_mgmt_tree(app):
    """관리 트리가 채워져도 액션 바가 밀려나지 않는다.

    빈 트리는 공간을 요구하지 않아 결함이 드러나지 않는다 — 소스가 여럿 있는
    상태에서 트리가 패널 높이를 전부 가져가려 할 때가 실제 사용 조건이다.
    """
    root = app.root
    _fill_mgmt(app)
    for size in ("760x480", "900x620"):
        root.geometry(size)
        root.update()
        for label, widget in _bottom_bars(app).items():
            _assert_visible(app, widget, label, f"{size} (관리 트리 채움)")


def test_action_toolbar_sits_between_list_and_mgmt(app):
    """액션 툴바가 파일 목록 바로 아래, 스토리지·디바이스 패널 위에 있다."""
    root = app.root
    root.geometry("900x620")
    _fill_mgmt(app)
    root.update()

    def top(w) -> int:
        return w.winfo_rooty() - root.winfo_rooty()

    toolbar = app.upload_btn.master
    assert top(toolbar) >= top(app.tree) + app.tree.winfo_height() - 1, \
        "액션 툴바가 파일 목록 위에 있음"
    assert top(toolbar) + toolbar.winfo_height() <= top(app._mgmt_frame) + 1, \
        "액션 툴바가 관리 패널 아래에 있음"


def test_sash_cannot_swallow_action_toolbar(app):
    """분할선을 끝까지 올려도 액션 툴바가 잘리지 않는다.

    ttk.PanedWindow는 pane별 minsize가 없어 드래그로 위쪽 pane을 0까지 줄일 수 있다.
    """
    root = app.root
    root.geometry("900x620")
    _fill_mgmt(app)
    root.update()

    app._paned.sashpos(0, 0)
    app._clamp_sash()
    root.update()
    _assert_visible(app, app.upload_btn.master, "액션 툴바", "분할선 최상단")


def test_file_list_has_scrollbar(app):
    """파일 목록에 세로 스크롤바가 붙어 있다(항목이 창보다 많을 때 필요)."""
    app.root.update()
    siblings = app.tree.master.winfo_children()
    bars = [w for w in siblings if w.winfo_class() == "TScrollbar"]
    assert bars, "파일 목록에 스크롤바가 없음"
    assert bars[0].winfo_ismapped()


def test_toolbar_buttons_follow_selection(app, tmp_path):
    """선택 상태에 따라 버튼이 활성/비활성된다(안내 창으로 되돌리지 않도록)."""
    buttons = app._action_buttons

    def disabled(key: str) -> bool:
        return "disabled" in buttons[key].state()

    try:
        # 설정 없음 → 전부 비활성(업로드 포함)
        app.config_path = None
        app._sync_action_states()
        app.root.update()
        assert disabled("upload") and disabled("download")

        # 설정이 있고 선택 없음 → 선택 불필요 동작만 활성
        app.config_path = str(tmp_path / "config.json")
        app._sync_action_states()
        assert not disabled("upload")
        assert not disabled("mkdir")
        assert disabled("download") and disabled("delete")
        assert disabled("backup_now")

        # 파일 1개 선택 → 파일 동작 활성
        app._populate([{"type": "file", "name": "a.txt", "size": 1,
                        "backup": "none"}])
        app.tree.selection_set(app.tree.get_children()[0])
        app.root.update()
        assert not disabled("download")
        assert not disabled("copy")
        assert not disabled("backup_now")

        # 폴더 1개 선택 → 파일 전용 동작은 비활성, 이동/삭제는 활성
        app._populate([{"type": "dir", "name": "sub", "size": 0}])
        app.tree.selection_set(app.tree.get_children()[0])
        app.root.update()
        assert disabled("download") and disabled("copy")
        assert disabled("backup_now")
        assert not disabled("move") and not disabled("delete")
    finally:
        app.config_path = None
        app._populate([])
