"""CLI 가상 경로 정규화(_vpath) 단위 테스트."""

import pytest

from stardustlib.cli.commands import _vpath


@pytest.mark.parametrize("raw, expected", [
    (None, "/"),
    ("", "/"),
    (".", "/"),
    ("./", "/"),
    ("/", "/"),
    ("foo.txt", "/foo.txt"),          # 상대 경로 → 선행 슬래시(MSYS 회피)
    ("a/b/c.txt", "/a/b/c.txt"),
    ("/already/abs", "/already/abs"),
    ("dir/", "/dir"),                  # 뒤 슬래시 제거
    ("a\\b\\c", "/a/b/c"),             # 백슬래시 → 슬래시
    ("//x//y//", "/x/y"),              # 중복 슬래시 정리
    ("  /sp  ", "/sp"),                # 공백 trim
])
def test_vpath_normalisation(raw, expected):
    assert _vpath(raw) == expected
