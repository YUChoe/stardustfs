"""GUI 사용자 설정(테마·언어) 지속.

config가 아니라 사용자 단위다 — GUI는 config 없이도 뜨고 실행 중에 config를 고를 수
있으므로, 표시 설정을 config에 묶으면 설정을 바꿀 때마다 테마가 되돌아간다.
읽기/쓰기 실패는 기본값으로 진행한다(표시 설정 때문에 GUI가 뜨지 못하면 안 된다).
"""

from __future__ import annotations

import json
import logging
import os

from stardustlib.gui.i18n import TRANSLATIONS
from stardustlib.single_instance import user_state_dir

logger = logging.getLogger(__name__)

_THEMES = ("dark", "light")


def _path() -> str:
    return os.path.join(user_state_dir(), "gui-prefs.json")


def load() -> dict:
    """저장된 설정을 읽는다. 없거나 손상됐으면 빈 dict."""
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("GUI 설정을 읽지 못했습니다(기본값 사용): %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def save(**values) -> None:
    """주어진 키만 갱신해 저장한다(기존 값 보존)."""
    data = load()
    data.update(values)
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning("GUI 설정을 저장하지 못했습니다: %s", e)


def theme(default: str = "dark") -> str:
    """저장된 테마(미지원 값이면 default)."""
    value = load().get("theme")
    return value if value in _THEMES else default


def lang(default: str) -> str:
    """저장된 언어(미지원 값이면 default — 보통 로케일 추정값)."""
    value = load().get("lang")
    return value if value in TRANSLATIONS else default


def flag(key: str, default: bool = False) -> bool:
    """저장된 참/거짓 설정(관리 패널 접힘, 트레이 안내 완료 등)."""
    value = load().get(key)
    return bool(value) if isinstance(value, bool) else default


def sort(default: tuple[str, bool] = ("name", False)) -> tuple[str, bool]:
    """저장된 목록 정렬 상태 (컬럼, 내림차순). 형식이 다르면 default."""
    value = load().get("sort")
    if (isinstance(value, list) and len(value) == 2
            and value[0] in ("name", "size", "backup")):
        return (str(value[0]), bool(value[1]))
    return default


def ratio(key: str, default: float, *, low: float = 0.2,
          high: float = 0.9) -> float:
    """저장된 비율(분할선 위치 등). 범위를 벗어나면 default."""
    value = load().get(key)
    if isinstance(value, (int, float)) and low <= float(value) <= high:
        return float(value)
    return default
