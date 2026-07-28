"""GUI 백엔드 동작 (Tk 비의존).

워커 스레드에서 호출된다. CLISession/StoragePool/AuthClient/daemon을 재사용하며,
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


class RemotePathExists(OSError):
    """업로드 대상 가상 경로에 이미 파일이 있어 덮어쓰기를 거부함(GUI 업로드 전용).

    코어 write_file의 덮어쓰기 의미는 CLI/데몬/동기화/복제에서 정상적으로 쓰이므로,
    같은 경로 재업로드 차단은 GUI put_file 경로에서만 가드한다.
    """


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

def _rows_for(session, base: str) -> list[dict]:
    """세션으로 디렉토리 항목 행을 만든다. 각 항목: type/name/size/owner/backup."""
    rows: list[dict] = []
    for e in session.storage_pool.list_directory(base):
        owner = ""
        backup = ""
        if not e.is_directory:
            vpath = base.rstrip("/") + "/" + e.name
            meta = session.metadata.lookup(vpath)
            owner = meta.device_id[:8] if meta and meta.device_id else ""
            backup = session.metadata.get_replication_status(vpath) or "none"
        rows.append({
            "type": "dir" if e.is_directory else "file",
            "name": e.name,
            "size": e.file_size,
            "owner": owner,
            "backup": backup,  # none|pending|replicated (로컬 상태)
        })
    rows.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))
    return rows


# 오프라인 조회용 세션 캐시. 워커 스레드 단일 실행 가정(sqlite 연결은 스레드별).
# 매 새로고침마다 _build_core(스토리지 초기화)가 반복 실행/로깅되는 것을 막는다.
_offline_cache: dict[str, "object"] = {}


def _offline_session(config_path: str):
    """조회·용량 표시용 캐시 세션. 루프백 FAT 이미지를 read_only로 연다.

    쓰기는 데몬 단독이므로, 조회 세션이 같은 이미지를 rw로 열어 데몬과 충돌하는 것을
    막는다(스토리지 생성 직후 데몬 reload와의 동시 rw 충돌 방지). evacuate처럼 쓰기가
    필요한 경우는 _rw_session을 쓴다.
    """
    session = _offline_cache.get(config_path)
    if session is None:
        session = CLISession.open(config_path, read_only=True)
        _offline_cache[config_path] = session
    return session


def _rw_session(config_path: str):
    """쓰기 가능한 1회용(비캐시) 오프라인 세션(evacuate 등). 호출자가 close 한다.

    주의: 데몬이 같은 소스를 쓰고 있으면 충돌할 수 있어 호출 전 daemon 정지 권장.
    """
    return CLISession.open(config_path, read_only=False)


def _fat_ready(path: str) -> bool:
    """루프백 FAT 이미지가 준비(포맷 완료)됐는지 read_only 프로브로 확인한다.

    파일이 없거나(추가 직후, 데몬 미생성) 아직 유효한 FAT가 아니면(mkfs 진행 중) False.
    read_only 개방이라 데몬의 rw 사용과 동시에 열어도 손상되지 않는다.
    """
    if not os.path.isfile(path):
        return False
    try:
        from pyfatfs.PyFatFS import PyFatFS

        probe = PyFatFS(path, read_only=True)
        probe.close()
        return True
    except Exception:  # noqa: BLE001 — 비-FAT/포맷 중
        return False


def storage_initializing(config_path: str) -> bool:
    """초기화 중(아직 준비 안 된) 로컬 루프백 소스가 하나라도 있으면 True.

    스토리지 추가 직후 데몬이 FAT 이미지를 생성·포맷하는 동안 True가 되며, 이때
    업로드/다운로드를 막아 반쯤 만들어진 소스로의 전송을 방지한다.
    """
    for s in list_sources(config_path):
        if s.get("type") == "loopback" and not _fat_ready(s.get("path", "")):
            return True
    return False


def _evacuate_offline(config_path: str, source_id: str) -> dict:
    """비캐시 rw 세션으로 evacuate를 수행한다(쓰기 필요).

    같은 프로세스의 read_only 캐시 세션이 이미지를 잡고 있지 않도록 먼저 무효화하고,
    rw 세션으로 이동 후 닫는다.
    """
    invalidate(config_path)
    session = _rw_session(config_path)
    try:
        return session.storage_pool.evacuate_source(source_id)
    finally:
        session.close()


def invalidate(config_path: str | None = None) -> None:
    """캐시된 오프라인 세션을 닫고 버린다(설정/소스 변경·쓰기 후 호출).

    반드시 세션을 생성한 워커 스레드에서 호출해야 한다(sqlite 스레드 제약).
    config_path=None이면 전체.
    """
    keys = [config_path] if config_path else list(_offline_cache)
    for k in keys:
        session = _offline_cache.pop(k, None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def browse(config_path: str, vpath: str) -> dict:
    """목록 + 용량 + 보류 수를 캐시된 단일 오프라인 세션에서 조회한다.

    세션을 재사용해 새로고침/탐색마다 스토리지 초기화가 반복되지 않는다. 파일 목록은
    매 조회 시 메타데이터를 새로 읽으므로 daemon 동기화 결과가 반영된다.
    """
    base = _vpath(vpath)
    session = _offline_session(config_path)
    rows = _rows_for(session, base)
    total = session.storage_pool.get_total_space()
    available = session.storage_pool.get_available_space()
    return {
        "rows": rows,
        "total": total,
        "used": total - available,
        "available": available,
        "pending": len(session.metadata.get_pending_files()),
        "backup_summary": _replication_summary(session),
        "sources": sum(
            1 for s in session.storage_pool.sources if not getattr(s, "is_remote", False)
        ),
    }


def _replication_summary(session) -> dict:
    """전체(전역) 파일의 백업 상태 집계: {none, pending, replicated, total}.

    replication_status는 소유자가 설정하고 동기화로 전파되는 전역 파일 속성이므로,
    같은 사용자의 모든 디바이스가 동일한 집계를 본다(소유 무관 전체 카운트).
    """
    meta = session.metadata
    counts = {
        st: len(meta.list_virtual_paths_for_replication((st,), None))
        for st in ("none", "pending", "replicated")
    }
    counts["total"] = counts["none"] + counts["pending"] + counts["replicated"]
    return counts


# --- 스토리지 소스 관리 (config 편집) ---

def list_sources(config_path: str) -> list[dict]:
    """설정의 스토리지 소스 목록을 반환한다."""
    config = ConfigLoader(config_path).load()
    return list(config.get("sources", []))


def _save_sources(config_path: str, sources: list[dict]) -> None:
    """config.json의 sources만 교체해 저장한다(다른 필드 보존)."""
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    data["sources"] = sources
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_source(
    config_path: str, stype: str, path: str, size: int | None = None
) -> str:
    """loopback 소스를 추가한다(디렉터리 타입 폐지). 생성된 source_id를 반환한다."""
    import uuid

    if stype != "loopback":
        raise ValueError(
            f"지원하지 않는 소스 유형: {stype} (loopback만 추가할 수 있습니다)"
        )
    if not size:
        raise ValueError("loopback 소스는 size(바이트)가 필요합니다.")
    sources = list_sources(config_path)
    entry: dict = {
        "type": "loopback",
        "id": f"loopback-{uuid.uuid4().hex[:6]}",
        "path": os.path.abspath(path),
        "size": int(size),
    }
    sources.append(entry)
    _save_sources(config_path, sources)
    return entry["id"]


def create_storage_image(config_path: str, source_id: str) -> None:
    """루프백 소스의 FAT 이미지를 생성·포맷한다(준비 완료). 이미 FAT면 마운트만.

    조회 세션은 read_only라 이미지를 만들지 못하므로, 추가 시점에 1회용 rw 세션으로
    명시적으로 포맷해 '초기화 중'을 즉시 '준비됨'으로 만든다(데몬 reload 타이밍에
    의존하지 않음). 워커 스레드에서 호출한다(대용량 포맷이 메인 루프를 막지 않도록).
    """
    from stardustlib.storage_source import LoopbackSource

    for s in list_sources(config_path):
        if s.get("id") == source_id and s.get("type") == "loopback":
            src = LoopbackSource(source_id, s["path"], int(s["size"]))
            src.initialize()  # 없거나 비-FAT면 포맷, 유효 FAT면 마운트
            src.close()       # rw 핸들 해제(데몬이 마운트하도록)
            return


def remove_source(config_path: str, source_id: str) -> None:
    """source_id 소스를 설정에서 제거한다(물리 데이터는 삭제하지 않음)."""
    sources = [s for s in list_sources(config_path) if s.get("id") != source_id]
    _save_sources(config_path, sources)


def detach_source(config_path: str, source_id: str) -> dict:
    """소스를 evacuate 후 분리한다.

    그 소스의 활성 파일을 남은 로컬 소스로, 로컬 용량 부족분은 온라인 리모트
    디바이스로 분산 이동한 뒤, 모두 이동되면 설정에서 소스를 제거한다(원자적).
    미이동 파일이 있으면 소스를 유지하고 보고한다.
    반환: {"ok", "moved": [...], "unmoved": [...], "detached": bool}.

    로그인 상태면 온라인 세션(로컬+리모트), 아니면 오프라인 세션(로컬만)으로 evacuate.
    주의: daemon이 같은 소스를 쓰고 있으면 충돌할 수 있어 호출 전 daemon 정지 권장.
    """
    config = ConfigLoader(config_path).load()
    server = config.get("server")
    server_url = server.get("url") if isinstance(server, dict) else None
    use_online = bool(server_url) and is_logged_in(config_path)

    # 분리 성공 시 빈 FAT 컨테이너 이미지를 삭제하기 위해 경로를 미리 확보한다.
    entry = next(
        (s for s in list_sources(config_path) if s.get("id") == source_id), None
    )

    report: dict
    if use_online:
        async def aop(s):
            r = s.storage_pool.evacuate_source(source_id)
            await s.upload_if_online()  # 이동된 메타데이터 전파
            return r

        try:
            report = asyncio.run(_run_online(config_path, aop, sync=True))
        except Exception:  # noqa: BLE001 — 온라인 불가 시 로컬만
            report = _evacuate_offline(config_path, source_id)
    else:
        report = _evacuate_offline(config_path, source_id)

    detached = False
    if report.get("ok"):
        remove_source(config_path, source_id)
        invalidate(config_path)  # 소스 목록 변경 → 다음 조회 시 코어 재빌드
        detached = True
        # 비워진 루프백 FAT 컨테이너 이미지 경로를 보고한다. 실제 삭제는 데몬이
        # 핸들을 놓은 뒤(호출자의 daemon reload 이후) delete_storage_image로 수행한다.
        if entry and entry.get("type") == "loopback":
            report["image_path"] = entry.get("path")
    report["detached"] = detached
    return report


def delete_storage_image(path: str, attempts: int = 12, delay: float = 0.3) -> bool:
    """분리된 루프백 FAT 이미지 파일을 삭제한다(공간 회수).

    데몬이 rw 핸들을 놓을 때까지 Windows에서 삭제가 막힐 수 있어, 짧게 재시도한다.
    삭제 성공/이미 없음이면 True, 끝내 못 지우면 False.
    """
    import time

    if not path or not os.path.isfile(path):
        return True
    for _ in range(attempts):
        try:
            os.remove(path)
            return True
        except OSError:
            time.sleep(delay)
    return not os.path.isfile(path)


# --- 온라인(서버/원격) ---

async def _run_online(config_path: str, aop, sync: bool):
    session = await CLISession.open_online(config_path, sync=sync)
    try:
        if not session.online:
            raise RuntimeError("로그인이 필요합니다. 먼저 로그인하세요.")
        return await aop(session)
    finally:
        await session.aclose()


def devices_summary(config_path: str) -> dict:
    """디바이스 온라인/전체 요약 {online, total}. 미로그인/오프라인이면 {}.

    경량 인증(토큰 + GET /devices)만 사용한다 — open_online(원격 마운트)을 쓰지 않아
    주기 폴링에 가볍고, 디바이스 목록 창(devices_list)과 동일한 /devices 응답을 세므로
    카운트가 일치한다.
    """
    config = ConfigLoader(config_path).load()
    server = config.get("server")
    server_url = server.get("url") if isinstance(server, dict) else None
    if not server_url:
        return {}

    async def run() -> dict:
        import httpx

        from stardustlib.auth_client import AuthClient
        from stardustlib.credential_store import CredentialStore
        from stardustlib.exceptions import AuthenticationError

        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        if not auth.load_from_store():
            await auth.close()
            return {}
        try:
            token = await auth.get_valid_token()
        except AuthenticationError:
            await auth.close()
            return {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{server_url}/devices",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code >= 400:
                return {}
            devs = resp.json()
            if not isinstance(devs, list):
                return {}
            return {
                "online": sum(1 for d in devs if d.get("is_online")),
                "total": len(devs),
            }
        finally:
            await auth.close()

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001 — 미로그인/오프라인
        return {}


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


def _daemon_live_sources(metadata_db: str) -> dict:
    """데몬이 보고하는 로컬 소스 실시간 용량. source_id → {total, used, state}.

    데몬 미실행·조회 실패면 빈 dict(호출자는 서버 레지스트리 값을 그대로 쓴다).
    """
    from stardustlib.daemon_control import storage_via_daemon

    try:
        rows = storage_via_daemon(metadata_db)
    except Exception:  # noqa: BLE001 — 표시 경로라 실패는 무시
        return {}
    if not rows:
        return {}
    return {r["source_id"]: r for r in rows if r.get("source_id")}


def _local_live_sources(config_path: str) -> list[dict]:
    """서버 미도달(강등) 시 이 디바이스의 로컬 소스를 라이브로 구성한다."""
    session = _offline_session(config_path)
    out: list[dict] = []
    for s in session.storage_pool.sources:
        if getattr(s, "is_remote", False):
            continue
        try:
            total = s.get_total_space()
            used = max(0, total - s.get_available_space())
        except Exception:  # noqa: BLE001 — 용량 조회 실패 시 미상
            total = used = None
        out.append({
            "device_id": None,
            "device": "",  # 이 기기(라벨은 GUI에서)
            "source_id": s.source_id,
            "type": "loopback",
            "total": total,
            "used": used,
            "state": "ready" if getattr(s, "is_active", False) else "initializing",
            "online": True,
            "self": True,
        })
    return out


def storage_overview(config_path: str) -> dict:
    """사용자의 모든 디바이스의 모든 소스를 서버 레지스트리(단일 원천)에서 구성한다.

    반환: {"sources": [...], "online": bool}. 각 소스: device_id, device, source_id,
    type, total, used, state, online, self. 모든 온라인 디바이스가 같은 레지스트리를
    동일하게 렌더하므로 결과가 일치한다(자기 표식 self만 다름). online=False면 서버
    미도달로 이 디바이스 로컬 소스만 라이브로 보여주는 강등 모드다(다른 디바이스 미상).
    """
    config = ConfigLoader(config_path).load()
    server = config.get("server")
    server_url = server.get("url") if isinstance(server, dict) else None
    device_name = server.get("device_name", "") if isinstance(server, dict) else ""
    if not server_url:
        return {"sources": _local_live_sources(config_path), "online": False}

    async def run_registry():
        import httpx

        from stardustlib.auth_client import AuthClient
        from stardustlib.cli.session import _identify_self
        from stardustlib.credential_store import CredentialStore
        from stardustlib.exceptions import AuthenticationError

        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        if not auth.load_from_store():
            await auth.close()
            return None
        try:
            token = await auth.get_valid_token()
        except AuthenticationError:
            await auth.close()
            return None
        try:
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                devs_resp = await client.get(
                    f"{server_url}/devices", headers=headers)
                srcs_resp = await client.get(
                    f"{server_url}/devices/sources", headers=headers)
            devs = devs_resp.json() if devs_resp.status_code < 400 else []
            srcs = srcs_resp.json() if srcs_resp.status_code < 400 else []
        finally:
            await auth.close()
        self_id = _identify_self(devs, device_name)
        out: list[dict] = []
        for s in srcs:
            did = s.get("device_id")
            out.append({
                "device_id": did,
                "device": s.get("device_name") or (did[:8] if did else "?"),
                "source_id": s.get("source_id"),
                "type": s.get("type"),
                "total": s.get("capacity_bytes"),
                "used": s.get("used_bytes"),
                "state": s.get("state", "ready"),
                "online": bool(s.get("is_online")),
                "self": bool(did and did == self_id),
            })
        out.sort(key=lambda r: (not r["self"], r["device"] or "", r["source_id"] or ""))
        return out

    try:
        rows = asyncio.run(run_registry())
    except Exception:  # noqa: BLE001 — 미로그인/오프라인이면 강등
        rows = None
    if rows is None:
        return {"sources": _local_live_sources(config_path), "online": False}
    return {"sources": rows, "online": True}


def storage_and_devices(config_path: str) -> dict:
    """디바이스(전체) + 각 디바이스의 소스를 병합해 반환한다(메인 창 하단 패널용).

    {"online": bool, "devices": [{id, name, online, self,
        sources: [{source_id, type, total, used, state, online}]}]}
    레지스트리 단일 원천이라 모든 디바이스에서 동일. online=False면 서버 미도달로
    이 기기 로컬 라이브만 보여주는 강등 모드다.
    """
    config = ConfigLoader(config_path).load()
    server = config.get("server")
    server_url = server.get("url") if isinstance(server, dict) else None
    device_name = server.get("device_name", "") if isinstance(server, dict) else ""

    def _degraded() -> dict:
        srcs = _local_live_sources(config_path)
        return {"online": False, "devices": [{
            "id": None, "name": device_name or "이 기기",
            "online": True, "self": True,
            "sources": [
                {"source_id": s["source_id"], "type": s["type"],
                 "total": s["total"], "used": s["used"],
                 "state": s["state"], "online": True}
                for s in srcs
            ],
        }]}

    if not server_url:
        return _degraded()

    async def run():
        import httpx

        from stardustlib.auth_client import AuthClient
        from stardustlib.credential_store import CredentialStore
        from stardustlib.exceptions import AuthenticationError

        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        if not auth.load_from_store():
            await auth.close()
            return None
        try:
            token = await auth.get_valid_token()
        except AuthenticationError:
            await auth.close()
            return None
        try:
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                dr = await client.get(f"{server_url}/devices", headers=headers)
                sr = await client.get(
                    f"{server_url}/devices/sources", headers=headers)
            devs = dr.json() if dr.status_code < 400 else []
            srcs = sr.json() if sr.status_code < 400 else []
        finally:
            await auth.close()
        return devs, srcs

    try:
        data = asyncio.run(run())
    except Exception:  # noqa: BLE001
        data = None
    if data is None:
        return _degraded()

    from stardustlib.cli.session import _identify_self

    devs, srcs = data
    self_id = _identify_self(devs, device_name)
    by_dev: dict = {}
    for s in srcs:
        by_dev.setdefault(s.get("device_id"), []).append({
            "source_id": s.get("source_id"),
            "type": s.get("type"),
            "total": s.get("capacity_bytes"),
            "used": s.get("used_bytes"),
            "state": s.get("state", "ready"),
            "online": bool(s.get("is_online")),
        })
    # 이 기기 소스는 데몬의 실시간 값으로 덮어쓴다. 서버 레지스트리 값은 소스
    # 인벤토리 재신고 주기만큼 뒤처져 백업이 도는 동안 화면이 멈춘 것처럼 보인다.
    live = _daemon_live_sources(config["metadata_db"])
    if live and self_id:
        for src in by_dev.get(self_id, []):
            fresh = live.get(src["source_id"])
            if fresh is None:
                continue
            if fresh.get("total") is not None:
                src["total"] = fresh["total"]
            if fresh.get("used") is not None:
                src["used"] = fresh["used"]
            src["state"] = fresh.get("state", src["state"])

    devices = []
    for d in devs:
        did = d.get("id")
        devices.append({
            "id": did,
            "name": d.get("name") or (did[:8] if did else "?"),
            "online": bool(d.get("is_online")),
            "self": bool(did and did == self_id),
            "sources": sorted(
                by_dev.get(did, []), key=lambda x: x["source_id"] or ""),
        })
    devices.sort(key=lambda r: (not r["self"], r["name"] or ""))
    return {"online": True, "devices": devices}


def replica_counts(config_path: str, vpath: str, names: list[str]) -> dict:
    """주어진 파일들의 실제 복제본 수(온라인 홀더)를 조회한다.

    {name: {"online": int, "chunks": int, "min": int}}. 로그인 안 됐거나 서버 도달
    불가/저장된 청크 없음이면 해당 항목 생략(상태 컬럼만 유지).

    open_online(전체 코어 재빌드·원격 마운트)을 쓰지 않고, 캐시된 오프라인 세션과
    경량 토큰만으로 서버 조회한다 — 새로고침마다 스토리지 초기화가 반복되지 않는다.
    replication_health는 storage_pool/metadata를 쓰지 않고 서버 API만 호출한다.
    """
    if not names:
        return {}
    base = _vpath(vpath)
    config = ConfigLoader(config_path).load()
    server = config.get("server")
    server_url = server.get("url") if isinstance(server, dict) else None
    if not server_url:
        return {}
    session = _offline_session(config_path)  # 캐시 재사용(재초기화 없음)

    async def run() -> dict:
        from stardustlib.auth_client import AuthClient
        from stardustlib.credential_store import CredentialStore
        from stardustlib.exceptions import AuthenticationError
        from stardustlib.replication_manager import ReplicationManager

        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        if not auth.load_from_store():
            await auth.close()
            return {}
        try:
            await auth.get_valid_token()
        except AuthenticationError:
            await auth.close()
            return {}
        mgr = ReplicationManager(
            auth, server_url, session.metadata, session.storage_pool
        )
        out: dict = {}
        try:
            for name in names:
                vp = base.rstrip("/") + "/" + name
                try:
                    summary = await asyncio.to_thread(
                        mgr.replication_health, vp
                    )
                except Exception:  # noqa: BLE001 — 파일 단위 격리
                    continue
                if summary.chunk_count > 0:
                    out[name] = {
                        "online": summary.min_copies,
                        "chunks": summary.chunk_count,
                        "min": mgr.target_copies,
                    }
        finally:
            mgr.close()
            await auth.close()
        return out

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001 — 오프라인/미로그인 시 상태만 표시
        return {}


def _remote_chunk_devices(session, virtual_path: str) -> set[str]:
    """이 파일의 청크를 보관한 다른 device 집합(로컬 보관은 제외)."""
    self_dev = getattr(session.storage_pool, "device_id", None)
    devices = set()
    for chunk in session.metadata.get_chunks(virtual_path):
        if chunk.device_id and chunk.device_id != self_dev:
            devices.add(chunk.device_id)
    return devices


def _delegate_backup(session, virtual_path: str, devices: set[str]) -> list[str]:
    """청크를 보관한 다른 device들에 백업을 위임한다. 실패한 device 목록 반환.

    데이터를 갖지 않은 기기가 원본을 릴레이로 당겨와 올리는 왕복 대신, 보관 기기가
    자기 몫을 직접 올리게 한다.
    """
    failed = []
    remotes = getattr(session.storage_pool, "_remote_devices", {})
    for device_id in sorted(devices):
        remote = remotes.get(device_id)
        if remote is None or not remote.announce_backup(virtual_path):
            failed.append(device_id)
    return failed


def backup_paths(config_path: str, vpaths: list[str]) -> list[dict]:
    """선택한 파일들을 지금 즉시 복제(백업)한다(온라인 세션 1회).

    이 device가 보관한 청크만 직접 올리고, 다른 device가 보관한 청크는 그 기기에
    위임한다(릴레이로 원본을 당겨오지 않는다).

    {path, status(replicated|pending|skipped|error), delegated?, unreachable?,
    error?} 목록을 반환한다.
    """
    norm = [_vpath(p) for p in vpaths]

    async def aop(s):
        mgr = s.make_replication_manager()
        out: list[dict] = []
        try:
            for vp in norm:
                try:
                    remote_devices = _remote_chunk_devices(s, vp)
                    result = await asyncio.to_thread(mgr.replicate, vp)
                    entry = {"path": vp, "status": result.status}
                    if remote_devices:
                        failed = await asyncio.to_thread(
                            _delegate_backup, s, vp, remote_devices
                        )
                        entry["delegated"] = len(remote_devices) - len(failed)
                        if failed:
                            entry["unreachable"] = failed
                    out.append(entry)
                except Exception as e:  # noqa: BLE001 — 파일 단위 격리
                    out.append({"path": vp, "status": "error", "error": str(e)})
        finally:
            mgr.close()
        return out

    return asyncio.run(_run_online(config_path, aop, sync=False))


def restore_paths(config_path: str, vpaths: list[str]) -> list[dict]:
    """선택한 파일들을 복제본에서 복구해 로컬에 다시 기록한다(온라인 세션 1회).

    소스 손상/유실 시 스웜(≥3 홀더)에서 청크를 받아 결합·복호화 후 로컬 소스에
    복원한다. 복원은 로컬 소유권/메타데이터를 갱신하므로 완료 후 서버에 반영한다.
    {path, status(restored|error), bytes?, error?} 목록을 반환한다.
    """
    norm = [_vpath(p) for p in vpaths]

    async def aop(s):
        mgr = s.make_replication_manager()
        out: list[dict] = []
        try:
            for vp in norm:
                try:
                    nbytes = await asyncio.to_thread(mgr.recover, vp)
                    out.append(
                        {"path": vp, "status": "restored", "bytes": nbytes}
                    )
                except Exception as e:  # noqa: BLE001 — 파일 단위 격리
                    out.append({"path": vp, "status": "error", "error": str(e)})
        finally:
            mgr.close()
        # 복구로 로컬 메타데이터가 바뀌었으면 서버에 반영
        await s.upload_if_online()
        return out

    return asyncio.run(_run_online(config_path, aop, sync=True))


def heal_paths(config_path: str, vpaths: list[str]) -> list[dict]:
    """선택한 파일들의 복제본 부족분을 지금 보충(재복제)한다(온라인 세션 1회)."""
    norm = [_vpath(p) for p in vpaths]

    async def aop(s):
        mgr = s.make_replication_manager()
        out: list[dict] = []
        try:
            for vp in norm:
                try:
                    report = await asyncio.to_thread(mgr.ensure_replicas, vp)
                    out.append({"path": vp, "status": report.status})
                except Exception as e:  # noqa: BLE001 — 파일 단위 격리
                    out.append({"path": vp, "status": "error", "error": str(e)})
        finally:
            mgr.close()
        return out

    return asyncio.run(_run_online(config_path, aop, sync=False))


def announce_paths(config_path: str, vpaths: list[str]) -> dict:
    """선택한 파일들의 백업을 데몬에 즉시 요청한다(announce).

    데몬의 백업 주기(기본 300초)를 기다리지 않고 다음 사이클에서 우선 처리하게 한다.
    전송은 데몬이 수행하므로 GUI는 대기하지 않는다(비차단).

    Returns:
        {"announced": int} 또는 데몬 미실행 시 {"announced": 0, "daemon": False}.

    Raises:
        OSError: 데몬이 리플리케이션 비활성(503) 등으로 요청을 거부한 경우.
    """
    from stardustlib import daemon_control

    config = ConfigLoader(config_path).load()
    db = config.get("metadata_db")
    if not db:
        return {"announced": 0, "daemon": False}
    norm = [_vpath(p) for p in vpaths]
    count = daemon_control.announce_via_daemon(db, norm)
    if count is None:
        return {"announced": 0, "daemon": False}
    return {"announced": count, "daemon": True}


def replication_progress(config_path: str) -> dict | None:
    """데몬의 복제 진행 상태를 조회한다(GUI 폴링).

    {"active": bool, "path", "stage", "done", "total", "secured", "elapsed"}
    또는 데몬 미실행·조회 실패 시 None(호출자는 진행 표시를 생략한다).
    """
    from stardustlib import daemon_control

    config = ConfigLoader(config_path).load()
    db = config.get("metadata_db")
    if not db:
        return None
    return daemon_control.progress_via_daemon(db)


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


def remove(config_path: str, path: str, recursive: bool) -> None:
    rv = _vpath(path)

    async def aop(s):
        if recursive:
            s.storage_pool.delete_directory(rv)
        else:
            s.storage_pool.delete_file(rv)
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


def metadata_mtime(config_path: str) -> float:
    """메타데이터 DB의 최근 변경 시각(metadata.db / -wal 중 최대). 없으면 0.

    GUI가 daemon(별도 프로세스)의 메타데이터 변경을 감지해 자동 새로고침하는 데 쓴다.
    WAL 모드에서는 쓰기가 -wal에 먼저 반영되므로 두 파일 mtime의 최대를 본다.
    """
    config = ConfigLoader(config_path).load()
    db = config.get("metadata_db")
    if not db:
        return 0.0
    latest = 0.0
    for suffix in ("", "-wal"):
        try:
            latest = max(latest, os.path.getmtime(db + suffix))
        except OSError:
            pass
    return latest


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


def daemon_signal_stop(config_path: str) -> dict:
    """정지 신호만 보내고 대기하지 않는다(GUI 종료 시 UI 멈춤 방지)."""
    config = ConfigLoader(config_path).load()
    return daemon.signal_stop(config["metadata_db"])


def daemon_signal_reload(config_path: str) -> dict:
    """config 리로드 신호를 보낸다(daemon이 로컬 소스를 다시 mount). 대기 없음."""
    config = ConfigLoader(config_path).load()
    return daemon.signal_reload(config["metadata_db"])


def daemon_start(config_path: str) -> int:
    """daemon을 백그라운드 프로세스로 시작하고 pid를 반환한다.

    daemon의 stdout/stderr는 {metadata_db}.daemon.log로 보낸다 — GUI 콘솔에
    daemon 초기화 로그가 섞여 '초기화 반복'처럼 보이지 않도록 한다(daemon은 GUI와
    별개 프로세스라 자체 코어 초기화를 수행한다).
    """
    config = ConfigLoader(config_path).load()
    log_path = config["metadata_db"] + ".daemon.log"
    log_file = open(log_path, "a", encoding="utf-8")
    # 자식 프로세스가 로그를 UTF-8로 쓰도록 강제(Windows 기본 cp949 → 파일 mojibake 방지).
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # 프로즌(PyInstaller) exe에는 stardustfs.py 소스가 없으므로 exe 자신의 daemon
    # 서브커맨드를 직접 호출한다. 소스 실행 시에는 python으로 stardustfs.py를 호출.
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "daemon", "--config", config_path]
        cwd = None
    else:
        cmd = [sys.executable, "stardustfs.py", "daemon", "--config", config_path]
        cwd = _REPO_ROOT
    # Windows: 자식(daemon) 콘솔 창이 뜨지 않도록 CREATE_NO_WINDOW.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT, env=env,
            creationflags=creationflags,
        )
    finally:
        log_file.close()  # 자식이 자체 핸들을 보유하므로 부모 핸들은 닫는다
    return proc.pid
