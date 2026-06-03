"""CLI 서브커맨드 등록 및 라우팅.

stardustfs.py main()이 argparse subparsers를 만들 때 add_subcommands()로 단발
명령을 등록하고, 파싱된 args를 run_cli()로 넘긴다.

명령은 온라인 여부로 나뉜다.
- 오프라인: 로컬 코어만으로 동작 (df/ls/status). 동기 실행.
- 온라인: 로그인·device 조회가 필요 (devices). 단일 asyncio.run 안에서 실행.
"""

from __future__ import annotations

import asyncio
import sys

from stardustlib.cli import commands
from stardustlib.cli.session import CLISession

# 명령 이름 → (핸들러, online 필요, 동기화 필요).
# 새 명령은 여기와 add_subcommands()에 함께 추가한다.
_HANDLERS = {
    "df": (commands.cmd_df, False, False),
    "ls": (commands.cmd_ls, False, False),
    "status": (commands.cmd_status, False, False),
    "devices": (commands.cmd_devices, True, False),
}


def add_subcommands(subparsers) -> None:
    """단발 CLI 서브커맨드를 argparse subparsers에 등록한다."""
    p_df = subparsers.add_parser("df", help="총/가용 용량 표시")
    p_ls = subparsers.add_parser("ls", help="가상 경로 목록")
    p_ls.add_argument("path", nargs="?", default="/", help="가상 경로 (기본 /)")
    p_status = subparsers.add_parser("status", help="동기화 상태 표시")
    p_devices = subparsers.add_parser(
        "devices", help="내 device 목록 (online 여부)"
    )

    for parser in (p_df, p_ls, p_status, p_devices):
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

    entry = _HANDLERS.get(args.command)
    if entry is None:
        print(f"오류: 알 수 없는 명령입니다: {args.command}", file=sys.stderr)
        return 2

    handler, needs_online, needs_sync = entry
    if needs_online:
        return asyncio.run(_run_online(handler, args, needs_sync))
    return _run_offline(handler, args)


def _run_offline(handler, args) -> int:
    """오프라인 세션으로 명령을 실행한다."""
    session = CLISession.open(args.config)
    try:
        return handler(session, args)
    finally:
        session.close()


async def _run_online(handler, args, needs_sync: bool) -> int:
    """온라인 세션으로 명령을 실행한다 (단일 asyncio.run 안)."""
    session = await CLISession.open_online(args.config, sync=needs_sync)
    try:
        return handler(session, args)
    finally:
        await session.aclose()
