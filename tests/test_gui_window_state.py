"""창 위치·크기 복원 규칙 (Tk 비의존)."""

from __future__ import annotations

from stardustlib.gui.window_state import initial_geometry, parse_geometry

SCREEN = (1920, 1080)
DEFAULT = (900, 620)
MINIMUM = (760, 480)


def _anywhere(_x, _y):
    return True


def _nowhere(_x, _y):
    return False


def test_parse_geometry():
    assert parse_geometry("900x620+10+20") == (900, 620, 10, 20)
    assert parse_geometry("900x620+-1800+20") == (900, 620, -1800, 20)
    assert parse_geometry("garbage") is None
    assert parse_geometry("") is None


def test_no_saved_state_centres_default_size():
    assert initial_geometry({}, SCREEN, DEFAULT, MINIMUM, _anywhere) == \
        "900x620+510+230"


def test_saved_position_on_a_monitor_is_kept():
    saved = {"geometry": "1000x700+-1500+100"}  # 왼쪽 보조 모니터
    assert initial_geometry(saved, SCREEN, DEFAULT, MINIMUM, _anywhere) == \
        "1000x700+-1500+100"


def test_saved_position_off_screen_is_recentred():
    """보조 모니터를 뗀 뒤에는 크기만 살리고 주 모니터 가운데로 연다."""
    saved = {"geometry": "1000x700+-1500+100"}
    assert initial_geometry(saved, SCREEN, DEFAULT, MINIMUM, _nowhere) == \
        "1000x700+460+190"


def test_saved_size_is_clamped():
    tiny = {"geometry": "100x100+0+0"}
    assert initial_geometry(tiny, SCREEN, DEFAULT, MINIMUM, _anywhere) \
        .startswith("760x480+")
    huge = {"geometry": "5000x4000+0+0"}
    assert initial_geometry(huge, SCREEN, DEFAULT, MINIMUM, _anywhere) \
        .startswith("1920x1080+")


def test_corrupt_geometry_falls_back_to_default():
    saved = {"geometry": "not-a-geometry"}
    assert initial_geometry(saved, SCREEN, DEFAULT, MINIMUM, _anywhere) == \
        "900x620+510+230"
