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


def _probe_error() -> str | None:
    """pystray/PIL import를 시도하고 실패 사유를 반환한다(성공이면 None)."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


def available() -> bool:
    """트레이 사용 가능 여부(pystray + PIL 설치)."""
    return _probe_error() is None


def reason() -> str | None:
    """트레이를 쓸 수 없는 사유 문자열(사용 가능하면 None)."""
    return _probe_error()


def _make_image():
    # 브랜드 다이아몬드 마크(theme._mark_image)를 트레이 아이콘으로 재사용한다.
    from stardustlib.gui import theme

    img = theme._mark_image(64)
    if img is not None:
        return img
    # 폴백: 마크 생성 실패 시 브랜드 블루 원.
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=(88, 166, 255, 255))
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
