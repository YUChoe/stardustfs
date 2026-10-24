"""창·화면 상태 지속 — 창 위치/크기, 분할선, 컬럼 폭, 마지막 폴더.

실행할 때마다 900x620 가운데·기본 컬럼 폭·루트 폴더로 돌아가면 사용자가 매번 같은
조정을 반복해야 한다. 종료·트레이 숨김·언어/테마 재구성 직전에 현재 상태를 prefs에
기록하고, 다음 실행에서 되살린다.

저장된 위치가 지금 연결된 모니터 밖이면(보조 모니터를 뗀 경우) 위치는 버리고 주
모니터 가운데에 연다 — 창이 화면 밖에 열려 "안 보임"으로 오인되던 문제를 되풀이하지
않기 위해서다.
"""

from __future__ import annotations

import logging
import os
import re
import tkinter as tk
from collections.abc import Callable

from stardustlib.gui import prefs

logger = logging.getLogger(__name__)

_GEOMETRY = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")

# 위치 검사에 쓰는 제목 표시줄 지점(창 왼쪽 위에서의 오프셋, px). 이 점이 어느
# 모니터 위에 있으면 사용자가 창을 잡아 옮길 수 있다.
_GRAB_DX, _GRAB_DY = 60, 12

# 컬럼 폭 prefs 키.
FILE_COLS_KEY = "file_cols"
MGMT_COLS_KEY = "mgmt_cols"


def parse_geometry(text: str) -> tuple[int, int, int, int] | None:
    """'WxH+X+Y'(X/Y 음수 허용)를 (w, h, x, y)로. 형식이 다르면 None."""
    match = _GEOMETRY.match(text or "")
    if not match:
        return None
    w, h, x, y = (int(v) for v in match.groups())
    return w, h, x, y


def on_any_monitor(x: int, y: int, screen: tuple[int, int]) -> bool:
    """화면 좌표 (x, y)가 연결된 모니터 위에 있는지.

    Windows는 MonitorFromPoint로 보조 모니터까지 확인한다. 그 밖의 플랫폼은 주
    모니터 범위만 본다(Tk가 가상 화면 크기를 플랫폼마다 다르게 돌려준다).
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            point = wintypes.POINT(x, y)
            # MONITOR_DEFAULTTONULL = 0 → 어느 모니터에도 없으면 NULL
            return bool(ctypes.windll.user32.MonitorFromPoint(point, 0))
        except Exception:  # noqa: BLE001 — 실패 시 주 모니터 기준으로 판정
            pass
    sw, sh = screen
    return 0 <= x < sw and 0 <= y < sh


def initial_geometry(
    saved: dict,
    screen: tuple[int, int],
    default_size: tuple[int, int],
    min_size: tuple[int, int],
    on_screen: Callable[[int, int], bool],
) -> str:
    """처음 열 창의 geometry 문자열을 정한다.

    크기는 저장값을 화면 크기와 최소 크기 사이로 맞춰 쓰고, 위치는 제목 표시줄이
    모니터 위에 있을 때만 되살린다. 그렇지 않으면 주 모니터 가운데.
    """
    sw, sh = screen
    w, h = default_size
    parsed = parse_geometry(saved.get("geometry", ""))
    if parsed:
        w, h = parsed[0], parsed[1]
    w = max(min_size[0], min(w, sw))
    h = max(min_size[1], min(h, sh))
    if parsed:
        x, y = parsed[2], parsed[3]
        if on_screen(x + min(_GRAB_DX, w // 2), y + _GRAB_DY):
            return f"{w}x{h}+{x}+{y}"
    x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
    return f"{w}x{h}+{x}+{y}"


# 첫(이름) 컬럼이 줄어들 수 있는 최소 폭(px).
_MIN_FIRST_COL = 120


def fit_first_column(tree, first: str, others) -> None:
    """트리 폭이 바뀌면 첫 컬럼이 남는 폭을 채우게 한다(가로 넘침 방지).

    Treeview는 처음 준 컬럼 폭을 창이 좁아져도 줄이지 않아, 좁은 창에서 마지막 컬럼
    헤딩이 잘린다. 나머지 컬럼 폭(사용자가 조정한 값)은 그대로 두고 이름 컬럼만
    늘이고 줄인다.
    """
    def fit(_e=None) -> None:
        try:
            width = tree.winfo_width()
            if width <= 1:
                return
            rest = sum(int(tree.column(c, "width")) for c in others)
            tree.column(first, width=max(_MIN_FIRST_COL, width - rest - 4))
        except tk.TclError:  # 파괴된 위젯
            pass

    tree.bind("<Configure>", fit, add="+")


def _column_widths(tree, columns) -> dict[str, int]:
    """Treeview 컬럼 폭(위젯이 이미 사라졌으면 빈 dict)."""
    try:
        if tree is None or not tree.winfo_exists():
            return {}
        return {c: int(tree.column(c, "width")) for c in columns}
    except tk.TclError:
        return {}


class ViewStateMixin:
    """창·분할선·컬럼 폭·마지막 폴더를 저장하고 되살린다."""

    def _restore_window(self, default_size, min_size) -> None:
        root = self.root
        screen = (root.winfo_screenwidth(), root.winfo_screenheight())
        saved = prefs.window()
        geometry = initial_geometry(
            saved, screen, default_size, min_size,
            lambda x, y: on_any_monitor(x, y, screen))
        root.geometry(geometry)
        root.minsize(*min_size)
        if saved.get("zoomed"):
            try:
                root.state("zoomed")
            except tk.TclError:  # 'zoomed'를 지원하지 않는 창 관리자
                pass

    def _save_sash_ratio(self) -> None:
        """현재 분할선 위치를 비율로 기억한다(다음 실행·재구성에서 복원)."""
        paned = getattr(self, "_paned", None)
        if paned is None:
            return
        try:
            height = paned.winfo_height()
            if height < 100:
                return
            ratio = paned.sashpos(0) / height
        except (tk.TclError, ZeroDivisionError):
            return
        if 0.2 <= ratio <= 0.9 and abs(ratio - self._sash_ratio) > 0.01:
            self._sash_ratio = ratio
            prefs.save(sash_ratio=round(ratio, 3))

    def _capture_columns(self) -> None:
        """두 트리의 현재 컬럼 폭을 인스턴스와 prefs에 기록한다."""
        files = _column_widths(getattr(self, "tree", None),
                               tuple(self._file_cols))
        mgmt = _column_widths(getattr(self, "mgmt_tree", None),
                              tuple(self._mgmt_cols))
        if files:
            self._file_cols.update(files)
        if mgmt:
            self._mgmt_cols.update(mgmt)
        prefs.save(**{FILE_COLS_KEY: self._file_cols,
                      MGMT_COLS_KEY: self._mgmt_cols})

    def _capture_view_state(self) -> None:
        """창 크기·위치, 분할선, 컬럼 폭, 마지막 폴더를 저장한다."""
        self._save_sash_ratio()
        self._capture_columns()
        values: dict = {}
        if self.config_path:
            values["last_config"] = self.config_path
            values["last_vpath"] = [self.config_path, self.vpath]
        try:
            zoomed = self.root.state() == "zoomed"
            window = dict(prefs.window())
            window["zoomed"] = zoomed
            if not zoomed and self.root.state() == "normal":
                # 최대화 상태의 geometry는 화면 크기라 되살릴 '보통 크기'가 아니다.
                window["geometry"] = self.root.geometry()
            values["window"] = window
        except tk.TclError:  # 창이 이미 파괴됨
            pass
        prefs.save(**values)
