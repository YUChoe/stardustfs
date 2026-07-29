"""파일 액션 정의 — 활성 조건과 정의 일관성.

툴바와 컨텍스트 메뉴가 같은 정의를 쓰므로, 정의가 실제 메서드·번역 키와 어긋나면
런타임에야 드러난다. 여기서 미리 잡는다.
"""

from __future__ import annotations

from stardustlib.gui import action_defs, i18n
from stardustlib.gui.action_defs import Action, Need, is_enabled

FILE = {"type": "file", "name": "a.txt"}
DIR = {"type": "dir", "name": "sub"}


def _all_actions() -> list[Action]:
    flat = [a for group in action_defs.TOOLBAR_GROUPS for a in group]
    flat += [a for a in action_defs.CONTEXT_MENU if a is not None]
    return flat


def test_nothing_needs_no_selection():
    """업로드·새 폴더는 선택이 없어도 활성이다."""
    upload = Action("upload", "_upload", Need.NOTHING)
    assert is_enabled(upload, [], has_config=True) is True


def test_no_config_disables_everything():
    """설정 파일이 없으면 어떤 동작도 할 수 없다."""
    for action in _all_actions():
        assert is_enabled(action, [FILE], has_config=False) is False


def test_selection_rules():
    one_file = Action("download", "_download", Need.ONE_FILE)
    one = Action("move", "_move", Need.ONE)
    any_row = Action("delete", "_delete", Need.ANY)
    files = Action("backup_now", "_backup_selected", Need.FILES)

    # 선택 없음 → 선택을 요구하는 동작은 전부 비활성
    for action in (one_file, one, any_row, files):
        assert is_enabled(action, [], has_config=True) is False

    # 파일 1개
    assert is_enabled(one_file, [FILE], has_config=True) is True
    assert is_enabled(one, [FILE], has_config=True) is True
    assert is_enabled(any_row, [FILE], has_config=True) is True
    assert is_enabled(files, [FILE], has_config=True) is True

    # 폴더 1개 — 다운로드/복사는 파일 전용, 이동/삭제는 가능
    assert is_enabled(one_file, [DIR], has_config=True) is False
    assert is_enabled(one, [DIR], has_config=True) is True
    assert is_enabled(any_row, [DIR], has_config=True) is True
    assert is_enabled(files, [DIR], has_config=True) is False

    # 다중 선택 — 단일 대상 동작은 비활성, 일괄 동작은 활성
    many = [FILE, DIR]
    assert is_enabled(one_file, many, has_config=True) is False
    assert is_enabled(one, many, has_config=True) is False
    assert is_enabled(any_row, many, has_config=True) is True
    assert is_enabled(files, many, has_config=True) is True  # 파일이 하나라도 있음


def test_labels_exist_in_every_language():
    """정의된 라벨 키가 모든 번역에 있어야 한다(KeyError로 창이 깨지지 않도록)."""
    for action in _all_actions():
        for lang, table in i18n.TRANSLATIONS.items():
            assert action.key in table, f"{lang}에 '{action.key}' 라벨이 없음"


def test_methods_exist_on_app():
    """정의된 메서드가 StardustApp에 실제로 있어야 한다."""
    from stardustlib.gui.app import StardustApp

    for action in _all_actions():
        assert hasattr(StardustApp, action.method), action.method
