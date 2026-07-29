"""GUI 백엔드 공용 하부 — 세션 개방/캐시, 온라인 실행 래퍼, 목록 조회.

워커 스레드에서 호출된다. sqlite 연결은 스레드별이므로 모든 코어 작업이 같은 워커
스레드에서 수행되어야 한다(단일 워커 스레드 가정).

- 조회(list_dir/df_info): 오프라인 세션(로컬, 빠름). 캐시해 재초기화를 피한다.
- 전송/쓰기/devices: 온라인 세션(open_online). 토큰 없으면 RuntimeError("로그인 필요").
"""

from __future__ import annotations

import os

from stardustlib.cli.commands import _vpath
from stardustlib.cli.session import CLISession
from stardustlib.config_loader import ConfigLoader


class RemotePathExists(OSError):
    """업로드 대상 가상 경로에 이미 파일이 있어 덮어쓰기를 거부함(GUI 업로드 전용).

    코어 write_file의 덮어쓰기 의미는 CLI/데몬/동기화/복제에서 정상적으로 쓰이므로,
    같은 경로 재업로드 차단은 GUI put_file 경로에서만 가드한다.
    """


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


async def _run_online(config_path: str, aop, sync: bool):
    """온라인 세션을 열어 aop(session)을 실행하고 반드시 닫는다."""
    session = await CLISession.open_online(config_path, sync=sync)
    try:
        if not session.online:
            raise RuntimeError("로그인이 필요합니다. 먼저 로그인하세요.")
        return await aop(session)
    finally:
        await session.aclose()


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
            1 for s in session.storage_pool.sources
            if not getattr(s, "is_remote", False)
        ),
    }


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
