"""CLI 출력 포매터.

Windows 콘솔(cp949)에서도 한국어/파일명이 깨지지 않도록 UTF-8 바이트로 출력한다.
"""

from __future__ import annotations

import json as _json
import sys


def _write(text: str) -> None:
    """UTF-8로 표준출력에 기록한다 (콘솔 인코딩 무관)."""
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def echo(text: str = "") -> None:
    """한 줄을 UTF-8로 표준출력에 기록한다 (Windows cp949 콘솔 인코딩 회피).

    print()는 콘솔 코드페이지로 인코딩하므로 em-dash 등 비-cp949 문자에서
    UnicodeEncodeError가 발생한다. CLI 메시지는 모두 이 함수를 사용한다.
    """
    _write(text + "\n")


def print_json(obj) -> None:
    """객체를 들여쓰기된 JSON으로 출력한다."""
    _write(_json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def print_table(rows: list[list], headers: list[str]) -> None:
    """간단한 좌측 정렬 표를 출력한다. 행이 없으면 헤더만 출력한다."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt(cells) -> str:
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    _write(fmt(headers) + "\n")
    for row in rows:
        _write(fmt(row) + "\n")
