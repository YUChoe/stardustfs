"""GUI 레이아웃·상태 회귀(실제 Tk 창을 띄워 확인).

- 좁은 창에서도 하단 액션 툴바가 잘리지 않는다. pack은 선언 순서로 공간을 떼어 가므로
  expand=True인 파일 목록을 먼저 배치하면 마지막에 배치된 툴바가 화면 밖으로 밀린다
  (높은 DPI 배율의 노트북에서 업로드 버튼이 보이지 않는 증상).
- 파일 목록에 세로 스크롤바가 있다.
- 툴바 버튼 활성 상태가 선택을 따라간다.

Tk root는 한 프로세스에서 여러 번 만들면 테마·이미지 리소스가 이전 root에 묶여
실패하므로(두 번째 이후가 조용히 skip된다), 모듈 전체가 창 하나를 공유한다.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="module")
def app():
    """모듈 전체가 공유하는 StardustApp(설정 없이도 위젯은 모두 생성된다)."""
    from stardustlib.gui.app import StardustApp

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("디스플레이가 없는 환경")
    instance = StardustApp(root, None)
    try:
        yield instance
    finally:
        root.destroy()


def test_bottom_bars_survive_small_windows(app):
    """최소 크기·그보다 짧은 창에서도 업로드 버튼과 상태바가 배치된다."""
    root = app.root
    for size in ("760x480", "760x400", "900x620"):
        root.geometry(size)
        root.update()
        assert app.upload_btn.winfo_ismapped(), f"업로드 버튼 잘림: {size}"
        assert app.status.winfo_ismapped(), f"상태바 잘림: {size}"
        # 버튼 하단이 창 안에 들어와야 한다(잘리면 창 높이를 넘는다).
        bottom = app.upload_btn.winfo_rooty() + app.upload_btn.winfo_height()
        assert bottom <= root.winfo_rooty() + root.winfo_height(), size


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
