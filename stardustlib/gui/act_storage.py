"""GUI 백엔드 — 설정 생성과 스토리지 소스 관리(추가/포맷/분리).

소스 목록은 config.json이 원천이고, 물리 컨테이너(루프백 FAT 이미지) 생성·삭제와
분리 시 evacuate까지 여기서 다룬다.
"""

from __future__ import annotations

import asyncio
import json
import os

from stardustlib.config_loader import ConfigLoader
from stardustlib.gui.act_auth import is_logged_in
from stardustlib.gui.act_core import _run_online, _rw_session, invalidate


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
