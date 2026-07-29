"""GUI 표시용 값 포매팅.

app.py와 upload_dialog.py에 같은 함수가 중복돼 있던 것을 한곳으로 모았다.
"""

from __future__ import annotations


def human_bytes(n: int) -> str:
    """바이트 수를 사람이 읽는 단위로 만든다(1024 기준, B는 소수점 없음)."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
