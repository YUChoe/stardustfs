"""파일 액션 정의 — 툴바와 컨텍스트 메뉴가 공유하는 단일 목록.

같은 동작을 두 곳에서 각각 정의하면 라벨·순서·활성 조건이 갈라진다. 정의를 한곳에
두고, 선택 상태로부터 활성 여부를 계산해 버튼과 메뉴 항목에 함께 적용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Need(Enum):
    """액션이 요구하는 선택 상태."""

    NOTHING = "nothing"    # 선택 없이도 실행 가능
    ANY = "any"            # 1개 이상 선택(파일/폴더 무관)
    ONE = "one"            # 정확히 1개 선택(파일/폴더 무관)
    ONE_FILE = "one_file"  # 정확히 1개 선택, 그것이 파일
    FILES = "files"        # 파일이 1개 이상 포함된 선택


@dataclass(frozen=True)
class Action:
    """i18n 라벨 키 + StardustApp 메서드 이름 + 활성 조건."""

    key: str
    method: str
    need: Need
    accent: bool = False


# 툴바 그룹(그룹 사이에 구분선). 주요 동작은 Accent로 강조한다.
TOOLBAR_GROUPS: tuple[tuple[Action, ...], ...] = (
    (
        Action("upload", "_upload", Need.NOTHING, accent=True),
        Action("download", "_download", Need.ONE_FILE),
    ),
    (
        Action("mkdir", "_mkdir", Need.NOTHING),
        Action("rename", "_rename", Need.ONE),
        Action("move", "_move", Need.ONE),
        Action("copy", "_copy", Need.ONE_FILE),
        Action("delete", "_delete", Need.ANY),
    ),
    (
        # 주 동작(Accent)은 업로드 하나만 둔다 — 강조가 둘이면 위계가 흐려진다.
        Action("backup_now", "_backup_selected", Need.FILES),
        Action("heal_now", "_heal_selected", Need.FILES),
        Action("restore_now", "_restore_selected", Need.FILES),
    ),
)

# 컨텍스트 메뉴 항목(None은 구분선). 선택된 행에 대한 동작만 싣는다.
CONTEXT_MENU: tuple[Action | None, ...] = (
    Action("ctx_announce", "_announce_selected", Need.FILES),
    None,
    Action("backup_now", "_backup_selected", Need.FILES),
    Action("heal_now", "_heal_selected", Need.FILES),
    Action("restore_now", "_restore_selected", Need.FILES),
    None,
    Action("download", "_download", Need.ONE_FILE),
    Action("rename", "_rename", Need.ONE),
    Action("delete", "_delete", Need.ANY),
    None,
    Action("upload_cancel", "_cancel_uploads", Need.NOTHING),
)


def tooltip(action: Action, t: dict, *, enabled: bool) -> str:
    """액션 툴팁 문구 — 동작 설명 + (비활성이면) 필요한 선택 상태.

    i18n 키는 정의에서 파생한다(`tip_{key}`, `need_{need}`). 액션을 추가하면 문구도
    같은 규칙으로 따라온다.
    """
    text = t.get(f"tip_{action.key}", t.get(action.key, action.key))
    if not enabled and action.need is not Need.NOTHING:
        hint = t.get(f"need_{action.need.value}")
        if hint:
            text = f"{text}\n{hint}"
    return text


def is_enabled(action: Action, rows: list[dict], *, has_config: bool) -> bool:
    """선택된 행 목록으로 액션 활성 여부를 판정한다.

    설정 파일이 없으면 어떤 동작도 할 수 없으므로 전부 비활성이다.
    """
    if not has_config:
        return False
    if action.need is Need.NOTHING:
        return True
    if not rows:
        return False
    if action.need is Need.ANY:
        return True
    if action.need is Need.ONE:
        return len(rows) == 1
    if action.need is Need.ONE_FILE:
        return len(rows) == 1 and rows[0].get("type") == "file"
    if action.need is Need.FILES:
        return any(r.get("type") == "file" for r in rows)
    return False
