"""중앙 서버 인증 클라이언트.

JWT 토큰 라이프사이클을 관리하며, 오프라인 모드 전환을 지원한다.
"""

from __future__ import annotations

import base64
import json
import logging
import time

import httpx

from stardustlib.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


def _decode_jwt_payload(token: str) -> dict:
    """JWT 토큰의 payload를 base64 디코딩하여 반환한다.

    서명 검증은 서버 측에서 수행하므로 여기서는 클레임 파싱만 한다.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload_b64 = parts[1]
    # base64url 패딩 보정
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


class AuthClient:
    """중앙 서버 인증 클라이언트."""

    def __init__(self, server_url: str, timeout: float = 10.0) -> None:
        self._server_url = server_url.rstrip("/")
        self._timeout = timeout
        self._access_token: str | None = None
        self._refresh_token_value: str | None = None
        self._token_expires_at: float = 0.0
        self._offline: bool = False
        self._user_id: str | None = None
        self._client = httpx.AsyncClient(timeout=timeout)

    async def login(self, email: str, password: str) -> None:
        """로그인하여 토큰 쌍을 메모리에 저장한다.

        Raises:
            AuthenticationError: 401 응답 시 (잘못된 자격 증명)
        """
        try:
            response = await self._client.post(
                f"{self._server_url}/auth/login",
                json={"email": email, "password": password},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning("Login failed due to network error: %s", e)
            self._offline = True
            raise AuthenticationError(
                f"Cannot connect to server: {e}"
            ) from e

        if response.status_code == 401:
            raise AuthenticationError("Invalid credentials")

        if response.status_code >= 500:
            logger.warning(
                "Login failed with server error: %d", response.status_code
            )
            self._offline = True
            raise AuthenticationError(
                f"Server error: {response.status_code}"
            )

        response.raise_for_status()

        data = response.json()
        self._store_tokens(data["access_token"], data["refresh_token"])
        self._offline = False
        logger.info("Login successful for user %s", self._user_id)

    async def refresh_token(self) -> None:
        """access_token 갱신. 401 시 토큰 삭제, 네트워크/5xx 시 기존 토큰 유지."""
        if self._refresh_token_value is None:
            raise AuthenticationError("No refresh token available")

        try:
            response = await self._client.post(
                f"{self._server_url}/auth/refresh",
                json={"refresh_token": self._refresh_token_value},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            # 네트워크 오류 시 기존 토큰 유지
            logger.warning(
                "Token refresh failed due to network error: %s. "
                "Keeping existing tokens.",
                e,
            )
            self._offline = True
            return

        if response.status_code == 401:
            # refresh 실패 → 토큰 삭제, 재로그인 필요
            logger.warning(
                "Refresh token rejected (401). Clearing tokens. "
                "Re-login required."
            )
            self._clear_tokens()
            raise AuthenticationError("Refresh token expired or invalid")

        if response.status_code >= 500:
            # 5xx 시 기존 토큰 유지
            logger.warning(
                "Token refresh failed with server error: %d. "
                "Keeping existing tokens.",
                response.status_code,
            )
            return

        response.raise_for_status()

        data = response.json()
        self._store_tokens(data["access_token"], data["refresh_token"])
        self._offline = False
        logger.info("Token refreshed successfully")

    async def get_valid_token(self) -> str:
        """유효한 access_token 반환. 만료 1분 전 자동 갱신.

        Raises:
            AuthenticationError: 인증되지 않은 상태
        """
        if self._access_token is None:
            raise AuthenticationError("Not authenticated")

        # 만료 1분 전이면 갱신 시도
        if time.time() >= self._token_expires_at - 60:
            await self.refresh_token()

        if self._access_token is None:
            raise AuthenticationError("Not authenticated after refresh")

        return self._access_token

    @property
    def is_authenticated(self) -> bool:
        """토큰이 저장되어 있는지 여부."""
        return self._access_token is not None

    @property
    def is_offline(self) -> bool:
        """오프라인 모드 여부."""
        return self._offline

    @property
    def user_id(self) -> str | None:
        """현재 인증된 사용자 ID."""
        return self._user_id

    def _store_tokens(
        self, access_token: str, refresh_token: str
    ) -> None:
        """토큰을 메모리에 저장하고 만료 시각/user_id를 파싱한다."""
        self._access_token = access_token
        self._refresh_token_value = refresh_token

        try:
            payload = _decode_jwt_payload(access_token)
            self._token_expires_at = float(payload.get("exp", 0))
            self._user_id = payload.get("sub")
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            logger.warning("Failed to parse JWT payload: %s", e)
            self._token_expires_at = 0.0

    def _clear_tokens(self) -> None:
        """저장된 토큰을 모두 삭제한다."""
        self._access_token = None
        self._refresh_token_value = None
        self._token_expires_at = 0.0
        self._user_id = None

    async def close(self) -> None:
        """HTTP 클라이언트를 닫는다."""
        await self._client.aclose()
