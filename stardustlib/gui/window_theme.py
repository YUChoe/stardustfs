"""창 외형 적용 — 테마 전환, 본문 폰트, Windows 제목 표시줄 색.

팔레트 값 자체는 theme.py(디자인 시스템 미러)가 갖고, 이 믹스인은 그것을 살아 있는
창에 입히는 절차를 담는다. 선택한 테마는 prefs에 저장해 다음 실행에도 유지한다.
"""

from __future__ import annotations

import logging
import os
from tkinter import ttk

from stardustlib.gui import prefs, theme

try:  # 현대적 플랫 테마(Windows 11 풍). 미설치 시 기본 ttk로 폴백.
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None

logger = logging.getLogger(__name__)


class WindowThemeMixin:
    """테마·폰트·제목 표시줄 적용."""

    def _apply_theme(self) -> None:
        """현대적 테마(sv-ttk) + 본문 폰트를 적용한다."""
        if sv_ttk is not None:
            try:
                sv_ttk.set_theme(self.theme)
            except Exception:  # noqa: BLE001
                pass
        self._apply_font()
        # 디자인 시스템(Primer) 팔레트를 sv_ttk 위에 재지정한다.
        try:
            theme.apply_palette(self.root, ttk.Style(), dark=self.theme == "dark")
        except Exception:  # noqa: BLE001
            pass
        self._apply_titlebar()

    def _apply_font(self) -> None:
        """본문 폰트를 통일하고 리스트 행 높이를 넉넉히 한다."""
        try:
            import tkinter.font as tkfont

            family = theme.ui_font_family()
            for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                          "TkHeadingFont", "TkFixedFont"):
                try:
                    tkfont.nametofont(fname).configure(family=family)
                except Exception:  # noqa: BLE001
                    pass
            fnt = (family, 10)
            style = ttk.Style()
            style.configure(".", font=fnt)
            # sv-ttk가 위젯 스타일별로 자체 폰트를 지정하므로 각 스타일에 직접 적용.
            for st in ("TLabel", "TButton", "Accent.TButton", "Cta.TButton",
                       "TEntry", "TMenubutton", "TCheckbutton", "TRadiobutton",
                       "Toolbutton", "TLabelframe.Label", "TNotebook.Tab",
                       "Treeview", "Treeview.Heading"):
                try:
                    style.configure(st, font=fnt)
                except Exception:  # noqa: BLE001
                    pass
            style.configure("Treeview", rowheight=28)
        except Exception:  # noqa: BLE001
            pass

    def _apply_titlebar(self) -> None:
        """Windows 11에서 제목 표시줄 색을 테마에 맞춘다(기본 강조색/주황 제거)."""
        if os.name != "nt":
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dark = self.theme == "dark"
            flag = ctypes.c_int(1 if dark else 0)
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(flag), ctypes.sizeof(flag)
            )
            # DWMWA_CAPTION_COLOR = 35 (Win11 22000+), COLORREF 0x00BBGGRR.
            # 다크는 Primer 캔버스(#0d1117 → BGR 0x171B0D).
            color = ctypes.c_int(0x00171B0D if dark else 0x00FAFAFA)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 35, ctypes.byref(color), ctypes.sizeof(color)
            )
        except Exception:  # noqa: BLE001 — 구버전 Windows 등은 무시
            pass

    def _set_theme(self, name: str) -> None:
        self.theme = name
        prefs.save(theme=name)  # 다음 실행에도 유지
        self._apply_theme()
        # 메뉴바 드롭다운(tk.Menu) 색은 테마를 자동으로 따르지 않으므로 재구성한다.
        self._build_menu()
        # 컨텍스트 메뉴도 tk.Menu라 색을 다시 입혀야 한다(본문 재구성 없이).
        self._build_context_menu()
        # 목록 행 태그 색은 다크/라이트가 다르다(다크용 밝은 회색을 라이트 배경에
        # 그대로 쓰면 글자가 묻는다).
        self._apply_row_tags()
        self._apply_mgmt_tags()
