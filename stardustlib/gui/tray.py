"""시스템 트레이 헬퍼 (선택 의존: pystray + Pillow).

Tkinter는 트레이를 지원하지 않으므로 pystray를 사용한다. 둘 다 prebuilt wheel이라
C 컴파일러가 필요 없다. 미설치 환경에서는 build_icon이 None을 반환하고, 호출 측은
트레이 없이(창 닫기=종료) 동작하도록 폴백한다.

메뉴 라벨은 호출 가능(callable)으로 받아 현재 언어를 반영한다(언어 전환 시 재생성
불필요). 메뉴 콜백은 pystray 스레드에서 실행되므로 호출 측이 Tk 메인 스레드로
마샬링해야 한다(root.after).
"""

from __future__ import annotations

from collections.abc import Callable


def available() -> bool:
    """트레이 사용 가능 여부(pystray + PIL 설치)."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _make_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=(74, 123, 255, 255))
    try:
        draw.text((25, 19), "S", fill=(255, 255, 255, 255))
    except Exception:  # noqa: BLE001 — 폰트 없으면 원만 표시
        pass
    return img


def build_icon(
    title: str,
    open_label: Callable[[], str],
    quit_label: Callable[[], str],
    on_open: Callable[[], None],
    on_quit: Callable[[], None],
):
    """pystray Icon을 만들어 반환한다. 사용 불가하면 None.

    open_label/quit_label은 현재 라벨 문자열을 반환하는 callable이다.
    """
    try:
        import pystray
    except Exception:  # noqa: BLE001
        return None

    menu = pystray.Menu(
        pystray.MenuItem(
            lambda _item: open_label(), lambda _icon, _item: on_open(),
            default=True,
        ),
        pystray.MenuItem(
            lambda _item: quit_label(), lambda _icon, _item: on_quit()
        ),
    )
    return pystray.Icon("stardustfs", _make_image(), title, menu)
