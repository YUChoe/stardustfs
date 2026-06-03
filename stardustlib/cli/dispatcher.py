"""CLI 서브커맨드 등록 및 라우팅.

stardustfs.py main()이 argparse subparsers를 만들 때 add_subcommands()로 단발
명령을 등록하고, 파싱된 args를 run_cli()로 넘긴다.

명령은 온라인 여부로 나뉜다.
- 오프라인: 로컬 코어만으로 동작 (df/ls/status). 동기 실행.
- 온라인: 로그인·device 조회·동기화가 필요 (devices/get/put/rm/mkdir/mv/cp).
  단일 asyncio.run 안에서 setup→op→teardown 을 수행한다.
"""

from __future__ import annotations

import asyncio
import inspect
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
    "get": (commands.cmd_get, True, True),
    "put": (commands.cmd_put, True, True),
    "rm": (commands.cmd_rm, True, True),
    "mkdir": (commands.cmd_mkdir, True, True),
    "mv": (commands.cmd_mv, True, True),
    "cp": (commands.cmd_cp, True, True),
    "backup": (commands.cmd_backup, True, True),
    "restore": (commands.cmd_restore, True, True),
}

# 인증 계열: 세션 없이 config + 자격증명 저장소로 동작. (args) -> int 코루틴.
_AUTH_HANDLERS = {
    "login": commands.cmd_login,
    "logout": commands.cmd_logout,
}


def add_subcommands(subparsers, parent=None) -> None:
    """단발 CLI 서브커맨드를 argparse subparsers에 등록한다.

    parent(공통 옵션 parser)를 주면 각 서브커맨드가 --config 등을 서브커맨드 뒤에서도
    받을 수 있다 (예: `stardustfs.py ls --config X`).
    """
    kw = {"parents": [parent]} if parent is not None else {}

    # 조회 계열
    p_df = subparsers.add_parser("df", help="총/가용 용량 표시", **kw)
    p_ls = subparsers.add_parser("ls", help="가상 경로 목록", **kw)
    p_ls.add_argument("path", nargs="?", default="/", help="가상 경로 (기본 /)")
    p_status = subparsers.add_parser("status", help="동기화 상태 표시", **kw)
    p_devices = subparsers.add_parser(
        "devices", help="내 device 목록 (online 여부)", **kw
    )
    for parser in (p_df, p_ls, p_status, p_devices):
        parser.add_argument(
            "--json", action="store_true", help="JSON 형식으로 출력"
        )

    # 전송/쓰기 계열
    p_put = subparsers.add_parser("put", help="로컬 파일 업로드", **kw)
    p_put.add_argument("local", help="로컬 파일 경로")
    p_put.add_argument("remote", nargs="?", help="가상 경로 (기본: /<파일명>)")

    p_get = subparsers.add_parser("get", help="파일 다운로드", **kw)
    p_get.add_argument("remote", help="가상 경로")
    p_get.add_argument("local", nargs="?", help="로컬 저장 경로 (기본: 파일명)")

    p_rm = subparsers.add_parser("rm", help="파일/디렉토리 삭제", **kw)
    p_rm.add_argument("path", help="가상 경로")
    p_rm.add_argument(
        "-r", "--recursive", action="store_true", help="디렉토리 재귀 삭제"
    )

    p_mkdir = subparsers.add_parser("mkdir", help="디렉토리 생성", **kw)
    p_mkdir.add_argument("path", help="가상 경로")

    p_mv = subparsers.add_parser("mv", help="이동/이름변경", **kw)
    p_mv.add_argument("src", help="원본 가상 경로")
    p_mv.add_argument("dst", help="대상 가상 경로")

    p_cp = subparsers.add_parser("cp", help="파일 복사", **kw)
    p_cp.add_argument("src", help="원본 가상 경로")
    p_cp.add_argument("dst", help="대상 가상 경로")

    # 리플리케이션 계열
    p_backup = subparsers.add_parser(
        "backup", help="파일을 암호화 복제(≥3 홀더)", **kw
    )
    p_backup.add_argument("path", help="가상 경로")
    p_restore = subparsers.add_parser(
        "restore", help="복제본에서 파일 복구", **kw
    )
    p_restore.add_argument("path", help="가상 경로")

    # 인증 계열
    p_login = subparsers.add_parser("login", help="이메일/비밀번호 로그인(토큰 저장)", **kw)
    p_login.add_argument("--email", help="이메일(미지정 시 환경변수/대화형)")
    p_login.add_argument("--password", help="비밀번호(미지정 시 환경변수/대화형)")
    p_login.add_argument(
        "--key-password", dest="key_password",
        help="마스터키 백업 암호(선택, 미지정 시 저장 안 함)",
    )
    subparsers.add_parser("logout", help="토큰 취소 + 자격증명 삭제", **kw)


def is_cli_command(command: str | None) -> bool:
    """주어진 서브커맨드가 단발 CLI 명령인지 여부."""
    return command in _HANDLERS or command in _AUTH_HANDLERS


def run_cli(args) -> int:
    """단발 CLI 명령을 실행하고 종료 코드를 반환한다."""
    if not getattr(args, "config", None):
        print("오류: --config 가 필요합니다.", file=sys.stderr)
        return 2

    # 인증 계열(login/logout): 세션 없이 직접 실행
    auth_handler = _AUTH_HANDLERS.get(args.command)
    if auth_handler is not None:
        return asyncio.run(auth_handler(args))

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
    """온라인 세션으로 명령을 실행한다 (단일 asyncio.run 안).

    핸들러가 동기(devices)든 비동기(전송 계열)든 모두 지원한다.
    """
    session = await CLISession.open_online(args.config, sync=needs_sync)
    try:
        # 온라인이 필요한데 토큰이 없거나 무효라 강등된 경우
        if not session.online:
            print(
                "오류: 로그인이 필요합니다. 'stardustfs login --config ...'을 "
                "먼저 실행하세요.",
                file=sys.stderr,
            )
            return 1
        result = handler(session, args)
        if inspect.isawaitable(result):
            result = await result
        return result
    finally:
        await session.aclose()
