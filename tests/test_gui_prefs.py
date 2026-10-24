"""GUI 표시 설정(테마·언어) 지속 — 저장·복원과 손상 내성.

설정 디렉토리는 LOCALAPPDATA 기준이므로 임시 경로로 바꿔 실제 사용자 설정을 건드리지
않는다.
"""

from __future__ import annotations

import os

from stardustlib.gui import prefs


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert prefs.load() == {}          # 아직 파일 없음
    assert prefs.theme("dark") == "dark"

    prefs.save(theme="light")
    assert prefs.theme("dark") == "light"

    # 다른 키를 저장해도 기존 값은 남는다
    prefs.save(lang="en")
    assert prefs.theme("dark") == "light"
    assert prefs.lang("ko") == "en"


def test_unsupported_values_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    prefs.save(theme="neon", lang="fr")
    assert prefs.theme("dark") == "dark"   # 미지원 테마 → 기본값
    assert prefs.lang("ko") == "ko"        # 미지원 언어 → 기본값


def test_corrupt_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    prefs.save(theme="light")             # 경로 생성
    with open(prefs._path(), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert prefs.load() == {}             # 예외 없이 기본값
    assert prefs.theme("dark") == "dark"


def test_save_failure_does_not_raise(tmp_path, monkeypatch):
    """설정을 못 써도 GUI는 계속 동작해야 한다(경로를 디렉토리로 막아 실패 유발)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    os.makedirs(prefs._path(), exist_ok=True)  # 파일 자리에 디렉토리
    prefs.save(theme="light")                  # 예외 없이 통과
    assert prefs.theme("dark") == "dark"


def test_widths_validate_each_column(tmp_path, monkeypatch):
    """컬럼마다 따로 검증해 이상한 값만 기본값으로 둔다."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    defaults = {"name": 440, "size": 110}
    assert prefs.widths("cols", defaults) == defaults
    prefs.save(cols={"name": 300, "size": -5, "extra": 99})
    assert prefs.widths("cols", defaults) == {"name": 300, "size": 110}
    prefs.save(cols="broken")
    assert prefs.widths("cols", defaults) == defaults


def test_last_vpath_belongs_to_its_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert prefs.last_vpath("a.json") == "/"
    prefs.save(last_vpath=["a.json", "/movies"])
    assert prefs.last_vpath("a.json") == "/movies"
    assert prefs.last_vpath("b.json") == "/"     # 다른 설정이면 루트
    assert prefs.last_vpath(None) == "/"
    prefs.save(last_vpath=["a.json", "relative"])
    assert prefs.last_vpath("a.json") == "/"     # 형식이 다르면 루트


def test_window_and_text(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert prefs.window() == {}
    assert prefs.text("last_config") == ""
    prefs.save(window={"geometry": "900x620+1+2", "zoomed": True, "x": 1},
               last_config="C:/cfg.json")
    assert prefs.window() == {"geometry": "900x620+1+2", "zoomed": True}
    assert prefs.text("last_config") == "C:/cfg.json"
    prefs.save(window=[1, 2], last_config=5)
    assert prefs.window() == {}
    assert prefs.text("last_config", "d") == "d"
