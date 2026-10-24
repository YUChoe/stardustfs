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
        t["storage_status_tip"].format(sources=1, used="1", total="2")
        t["download_done"].format(path="x", size="1 B")


def test_tray_available_is_bool():
    assert isinstance(tray.available(), bool)


# 사용자에게 보이는 영어는 영국 영어 철자를 쓴다(대상 사용자가 영국).
_AMERICAN = (
    "initializ", "minimiz", "maximiz", "organiz", "recogniz", "synchroniz",
    "authoriz", "customiz", "optimiz", "prioritiz", "finaliz", "normaliz",
    "color", "center", "canceled", "canceling", "favorite", "behavior",
    "catalog", "analyz",
)


def test_english_uses_british_spelling():
    en = i18n.get_text("en")
    for key, text in en.items():
        lower = text.lower()
        for word in _AMERICAN:
            assert word not in lower, f"{key}: '{word}' — {text}"


# 키가 코드에서 조합되는 접두사(문자열 리터럴로 나타나지 않는다).
_DYNAMIC_PREFIXES = ("tip_", "need_", "src_kind_")


def test_every_key_is_used():
    """번역 키가 GUI 코드 어딘가에서 쓰인다(예전 화면의 잔재가 남지 않도록)."""
    import pathlib
    import re

    gui = pathlib.Path(i18n.__file__).parent
    source = "\n".join(
        p.read_text(encoding="utf-8") for p in gui.rglob("*.py")
        if p.name != "i18n.py"
    )
    literals = set(re.findall(r"[\"']([a-z0-9_]+)[\"']", source))
    unused = [
        k for k in i18n.get_text("ko")
        if k not in literals and not k.startswith(_DYNAMIC_PREFIXES)
    ]
    assert unused == []
