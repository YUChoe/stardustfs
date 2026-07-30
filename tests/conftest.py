"""테스트 공용 fixture.

Tk root는 한 프로세스에서 여러 번 만들면 테마·이미지 리소스가 이전 root에 묶여
실패한다(두 번째 이후가 조용히 skip된다). GUI 테스트가 여러 파일로 나뉘어도 창
하나를 공유하도록 세션 스코프로 둔다.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    """GUI 설정 저장을 임시 파일로 돌린다.

    prefs는 사용자 단위 파일이라, 테스트가 테마·언어·정렬을 바꾸면 실제 사용자
    설정이 덮어써진다.
    """
    from stardustlib.gui import prefs

    monkeypatch.setattr(prefs, "_path", lambda: str(tmp_path / "gui-prefs.json"))


@pytest.fixture(scope="session")
def gui_app():
    """세션 전체가 공유하는 StardustApp(설정 없이도 위젯은 모두 생성된다)."""
    tk = pytest.importorskip("tkinter")
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
