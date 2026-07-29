"""GUI 백엔드 — 디바이스·소스 인벤토리(하단 관리 패널의 데이터 원천).

서버 레지스트리를 단일 원천으로 쓴다. 모든 온라인 디바이스가 같은 레지스트리를
동일하게 렌더하므로 결과가 일치한다(자기 표식 self만 다름). 서버 미도달이면 이 기기
로컬 소스만 라이브로 보여주는 강등 모드로 떨어진다.
"""

from __future__ import annotations

import asyncio

from stardustlib.config_loader import ConfigLoader
from stardustlib.gui.act_core import _offline_session


def _split_inventory(devices: list[dict]) -> tuple[list[dict], list[dict]]:
    """인벤토리 응답을 (디바이스 목록, 소스 목록)으로 되돌린다.

    아래 병합 코드는 /devices + /devices/sources 형태를 전제하므로, 합쳐 받은 것을
    같은 모양으로 풀어 준다(구버전 서버 폴백과 경로를 공유한다).
    """
    devs: list[dict] = []
    srcs: list[dict] = []
    for device in devices:
        device_id = device.get("id")
        devs.append({k: v for k, v in device.items() if k != "sources"})
        for source in device.get("sources") or []:
            srcs.append({
                **source,
                "device_id": device_id,
                "device_name": device.get("name"),
                # 소스의 온라인 여부는 그 소스를 가진 디바이스의 상태다.
                "is_online": device.get("is_online"),
            })
    return devs, srcs


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


async def _registry_token(config: dict, server_url: str):
    """레지스트리 조회용 토큰과 AuthClient를 준비한다.

    저장된 자격이 없거나 갱신이 실패하면 (None, None) — 호출자는 강등 처리한다.
    """
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore
    from stardustlib.exceptions import AuthenticationError

    store = CredentialStore(config["metadata_db"])
    auth = AuthClient(server_url, credential_store=store)
    if not auth.load_from_store():
        await auth.close()
        return None, None
    try:
        return auth, await auth.get_valid_token()
    except AuthenticationError:
        await auth.close()
        return None, None


def storage_overview(config_path: str) -> dict:
    """사용자의 모든 디바이스의 모든 소스를 서버 레지스트리(단일 원천)에서 구성한다.

    반환: {"sources": [...], "online": bool}. 각 소스: device_id, device, source_id,
    type, total, used, state, online, self. online=False면 서버 미도달로 이 디바이스
    로컬 소스만 라이브로 보여주는 강등 모드다(다른 디바이스 미상).
    """
    config = ConfigLoader(config_path).load()
    server = config.get("server")
    server_url = server.get("url") if isinstance(server, dict) else None
    device_name = server.get("device_name", "") if isinstance(server, dict) else ""
    if not server_url:
        return {"sources": _local_live_sources(config_path), "online": False}

    async def run_registry():
        import httpx

        from stardustlib.cli.session import _identify_self

        auth, token = await _registry_token(config, server_url)
        if auth is None:
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

        auth, token = await _registry_token(config, server_url)
        if auth is None:
            return None
        try:
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 인벤토리 1회로 디바이스 + 소스를 함께 받는다. 이 화면은 주기
                # 폴링되므로 왕복 하나가 그대로 반복 부하가 된다.
                ir = await client.get(
                    f"{server_url}/devices/inventory", headers=headers)
                if ir.status_code == 200:
                    return _split_inventory(ir.json().get("devices", []))
                # 구버전 서버(인벤토리 미지원) → 기존 두 엔드포인트
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
