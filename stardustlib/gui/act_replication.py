"""GUI 백엔드 — 백업/복구/heal과 복제 상태 조회.

수동 백업은 이 기기가 보관한 청크만 직접 올리고, 다른 기기가 보관한 청크는 그 기기에
위임한다(릴레이로 원본을 당겨오지 않는다). 진행 상태는 데몬 제어 채널에서 읽는다.
"""

from __future__ import annotations

import asyncio

from stardustlib.cli.commands import _vpath
from stardustlib.config_loader import ConfigLoader
from stardustlib.gui.act_core import _offline_session, _run_online


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
