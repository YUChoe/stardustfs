"""GUI 표시용 값 포매팅.

여러 화면에 같은 함수가 중복돼 있던 것을 한곳으로 모았다.
"""

from __future__ import annotations


def shorten(text: str, limit: int = 20) -> str:
    """상태바 한 줄에 들어가도록 가운데를 줄인다(확장자는 남긴다).

    긴 파일명을 그대로 쓰면 좁은 창에서 옆 항목을 밀어내 잘리게 만든다.
    """
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return f"{text[:head]}…{text[-tail:]}"


def human_bytes(n: int) -> str:
    """바이트 수를 사람이 읽는 단위로 만든다(1024 기준, B는 소수점 없음)."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
