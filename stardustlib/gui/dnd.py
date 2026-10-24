"""파일 끌어다 놓기(drag & drop) — 선택 의존 tkinterdnd2.

tkinter에는 OS 파일 드롭이 없다. tkinterdnd2(tkdnd 확장)가 있으면 Tk 루트에 확장을
불러와 위젯을 드롭 대상으로 등록하고, 없거나 불러오지 못하면 조용히 비활성으로 둔다
(업로드 버튼 경로는 그대로 동작한다).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_enabled: dict[int, bool] = {}


def enable(root) -> bool:
    """루트 인터프리터에 tkdnd를 불러온다. 성공하면 True(루트당 한 번만 시도)."""
    key = id(root)
    if key in _enabled:
        return _enabled[key]
    try:
        from tkinterdnd2 import TkinterDnD

        TkinterDnD.require(root)
        _enabled[key] = True
    except Exception as e:  # noqa: BLE001 — 미설치·바이너리 없음 → 드롭만 끈다
        logger.info("파일 끌어다 놓기 비활성: %s", e)
        _enabled[key] = False
    return _enabled[key]


def register(widget, on_paths: Callable[[list[str]], None]) -> bool:
    """widget을 파일 드롭 대상으로 등록한다. 등록하면 True."""
    if not enable(widget.winfo_toplevel()):
        return False
    try:
        from tkinterdnd2 import COPY, DND_FILES

        def _drop(event):
            # event.data는 Tcl 리스트 문자열이다(공백 있는 경로는 {}로 감싸짐).
            on_paths(list(widget.tk.splitlist(event.data)))
            return COPY

        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", _drop)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("드롭 대상 등록 실패: %s", e)
        return False
