"""CLI 서브커맨드 등록 및 라우팅.

stardustfs.py main()이 argparse subparsers를 만들 때 add_subcommands()로 단발
명령을 등록하고, 파싱된 args를 run_cli()로 넘긴다.
"""

from __future__ import annotations

import sys

from stardustlib.cli import commands
from stardustlib.cli.session import CLISession

# 명령 이름 → 핸들러. 새 명령은 여기와 add_subcommands()에 함께 추가한다.
_HANDLERS = {
    "df": commands.cmd_df,
    "ls": commands.cmd_ls,
}


def add_subcommands(subparsers) -> None:
    """단발 CLI 서브커맨드를 argparse subparsers에 등록한다."""
    p_df = subparsers.add_parser("df", help="총/가용 용량 표시")

    p_ls = subparsers.add_parser("ls", help="가상 경로 목록")
    p_ls.add_argument("path", nargs="?", default="/", help="가상 경로 (기본 /)")

    # 공통 옵션
    for parser in (p_df, p_ls):
        parser.add_argument(
            "--json", action="store_true", help="JSON 형식으로 출력"
        )


def is_cli_command(command: str | None) -> bool:
    """주어진 서브커맨드가 단발 CLI 명령인지 여부."""
    return command in _HANDLERS


def run_cli(args) -> int:
    """단발 CLI 명령을 실행하고 종료 코드를 반환한다."""
    if not getattr(args, "config", None):
        print("오류: --config 가 필요합니다.", file=sys.stderr)
        return 2

    handler = _HANDLERS.get(args.command)
    if handler is None:
        print(f"오류: 알 수 없는 명령입니다: {args.command}", file=sys.stderr)
        return 2

    session = CLISession.open(args.config)
    try:
        return handler(session, args)
    finally:
        session.close()
