"""CLI 명령 구현.

각 명령은 (session, args) -> int(종료 코드) 시그니처를 따른다.
Phase 1: 로컬 코어만으로 동작하는 df/ls. 전송 계열(put/get) 등은 후속 Phase.
"""

from __future__ import annotations

from stardustlib.cli.format import print_json, print_table


def cmd_df(session, args) -> int:
    """총/사용/가용 용량을 출력한다 (로컬 소스 합산)."""
    total = session.jbod.get_total_space()
    available = session.jbod.get_available_space()
    used = total - available

    if args.json:
        print_json({"total": total, "used": used, "available": available})
    else:
        print_table(
            [[total, used, available]], ["total", "used", "available"]
        )
    return 0


def cmd_ls(session, args) -> int:
    """가상 경로의 디렉토리 목록을 출력한다."""
    entries = session.jbod.list_directory(args.path)

    if args.json:
        print_json(
            [
                {
                    "name": e.name,
                    "is_directory": e.is_directory,
                    "file_size": e.file_size,
                    "modified_at": e.modified_at,
                }
                for e in entries
            ]
        )
    else:
        rows = [
            ["d" if e.is_directory else "-", e.file_size, e.name]
            for e in entries
        ]
        print_table(rows, ["type", "size", "name"])
    return 0
