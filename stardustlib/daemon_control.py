"""데몬 로컬 제어 채널 (전송 위임).

GUI/CLI가 전송(put/get)을 항상-온라인 데몬에 위임한다. 데몬은 홀펀칭 + 릴레이 정책을
보유하므로, 로컬 만석 시 리모트 스필오버나 리모트 파일 get을 직접 UDP로 수행할 수 있다.

서버: 데몬이 127.0.0.1의 임의 포트에 aiohttp 제어 서버를 띄우고 {port, token}을 소유자
전용 제어 파일({metadata_db}.daemon.ctl.json)에 기록한다. 라우트 POST /ctl/put,
/ctl/get은 X-Ctl-Token 헤더로 인증한다(127.0.0.1 바인딩이 1차 신뢰 경계, 토큰은 방어
심화).

클라이언트: 제어 파일을 읽어 데몬에 위임한다. 파일/연결이 없으면 None을 반환해 호출자가
직접 수행으로 fallback하게 한다. 같은 머신이므로 데이터가 아니라 경로를 전달한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets

import httpx
from aiohttp import web

logger = logging.getLogger(__name__)

_CTL_TIMEOUT = 600.0  # 초 — 대용량 전송 허용


def _ctl_path(metadata_db: str) -> str:
    return metadata_db + ".daemon.ctl.json"


def read_ctl(metadata_db: str) -> dict | None:
    """제어 파일을 읽어 {port, token}을 반환한다(없거나 손상 시 None)."""
    try:
        with open(_ctl_path(metadata_db), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "port" in data and "token" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def transfer_via_daemon(
    metadata_db: str, op: str, virtual_path: str, local_path: str
) -> dict | None:
    """데몬 제어 채널로 put/get을 위임한다. 데몬 미실행/실패 시 None(직접 수행 fallback).

    op는 "put" 또는 "get". 같은 머신이므로 경로만 전달한다.
    """
    ctl = read_ctl(metadata_db)
    if ctl is None:
        return None
    url = f"http://127.0.0.1:{ctl['port']}/ctl/{op}"
    try:
        resp = httpx.post(
            url,
            json={"virtual_path": virtual_path, "local_path": local_path},
            headers={"X-Ctl-Token": ctl["token"]},
            timeout=_CTL_TIMEOUT,
        )
    except httpx.HTTPError as e:
        logger.info("데몬 위임 실패(직접 수행으로 fallback): %s", e)
        return None
    if resp.status_code == 200:
        return resp.json()
    # 데몬이 응답했으나 처리 실패(용량 부족 등) — 직접 수행해도 동일하므로 오류 전파.
    raise OSError(
        f"데몬 전송 실패 ({op}): HTTP {resp.status_code} "
        f"{resp.json().get('error', '') if resp.headers.get('content-type','').startswith('application/json') else ''}"
    )


def announce_via_daemon(metadata_db: str, virtual_paths: list[str]) -> int | None:
    """데몬에 수동 백업(announce)을 요청한다.

    Returns:
        등록된 경로 수. 데몬 미실행/연결 실패 시 None(호출자가 안내).

    Raises:
        OSError: 데몬이 응답했으나 처리 실패(예: 리플리케이션 비활성 503).
    """
    ctl = read_ctl(metadata_db)
    if ctl is None:
        return None
    url = f"http://127.0.0.1:{ctl['port']}/ctl/announce"
    try:
        resp = httpx.post(
            url,
            json={"virtual_paths": virtual_paths},
            headers={"X-Ctl-Token": ctl["token"]},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        logger.info("데몬 announce 실패: %s", e)
        return None
    if resp.status_code == 200:
        return int(resp.json().get("announced", 0))
    raise OSError(
        f"데몬 백업 요청 실패: HTTP {resp.status_code}"
    )


class DaemonControlServer:
    """데몬의 로컬 전송 위임 서버(127.0.0.1)."""

    def __init__(
        self,
        storage_pool,
        sync_client,
        metadata_db: str,
        repl_scheduler=None,
    ) -> None:
        self._storage_pool = storage_pool
        self._sync = sync_client
        self._db = metadata_db
        # 리플리케이션 스케줄러(선택). 쓰기 직후 announce로 즉시 백업을 트리거한다.
        self._repl_scheduler = repl_scheduler
        self._token = secrets.token_hex(16)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([
            web.post("/ctl/put", self._handle_put),
            web.post("/ctl/get", self._handle_get),
            web.post("/ctl/announce", self._handle_announce),
        ])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self._write_ctl(port)
        logger.info("데몬 제어 채널 시작: 127.0.0.1:%d", port)

    def _write_ctl(self, port: int) -> None:
        path = _ctl_path(self._db)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": self._token}, f)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)  # 소유자 전용
        except OSError:
            pass

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        try:
            os.remove(_ctl_path(self._db))
        except OSError:
            pass

    def _authorised(self, request: web.Request) -> bool:
        return request.headers.get("X-Ctl-Token") == self._token

    async def _handle_put(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorised"}, status=403)
        body = await request.json()
        vpath = body.get("virtual_path", "")
        local = body.get("local_path", "")
        try:
            with open(local, "rb") as f:
                data = f.read()
            logger.info("데몬 put 시작: %s (%d bytes)", vpath, len(data))
            # 로컬 우선, 만석 시 홀펀칭 UDP로 리모트 스필오버(데몬 storage_pool에 주입됨).
            await asyncio.to_thread(self._storage_pool.write_file, vpath, data)
            if self._sync is not None:
                await self._sync.upload_metadata()
        except Exception as e:  # noqa: BLE001 — 결과를 위임자에게 전달
            logger.warning("데몬 put 실패 %s: %s", vpath, e)
            return web.json_response({"error": str(e)}, status=500)
        # 주기(기본 300초)를 기다리지 않고 즉시 백업하도록 알린다.
        self._announce(vpath)
        logger.info("데몬 put 완료: %s (%d bytes)", vpath, len(data))
        return web.json_response({"ok": True, "bytes": len(data)})

    def _announce(self, virtual_path: str) -> None:
        """리플리케이션 스케줄러에 즉시 백업을 알린다(스케줄러 없으면 무시)."""
        scheduler = self._repl_scheduler
        if scheduler is None:
            return
        try:
            scheduler.announce(virtual_path)
        except Exception as e:  # noqa: BLE001 — 전송 결과에 영향 주지 않음
            logger.warning("백업 announce 실패 %s: %s", virtual_path, e)

    async def _handle_announce(self, request: web.Request) -> web.Response:
        """수동 백업 요청(GUI 컨텍스트 메뉴). 경로들을 즉시 백업 대상으로 등록한다."""
        if not self._authorised(request):
            return web.json_response({"error": "unauthorised"}, status=403)
        body = await request.json()
        vpaths = body.get("virtual_paths") or []
        if not isinstance(vpaths, list):
            return web.json_response(
                {"error": "virtual_paths must be a list"}, status=422
            )
        if self._repl_scheduler is None:
            return web.json_response(
                {"error": "replication is not enabled"}, status=503
            )
        for vpath in vpaths:
            self._announce(str(vpath))
        logger.info("수동 백업 announce: %d개", len(vpaths))
        return web.json_response({"ok": True, "announced": len(vpaths)})

    async def _handle_get(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorised"}, status=403)
        body = await request.json()
        vpath = body.get("virtual_path", "")
        local = body.get("local_path", "")
        try:
            logger.info("데몬 get 시작: %s", vpath)
            data = await asyncio.to_thread(self._storage_pool.read_file, vpath)
            with open(local, "wb") as f:
                f.write(data)
        except Exception as e:  # noqa: BLE001
            logger.warning("데몬 get 실패 %s: %s", vpath, e)
            return web.json_response({"error": str(e)}, status=500)
        logger.info("데몬 get 완료: %s (%d bytes)", vpath, len(data))
        return web.json_response({"ok": True, "bytes": len(data)})
