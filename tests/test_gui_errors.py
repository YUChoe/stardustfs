"""작업 실패 → 사용자 문구 변환, 업로드 이름 충돌 시 새 이름 고르기, 폭 맞춤 줄임."""

from __future__ import annotations

import pytest

from stardustlib.exceptions import AuthenticationError, InsufficientStorageError
from stardustlib.gui import errors, i18n
from stardustlib.gui.act_auth import ServerNotConfigured
from stardustlib.gui.act_core import LoginRequired, RemotePathExists
from stardustlib.gui.act_files import free_name
from stardustlib.gui.format import fit_text

EN = i18n.get_text("en")


@pytest.mark.parametrize("exc, key", [
    (LoginRequired(), "err_login"),
    (ServerNotConfigured(), "err_no_server"),
    (AuthenticationError("Invalid credentials"), "err_auth"),
    (AuthenticationError("Cannot connect to server: x"), "err_network"),
    (AuthenticationError("Server error: 503"), "err_server"),
    (InsufficientStorageError("full"), "err_no_space"),
    (TimeoutError(), "err_timeout"),
    (ConnectionRefusedError(), "err_network"),
])
def test_known_errors_are_translated(exc, key):
    assert errors.friendly(exc, EN) == EN[key]


def test_path_errors_name_the_target():
    assert errors.friendly(FileNotFoundError(2, "x", "/a/b.txt"), EN) == \
        EN["err_not_found"].format(msg="/a/b.txt")
    assert errors.friendly(RemotePathExists("/a/b.txt"), EN) == \
        EN["err_exists"].format(msg="/a/b.txt")


def test_backend_korean_message_is_not_shown_to_english_users():
    """백엔드의 한국어 로그인 안내가 영어 UI에 그대로 섞이지 않는다."""
    text = errors.friendly(LoginRequired(), EN)
    assert "로그인" not in text


def test_httpx_errors_count_as_network():
    httpx = pytest.importorskip("httpx")
    assert errors.friendly(httpx.ConnectError("refused"), EN) == EN["err_network"]


def test_unknown_error_keeps_original_text():
    text = errors.friendly(ValueError("odd"), EN)
    assert text == EN["err_unexpected"].format(msg="odd")
    # 메시지가 없으면 예외 이름이라도 보인다
    assert "KeyError" in errors.friendly(KeyError(), EN)


def test_describe_passes_plain_strings_through():
    assert errors.describe("그대로", EN) == "그대로"


# --- 이름 충돌 ---

def test_free_name_adds_number_before_extension():
    taken = {"/a/b.txt", "/a/b (1).txt"}
    assert free_name(taken.__contains__, "/a/c.txt") == "/a/c.txt"
    assert free_name(taken.__contains__, "/a/b.txt") == "/a/b (2).txt"


def test_free_name_without_extension():
    taken = {"/README", "/.bashrc"}
    assert free_name(taken.__contains__, "/README") == "/README (1)"
    assert free_name(taken.__contains__, "/.bashrc") == "/.bashrc (1)"


def test_put_file_rejects_unknown_conflict_mode(tmp_path):
    from stardustlib.gui import actions

    with pytest.raises(ValueError):
        actions.put_file(str(tmp_path / "c.json"), "x", "/x", on_conflict="merge")


# --- 폭 맞춤 ---

def test_fit_text_keeps_short_text():
    assert fit_text("abc", 10, len) == "abc"


def test_fit_text_ellipsises_to_width():
    out = fit_text("abcdefghij", 5, len)
    assert out == "abcd…" and len(out) <= 5


def test_fit_text_degenerate_width():
    assert fit_text("abcdef", 1, len) == "…"
    assert fit_text("abcdef", 0, len) == "abcdef"  # 폭을 모르면 줄이지 않는다
