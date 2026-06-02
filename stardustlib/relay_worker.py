"""P2P 릴레이 워커 (대상 측).

인바운드 연결을 받을 수 없는 디바이스가 중앙 서버에 long-poll로 대기하여,
자신 앞으로 온 릴레이 요청을 받아 로컬에서 처리하고 결과를 서버에 올린다.

P2PServer.dispatch(op, payload)로 기존 P2P 작업 로직을 재사용한다. 인가는 중앙
서버가 user_id 일치로 보장하므로 워커는 토큰 검증을 하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.p2p_server import P2PServer

logger = logging.getLogger(__name__)

# poll long-poll 타임아웃(서버 25s)보다 약간 길게
_POLL_HTTP_TIMEOUT = 30.0
# 폴링 실패(서버 일시 장애) 시 재시도 대기
_RETRY_DELAY = 5.0


class RelayWorker:
    """대상 디바이스의 백그라운드 릴레이 수신 워커."""

    def __init__(
        self,
        p2p_server: P2PServer,
        auth_client: AuthClient,
        server_url: str,
        device_id: str,
    ) -> None:
        self._p2p_server = p2p_server
        self._auth_client = auth_client
        self._server_url = server_url.rstrip("/")
        self._device_id = device_id
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False
        self._client = httpx.AsyncClient(timeout=_POLL_HTTP_TIMEOUT)

    async def start(self) -> None:
        """릴레이 폴링 루프를 백그라운드 태스크로 시작한다."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Relay worker 시작: device=%s", self._device_id)

    async def stop(self) -> None:
        """릴레이 워커를 정상 종료한다."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._client.aclose()
        logger.info("Relay worker 종료: device=%s", self._device_id)

    async def _loop(self) -> None:
        """poll → dispatch → response 루프."""
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Relay worker 폴링 오류, 재시도: %s", e)
                await asyncio.sleep(_RETRY_DELAY)

    async def _poll_once(self) -> None:
        """단일 폴링 주기: 요청 수신 → 처리 → 결과 업로드."""
        token = await self._auth_client.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        resp = await self._client.get(
            f"{self._server_url}/relay/poll",
            params={"device_id": self._device_id},
            headers=headers,
        )
        # 204: 대기열 비어있음(타임아웃) → 즉시 재폴링
        if resp.status_code == 204:
            return
        if resp.status_code >= 400:
            logger.warning("Relay poll 실패: HTTP %d", resp.status_code)
            await asyncio.sleep(_RETRY_DELAY)
            return

        data = resp.json()
        request_id = data.get("request_id")
        op = data.get("op", "")
        payload = data.get("payload", {})
        if not request_id:
            return

        logger.info("Relay 요청 수신: id=%s op=%s", request_id, op)
        # 기존 P2P 작업 로직 재사용 (인가는 서버가 보장)
        status, result = self._p2p_server.dispatch(op, payload)
        logger.info("Relay dispatch 완료: id=%s status=%d", request_id, status)

        # 결과 업로드
        try:
            await self._client.post(
                f"{self._server_url}/relay/response/{request_id}",
                json={"status": status, "result": result},
                headers=headers,
            )
            logger.info("Relay 응답 업로드 완료: id=%s", request_id)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning("Relay response 업로드 실패: %s", e)
