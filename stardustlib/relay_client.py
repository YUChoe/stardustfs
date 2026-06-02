"""P2P 릴레이 클라이언트 (요청자 측).

직접 P2P 연결이 불가능할 때(이중 NAT/CGNAT) 중앙 서버 릴레이를 통해 P2P 작업을
전달하고 결과를 받는다. 모든 요청이 outbound HTTP이므로 NAT를 통과한다.

서버는 payload/result를 불투명 blob으로 중계한다. 파일 데이터는 호출자(RemoteSource)
단계에서 이미 master_key로 암호화된 암호문이다.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# 응답 long-poll은 서버(30s)보다 약간 길게 잡아 서버 504를 받을 수 있게 한다
_REQUEST_TIMEOUT = 35.0


class RelayClient:
    """요청자 측 릴레이 클라이언트.

    request(op, payload)를 호출하면:
      1. POST /relay/request 로 요청 적재 → request_id 수신
      2. GET /relay/response/{request_id} long-poll 로 결과 수신
      3. status != 200 이면 OSError, 200이면 result(dict) 반환
    """

    def __init__(
        self,
        auth_client: AuthClient,
        server_url: str,
        target_device_id: str,
        io,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        self._auth_client = auth_client
        self._server_url = server_url.rstrip("/")
        self._target_device_id = target_device_id
        self._timeout = timeout
        # RemoteSource와 동일한 _EventLoopThread를 공유한다(동기 인터페이스용)
        self._io = io

    def request(self, op: str, payload: dict) -> dict:
        """릴레이로 op를 전달하고 result dict를 반환한다 (동기).

        Raises:
            OSError: 릴레이 실패(타임아웃/오류 상태/서버 도달 불가) 시.
        """
        return self._io.run_coroutine(self.request_async(op, payload))

    async def request_async(self, op: str, payload: dict) -> dict:
        """request의 비동기 버전 (이미 이벤트 루프 안에서 호출할 때 사용)."""
        return await self._async_request(op, payload)

    async def _async_request(self, op: str, payload: dict) -> dict:
        """릴레이 요청-응답 왕복을 수행한다."""
        try:
            token = await self._auth_client.get_valid_token()
        except AuthenticationError as e:
            raise OSError(f"Relay auth failed: {e}") from e

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # 1. 요청 적재
            try:
                resp = await client.post(
                    f"{self._server_url}/relay/request",
                    json={
                        "target_device_id": self._target_device_id,
                        "op": op,
                        "payload": payload,
                    },
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                raise OSError(f"Relay request failed: {e}") from e

            if resp.status_code >= 400:
                raise OSError(
                    f"Relay request rejected: HTTP {resp.status_code}"
                )

            request_id = resp.json().get("request_id")
            if not request_id:
                raise OSError("Relay request: missing request_id")

            # 2. 응답 long-poll
            try:
                wait = await client.get(
                    f"{self._server_url}/relay/response/{request_id}",
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                raise OSError(f"Relay response wait failed: {e}") from e

            if wait.status_code == 504:
                raise OSError(
                    f"Relay timeout: target device did not respond "
                    f"(op={op})"
                )
            if wait.status_code >= 400:
                raise OSError(
                    f"Relay response error: HTTP {wait.status_code}"
                )

            envelope = wait.json()
            status = envelope.get("status")
            result = envelope.get("result", {})

            # 3. 대상 핸들러가 낸 상태를 그대로 반영
            if status != 200:
                error = (
                    result.get("error")
                    if isinstance(result, dict)
                    else None
                )
                raise OSError(
                    f"Relay op failed (status={status}): {error}"
                )

            return result if isinstance(result, dict) else {}
