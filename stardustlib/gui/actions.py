"""GUI 백엔드 동작 (Tk 비의존).

워커 스레드에서 호출된다. CLISession/JBODManager/AuthClient/daemon을 재사용하며,
각 동작은 자체적으로 세션을 열고 닫는다(단일 워커 스레드 가정 — sqlite 연결은
스레드별이므로 모든 코어 작업이 같은 워커 스레드에서 수행되어야 한다).

- 조회(list_dir/df_info/status_info): 오프라인 세션(로컬, 빠름).
- 전송/쓰기/devices: 온라인 세션(open_online). 토큰 없으면 RuntimeError("로그인 필요").
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from stardustlib import daemon
from stardustlib.cli.commands import _vpath
from stardustlib.cli.session import CLISession
from stardustlib.config_loader import ConfigLoader

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# --- 초기 설정 생성 (닭-달걀 해소: 설정이 없을 때 새로 만든다) ---

def create_config(
    base_dir: str,
    server_url: str | None,
    device_name: str,
    generate_key: bool = True,
    p2p_port: int = 9090,
) -> str:
    """base_dir에 v2 설정·스토리지 폴더를 만들고 config.json 경로를 반환한다.

    - directory 소스 1개(base/storage), metadata_db, key_file(base/master.key).
    - generate_key=True(첫 디바이스): master.key를 새로 생성.
    - generate_key=False(기존 계정): key_file은 생성하지 않음 → 로그인(키 백업 암호
      포함) 후 첫 온라인 작업에서 서버 백업으로 복원된다.
    - server_url이 비면 오프라인 전용 설정(server.url=null).
    """
    base = os.path.abspath(base_dir)
    storage = os.path.join(base, "storage")
    os.makedirs(storage, exist_ok=True)

    key_path = os.path.join(base, "master.key")
    if generate_key and not os.path.exists(key_path):
        with open(key_path, "wb") as f:
            f.write(os.urandom(32))

    config = {
        "version": 2,
        "server": {"url": server_url or None, "device_name": device_name},
        "sources": [
            {"type": "directory", "id": "local-1", "path": storage}
        ],
        "metadata_db": os.path.join(base, "metadata.db"),
        "key_file": key_path,
        "sync": {"interval_seconds": 30, "conflict_strategy": "copy"},
        "p2p": {"port": p2p_port, "enabled": True},
    }
    cfg_path = os.path.join(base, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return cfg_path


# --- 오프라인 조회 ---

def list_dir(config_path: str, vpath: str) -> list[dict]:
    """가상 경로 목록(로컬 메타데이터). 각 항목: type/name/size/owner."""
    base = _vpath(vpath)
    session = CLISession.open(config_path)
    try:
        rows: list[dict] = []
        for e in session.jbod.list_directory(base):
            owner = ""
            if not e.is_directory:
                meta = session.metadata.lookup(base.rstrip("/") + "/" + e.name)
                owner = meta.device_id[:8] if meta and meta.device_id else ""
            rows.append({
                "type": "dir" if e.is_directory else "file",
                "name": e.name,
                "size": e.file_size,
                "owner": owner,
            })
        rows.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))
        return rows
    finally:
        session.close()


def df_info(config_path: str) -> dict:
    """총/사용/가용 용량."""
    session = CLISession.open(config_path)
    try:
        total = session.jbod.get_total_space()
        available = session.jbod.get_available_space()
        return {"total": total, "used": total - available, "available": available}
    finally:
        session.close()


def status_info(config_path: str) -> dict:
    """보류 변경 수 + 루트 엔트리 수."""
    session = CLISession.open(config_path)
    try:
        return {
            "pending": len(session.metadata.get_pending_files()),
            "root_entries": len(session.jbod.list_directory("/")),
        }
    finally:
        session.close()


# --- 온라인(서버/원격) ---

async def _run_online(config_path: str, aop, sync: bool):
    session = await CLISession.open_online(config_path, sync=sync)
    try:
        if not session.online:
            raise RuntimeError("로그인이 필요합니다. 먼저 로그인하세요.")
        return await aop(session)
    finally:
        await session.aclose()


def devices_list(config_path: str) -> list[dict]:
    async def aop(s):
        return [
            {
                "id": (d.get("id") or "")[:8],
                "name": d.get("name"),
                "online": bool(d.get("is_online")),
                "self": d.get("id") == s.self_device_id,
            }
            for d in (s.my_devices or [])
        ]

    return asyncio.run(_run_online(config_path, aop, sync=False))


def put_file(config_path: str, local: str, remote: str) -> int:
    with open(local, "rb") as f:
        data = f.read()
    rv = _vpath(remote)

    async def aop(s):
        s.jbod.write_file(rv, data)
        await s.upload_if_online()
        return len(data)

    return asyncio.run(_run_online(config_path, aop, sync=True))


def get_file(config_path: str, remote: str, local: str) -> int:
    rv = _vpath(remote)

    async def aop(s):
        return s.jbod.read_file(rv)

    data = asyncio.run(_run_online(config_path, aop, sync=True))
    with open(local, "wb") as f:
        f.write(data)
    return len(data)


def mkdir(config_path: str, path: str) -> None:
    rv = _vpath(path)

    async def aop(s):
        s.jbod.create_directory(rv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))


def remove(config_path: str, path: str, recursive: bool) -> None:
    rv = _vpath(path)

    async def aop(s):
        if recursive:
            s.jbod.delete_directory(rv)
        else:
            s.jbod.delete_file(rv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))


def move(config_path: str, src: str, dst: str) -> None:
    sv, dv = _vpath(src), _vpath(dst)

    async def aop(s):
        if s.jbod.file_exists(sv):
            s.jbod.move_file(sv, dv)
        else:
            s.jbod.move_directory(sv, dv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))


def copy(config_path: str, src: str, dst: str) -> None:
    sv, dv = _vpath(src), _vpath(dst)

    async def aop(s):
        s.jbod.copy_file(sv, dv)
        await s.upload_if_online()

    asyncio.run(_run_online(config_path, aop, sync=True))


# --- 인증 ---

def login(config_path: str, email: str, password: str,
          key_password: str | None = None) -> None:
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore

    async def _do():
        config = ConfigLoader(config_path).load()
        server = config.get("server") or {}
        server_url = server.get("url") if isinstance(server, dict) else None
        if not server_url:
            raise RuntimeError("server.url이 설정되어 있지 않습니다.")
        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        try:
            await auth.login(email, password)
            if key_password:
                auth.set_key_password(key_password)
        finally:
            await auth.close()

    asyncio.run(_do())


def logout(config_path: str) -> None:
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore

    async def _do():
        config = ConfigLoader(config_path).load()
        store = CredentialStore(config["metadata_db"])
        if not store.exists():
            return
        server = config.get("server") or {}
        server_url = (server.get("url") if isinstance(server, dict) else "") or ""
        auth = AuthClient(server_url, credential_store=store)
        auth.load_from_store()
        if server_url:
            await auth.logout()
        await auth.close()
        store.clear()

    asyncio.run(_do())


def is_logged_in(config_path: str) -> bool:
    config = ConfigLoader(config_path).load()
    from stardustlib.credential_store import CredentialStore
    return CredentialStore(config["metadata_db"]).exists()


# --- daemon 라이프사이클 ---

def daemon_status(config_path: str) -> dict:
    config = ConfigLoader(config_path).load()
    return daemon.read_status(config["metadata_db"])


def daemon_stop(config_path: str) -> dict:
    config = ConfigLoader(config_path).load()
    return daemon.request_stop(config["metadata_db"])


def daemon_start(config_path: str) -> int:
    """daemon을 백그라운드 프로세스로 시작하고 pid를 반환한다."""
    proc = subprocess.Popen(
        [sys.executable, "stardustfs.py", "daemon", "--config", config_path],
        cwd=_REPO_ROOT,
    )
    return proc.pid
