"""StardustFS GUI 테마 — 디자인 시스템(GitHub Primer 다크) 팔레트.

서버 저장소의 .design_system_v1/tokens/colors.css 값을 코드 상수로 미러링하고,
sv_ttk 위에 Primer 색을 재지정한다. 브랜드 마크(다이아몬드 + 스파클)를 그려 창
아이콘으로 설정한다(로고는 텍스트 워드마크가 정식이며, 마크는 보조 제안).

ttk는 테마(sv_ttk)가 일부 색을 엘리먼트 이미지로 그려 background 지정이 완전히
먹지 않을 수 있다. 안정적으로 먹는 표면(캔버스/Treeview/Label/Frame)과 tk 위젯
(Listbox/Text)에 Primer를 적용하고, Accent 버튼 녹색은 best-effort로 지정한다.
"""

from __future__ import annotations

import tkinter as tk

# .design_system_v1/tokens/colors.css 미러(단일 소스)
PALETTE: dict[str, str] = {
    "canvas": "#0d1117",
    "surface": "#161b22",
    "surface_hover": "#1c222b",
    "border_muted": "#21262d",
    "border_default": "#30363d",
    "fg_strong": "#f0f6fc",
    "fg_default": "#e0e0e0",
    "fg_muted": "#c9d1d9",
    "fg_subtle": "#8b949e",
    "fg_faint": "#6e7681",
    "fg_disabled": "#484f58",
    "accent": "#58a6ff",
    "accent_hover": "#79b8ff",
    "success_emphasis": "#238636",   # 기본 CTA 녹색
    "success_emphasis_hover": "#2ea043",
    "success": "#3fb950",            # 성공 텍스트/체크
    "success_subtle": "#1f3d2a",
    "danger": "#f85149",
    "danger_border": "#6e3030",
    "danger_subtle": "#3d1f1f",
}


def apply_palette(root: tk.Misc, style, *, dark: bool) -> None:
    """Primer 팔레트를 적용한다. dark일 때 표면색까지 칠하고, 브랜드 강조(녹색 CTA,
    파란 선택)는 항상 적용한다(light는 sv_ttk 밝은 표면 유지)."""
    p = PALETTE
    if dark:
        root.configure(bg=p["canvas"])
        style.configure(".", background=p["canvas"], foreground=p["fg_default"])
        style.configure("TFrame", background=p["canvas"])
        style.configure("TLabel", background=p["canvas"], foreground=p["fg_default"])
        style.configure("TLabelframe", background=p["canvas"],
                        bordercolor=p["border_muted"])
        style.configure("TLabelframe.Label", background=p["canvas"],
                        foreground=p["fg_subtle"])
        style.configure("TSeparator", background=p["border_muted"])
        style.configure("Treeview", background=p["surface"],
                        fieldbackground=p["surface"], foreground=p["fg_default"],
                        bordercolor=p["border_muted"])
        style.configure("Treeview.Heading", background=p["surface"],
                        foreground=p["fg_subtle"])
    # 브랜드 강조(테마 무관)
    style.map("Treeview",
              background=[("selected", p["accent"])],
              foreground=[("selected", "#ffffff")])
    style.configure("Accent.TButton", background=p["success_emphasis"])
    style.map("Accent.TButton",
              background=[("active", p["success_emphasis_hover"]),
                          ("pressed", p["success_emphasis_hover"])])


def style_listbox(listbox: tk.Listbox) -> None:
    """tk.Listbox를 Primer 표면색으로 칠한다(ttk가 아니라 직접 제어)."""
    p = PALETTE
    listbox.configure(
        background=p["surface"], foreground=p["fg_default"],
        selectbackground=p["accent"], selectforeground="#ffffff",
        highlightthickness=1, highlightbackground=p["border_muted"],
        highlightcolor=p["border_default"], borderwidth=0,
    )


def style_text(text: tk.Text) -> None:
    """tk.Text(상태 로그 등)를 Primer 캔버스색으로 칠한다."""
    p = PALETTE
    text.configure(
        background=p["canvas"], foreground=p["fg_default"],
        insertbackground=p["fg_default"],
        highlightthickness=1, highlightbackground=p["border_muted"],
        highlightcolor=p["border_default"], borderwidth=0,
    )


def _mark_image(size: int = 64):
    """브랜드 다이아몬드 마크를 그린 RGBA 이미지를 반환한다(Pillow). 실패 시 None."""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 — Pillow 미설치
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size / 2
    r = size * 0.44
    blue = (88, 166, 255, 255)
    blue_soft = (88, 166, 255, 150)
    blue2 = (121, 184, 255, 255)
    lite = (207, 227, 255, 255)
    pts = [(c, c - r), (c + r, c), (c, c + r), (c - r, c)]
    d.line(pts + [pts[0]], fill=blue_soft, width=max(2, size // 28))
    dot = max(2, size * 0.045)
    for (x, y) in pts:
        d.ellipse([x - dot, y - dot, x + dot, y + dot], fill=blue)
    s = size * 0.26
    star = [
        (c, c - s), (c + s * 0.3, c - s * 0.3), (c + s, c), (c + s * 0.3, c + s * 0.3),
        (c, c + s), (c - s * 0.3, c + s * 0.3), (c - s, c), (c - s * 0.3, c - s * 0.3),
    ]
    d.polygon(star, fill=blue2)
    d.ellipse([c - dot, c - dot, c + dot, c + dot], fill=lite)
    return img


def set_window_icon(root: tk.Misc):
    """브랜드 마크를 창 아이콘으로 설정하고 PhotoImage 참조를 반환한다(GC 방지용으로
    호출자가 보관). Pillow/ImageTk 미설치면 None."""
    img = _mark_image(64)
    if img is None:
        return None
    try:
        from PIL import ImageTk

        photo = ImageTk.PhotoImage(img)
        root.iconphoto(True, photo)
        return photo
    except Exception:  # noqa: BLE001
        return None
