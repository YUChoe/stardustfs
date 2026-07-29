"""GUI 백엔드 — 파일 전송과 조작(업로드/다운로드/폴더/삭제/이동/복사).

데몬이 실행 중이면 전송을 데몬에 위임한다(홀펀칭·리모트 스필오버 활용). 미실행이면
온라인 세션을 직접 열어 수행하고 메타데이터를 서버에 전파한다.
"""

from __future__ import annotations

import asyncio
import os

from stardustlib.cli.commands import _vpath
from stardustlib.config_loader import ConfigLoader
from stardustlib.gui.act_core import (
    RemotePathExists,
    _offline_session,
    _run_online,
)


def _delegate(config_path: str, op: str, virtual_path: str, local_path: str):
    """데몬이 실행 중이면 전송을 데몬에 위임한다(홀펀칭 활용). 반환 dict 또는 None.

    데몬 미실행/제어 채널 부재면 None을 반환해 호출자가 직접 수행하게 한다.
    """
    from stardustlib import daemon_control

    config = ConfigLoader(config_path).load()
    db = config.get("metadata_db")
    if not db:
        return None
    return daemon_control.transfer_via_daemon(
        db, op, virtual_path, os.path.abspath(local_path)
    )


def put_file(config_path: str, local: str, remote: str) -> int:
    rv = _vpath(remote)
    # 같은 가상 경로가 이미 있으면 덮어쓰지 않고 알린다(WAL이라 데몬이 커밋한 최신
    # 메타데이터도 읽힌다). 호출자(업로드 다이얼로그)가 RemotePathExists를 처리한다.
    if _offline_session(config_path).metadata.lookup(rv) is not None:
        raise RemotePathExists(rv)
    # 데몬 위임 우선(로컬 만석 시 홀펀칭 리모트 스필오버). 미실행이면 직접 수행.
    res = _delegate(config_path, "put", rv, local)
    if res is not None:
        return res.get("bytes", 0)

    with open(local, "rb") as f:
        data = f.read()

    async def aop(s):
        s.storage_pool.write_file(rv, data)
        await s.upload_if_online()
        return len(data)

    return asyncio.run(_run_online(config_path, aop, sync=True))


def get_file(config_path: str, remote: str, local: str) -> int:
    rv = _vpath(remote)
    res = _delegate(config_path, "get", rv, local)
    if res is not None:
        return res.get("bytes", 0)

    async def aop(s):
        return s.storage_pool.read_file(rv)

    data = asyncio.run(_run_online(config_path, aop, sync=True))
    with open(local, "wb") as f:
        f.write(data)
    return len(data)


def mkdir(config_path: str, path: str) -> None:
    rv = _vpath(path)

    async def aop(s):
        s.storage_pool.create_directory(rv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))


def remove_many(config_path: str, items: list[tuple[str, bool]]) -> int:
    """여러 경로를 한 번의 온라인 세션에서 삭제하고 1회만 서버에 전파한다.

    items: (가상경로, recursive) 목록. 삭제 성공 수를 반환한다(이미 없는 항목은 무시).
    파일마다 open_online을 반복하지 않아 일괄 삭제가 빠르다.
    """
    norm = [(_vpath(p), bool(r)) for p, r in items]

    async def aop(s):
        count = 0
        for path, recursive in norm:
            try:
                if recursive:
                    s.storage_pool.delete_directory(path)
                else:
                    s.storage_pool.delete_file(path)
                count += 1
            except FileNotFoundError:
                pass  # 이미 삭제됨
        await s.upload_if_online()
        return count

    return asyncio.run(_run_online(config_path, aop, sync=True))


def move(config_path: str, src: str, dst: str) -> None:
    sv, dv = _vpath(src), _vpath(dst)

    async def aop(s):
        if s.storage_pool.file_exists(sv):
            s.storage_pool.move_file(sv, dv)
        else:
            s.storage_pool.move_directory(sv, dv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))


def copy(config_path: str, src: str, dst: str) -> None:
    sv, dv = _vpath(src), _vpath(dst)

    async def aop(s):
        s.storage_pool.copy_file(sv, dv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))
