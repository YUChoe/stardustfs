"""중앙 서버 인증 클라이언트.

JWT 토큰 라이프사이클을 관리하며, 오프라인 모드 전환을 지원한다.
"""

from __future__ import annotations

import base64
import json
import logging
import time

import httpx

from stardustlib.credential_store import CredentialStoreError, file_lock
from stardustlib.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

_STORE_VERSION = 1
_REFRESH_LOCK_TIMEOUT = 10.0


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

    def __init__(
        self, server_url: str, timeout: float = 10.0, credential_store=None
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._timeout = timeout
        self._access_token: str | None = None
        self._refresh_token_value: str | None = None
        self._token_expires_at: float = 0.0
        self._offline: bool = False
        self._user_id: str | None = None
        self._email: str | None = None
        self._key_password: str | None = None
        # 자격증명 저장소(선택). None이면 메모리 전용(하위호환).
        self._credential_store = credential_store
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
        self._email = email
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
            self._persist()  # 저장소의 무효 토큰도 비움
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

        # 만료 1분 전이면 갱신 시도 (저장소가 있으면 락으로 직렬화)
        if time.time() >= self._token_expires_at - 60:
            await self._refresh_with_lock()

        if self._access_token is None:
            raise AuthenticationError("Not authenticated after refresh")

        return self._access_token

    async def _refresh_with_lock(self) -> None:
        """저장소 락으로 갱신을 직렬화한다 (daemon/CLI 동시 갱신 대비).

        락 획득 후 저장소를 재로딩하여, 다른 프로세스가 이미 갱신했으면 재갱신하지
        않는다(refresh 회전 유실 방지). 저장소가 없으면 단순 갱신.
        """
        if self._credential_store is None:
            await self.refresh_token()
            return
        try:
            with file_lock(
                self._credential_store.lock_path, timeout=_REFRESH_LOCK_TIMEOUT
            ):
                self._reload_tokens_from_store()
                # 다른 프로세스가 이미 갱신해 유효하면 재갱신 불필요
                if (
                    self._access_token is not None
                    and time.time() < self._token_expires_at - 60
                ):
                    return
                await self.refresh_token()
        except TimeoutError:
            logger.warning("토큰 갱신 락 타임아웃, 기존 토큰 사용")

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
        self._persist()

    def _clear_tokens(self) -> None:
        """저장된 토큰을 모두 삭제한다."""
        self._access_token = None
        self._refresh_token_value = None
        self._token_expires_at = 0.0
        self._user_id = None

    async def logout(self) -> None:
        """서버에 refresh 토큰 취소를 요청한다 (best-effort).

        서버 엔드포인트 미배포(404)나 네트워크 오류여도 예외를 전파하지 않는다.
        로컬 자격증명 삭제는 호출자(logout 명령)가 수행한다.
        """
        if self._refresh_token_value is None:
            return
        try:
            headers = {}
            if self._access_token is not None:
                headers["Authorization"] = f"Bearer {self._access_token}"
            response = await self._client.post(
                f"{self._server_url}/auth/logout",
                headers=headers,
                json={"refresh_token": self._refresh_token_value},
            )
            if response.status_code == 404:
                logger.info(
                    "서버 logout 엔드포인트 미배포(404), 로컬 정리만 수행"
                )
            elif response.status_code >= 400:
                logger.warning(
                    "서버 logout 실패: HTTP %d", response.status_code
                )
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("서버 logout 호출 실패: %s", e)

    # --- 자격증명 저장소 연동 ---

    def load_from_store(self) -> bool:
        """저장소에서 토큰/이메일/key_password를 메모리로 로딩한다.

        Returns:
            유효한 access_token을 로딩했으면 True.
        """
        if self._credential_store is None:
            return False
        try:
            data = self._credential_store.load()
        except CredentialStoreError as e:
            logger.warning("자격증명 저장소 로딩 실패: %s", e)
            return False
        if not data:
            return False
        self._access_token = data.get("access_token")
        self._refresh_token_value = data.get("refresh_token")
        self._token_expires_at = float(data.get("access_expires_at") or 0.0)
        self._user_id = data.get("user_id")
        self._email = data.get("email")
        self._key_password = data.get("key_password")
        return self._access_token is not None

    def _reload_tokens_from_store(self) -> None:
        """락 보유 중 저장소의 최신 토큰을 메모리로 다시 읽는다."""
        if self._credential_store is None:
            return
        try:
            data = self._credential_store.load()
        except CredentialStoreError:
            return
        if not data:
            return
        self._access_token = data.get("access_token")
        self._refresh_token_value = data.get("refresh_token")
        self._token_expires_at = float(data.get("access_expires_at") or 0.0)
        self._user_id = data.get("user_id")

    def _persist(self) -> None:
        """현재 토큰/이메일/key_password를 저장소에 기록한다(저장소가 있을 때만)."""
        if self._credential_store is None:
            return
        self._credential_store.save({
            "version": _STORE_VERSION,
            "server_url": self._server_url,
            "access_token": self._access_token,
            "refresh_token": self._refresh_token_value,
            "access_expires_at": self._token_expires_at,
            "user_id": self._user_id,
            "email": self._email,
            "key_password": self._key_password,
        })

    def set_key_password(self, key_password: str | None) -> None:
        """마스터키 백업 암호를 설정하고 저장소에 반영한다."""
        self._key_password = key_password
        self._persist()

    @property
    def key_password(self) -> str | None:
        """저장된 마스터키 백업 암호(없으면 None)."""
        return self._key_password

    async def close(self) -> None:
        """HTTP 클라이언트를 닫는다."""
        await self._client.aclose()
