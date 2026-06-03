"""GUI i18n / tray 헬퍼 단위 테스트 (Tk 비의존)."""

from stardustlib.gui import i18n, tray


def test_key_parity():
    """ko/en 키 집합이 동일해야 한다(누락 번역 방지)."""
    ko = i18n.get_text("ko")
    en = i18n.get_text("en")
    assert set(ko) == set(en)


def test_get_text_fallback():
    assert i18n.get_text("zz") is i18n.get_text(i18n.DEFAULT_LANG)


def test_detect_lang_supported():
    assert i18n.detect_lang() in i18n.SUPPORTED_LANGS


def test_format_placeholders():
    # 동적 문자열 포맷이 양 언어에서 동작
    for lang in ("ko", "en"):
        t = i18n.get_text(lang)
        assert "pid=" in t["daemon_running"].format(pid=123) or "123" in \
            t["daemon_running"].format(pid=123)
        t["cap"].format(used="1", total="2", avail="3", pending=0)
        t["download_done"].format(path="x", size="1 B")


def test_tray_available_is_bool():
    assert isinstance(tray.available(), bool)
