"""AuthClient 단위 테스트.

pytest-httpx를 사용하여 HTTP 요청을 모킹한다.
Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""

from __future__ import annotations

import base64
import json
import time

import pytest
import httpx
from pytest_httpx import HTTPXMock

from stardustlib.auth_client import AuthClient, _decode_jwt_payload
from stardustlib.exceptions import AuthenticationError


def _make_jwt(payload: dict) -> str:
    """테스트용 JWT 토큰 생성 (서명 없음)."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(b"fake_sig").rstrip(b"=").decode()
    return f"{header}.{body}.{signature}"


def _make_token_pair(
    user_id: str = "user-123",
    expires_in: float = 3600,
) -> dict:
    """access_token + refresh_token 응답 데이터 생성."""
    exp = time.time() + expires_in
    access_token = _make_jwt({"sub": user_id, "exp": exp})
    refresh_token = _make_jwt({"sub": user_id, "exp": exp + 86400})
    return {"access_token": access_token, "refresh_token": refresh_token}


SERVER_URL = "https://api.stardustfs.io"


@pytest.fixture
def auth_client():
    """AuthClient 인스턴스 생성."""
    return AuthClient(SERVER_URL)


class TestLogin:
    """Requirement 1.1, 1.2, 1.3: 로그인 성공/실패 테스트."""

    @pytest.mark.asyncio
    async def test_login_success(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """유효한 자격 증명으로 로그인 시 토큰이 저장된다."""
        token_data = _make_token_pair()
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )

        await auth_client.login("user@example.com", "password123")

        assert auth_client.is_authenticated is True
        assert auth_client.user_id == "user-123"
        assert auth_client.is_offline is False

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """401 응답 시 AuthenticationError가 발생한다."""
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            status_code=401,
        )

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await auth_client.login("user@example.com", "wrong")

        assert auth_client.is_authenticated is False

    @pytest.mark.asyncio
    async def test_login_server_error(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """5xx 응답 시 오프라인 모드로 전환된다."""
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            status_code=500,
        )

        with pytest.raises(AuthenticationError, match="Server error"):
            await auth_client.login("user@example.com", "password123")

        assert auth_client.is_offline is True

    @pytest.mark.asyncio
    async def test_login_timeout(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """타임아웃 시 오프라인 모드로 전환된다."""
        httpx_mock.add_exception(
            httpx.ConnectTimeout("Connection timed out"),
            url=f"{SERVER_URL}/auth/login",
        )

        with pytest.raises(AuthenticationError, match="Cannot connect"):
            await auth_client.login("user@example.com", "password123")

        assert auth_client.is_offline is True


class TestRefreshToken:
    """Requirement 1.4, 1.5, 1.6: 토큰 갱신 테스트."""

    @pytest.mark.asyncio
    async def test_refresh_success(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """refresh_token으로 새 토큰 쌍을 수신한다."""
        # 먼저 로그인
        token_data = _make_token_pair()
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        # 갱신
        new_token_data = _make_token_pair(expires_in=7200)
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/refresh",
            method="POST",
            json=new_token_data,
            status_code=200,
        )
        await auth_client.refresh_token()

        assert auth_client.is_authenticated is True

    @pytest.mark.asyncio
    async def test_refresh_401_clears_tokens(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """refresh 실패(401) 시 토큰이 삭제된다."""
        # 로그인
        token_data = _make_token_pair()
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        # refresh 401
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/refresh",
            method="POST",
            status_code=401,
        )

        with pytest.raises(AuthenticationError, match="expired or invalid"):
            await auth_client.refresh_token()

        assert auth_client.is_authenticated is False
        assert auth_client.user_id is None

    @pytest.mark.asyncio
    async def test_refresh_network_error_keeps_tokens(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """네트워크 오류 시 기존 토큰을 유지한다."""
        # 로그인
        token_data = _make_token_pair()
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        # 네트워크 오류
        httpx_mock.add_exception(
            httpx.ConnectTimeout("timeout"),
            url=f"{SERVER_URL}/auth/refresh",
        )
        await auth_client.refresh_token()

        # 기존 토큰 유지
        assert auth_client.is_authenticated is True
        assert auth_client.is_offline is True

    @pytest.mark.asyncio
    async def test_refresh_5xx_keeps_tokens(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """5xx 응답 시 기존 토큰을 유지한다."""
        # 로그인
        token_data = _make_token_pair()
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        # 5xx
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/refresh",
            method="POST",
            status_code=503,
        )
        await auth_client.refresh_token()

        assert auth_client.is_authenticated is True

    @pytest.mark.asyncio
    async def test_refresh_without_token_raises(
        self, auth_client: AuthClient
    ):
        """refresh_token이 없는 상태에서 호출 시 에러."""
        with pytest.raises(AuthenticationError, match="No refresh token"):
            await auth_client.refresh_token()


class TestGetValidToken:
    """Requirement 1.4: 만료 1분 전 자동 갱신."""

    @pytest.mark.asyncio
    async def test_returns_token_when_valid(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """유효한 토큰이 있으면 그대로 반환한다."""
        token_data = _make_token_pair(expires_in=3600)
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        token = await auth_client.get_valid_token()
        assert token == token_data["access_token"]

    @pytest.mark.asyncio
    async def test_auto_refresh_when_expiring_soon(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """만료 1분 이내이면 자동 갱신한다."""
        # 30초 후 만료되는 토큰으로 로그인
        token_data = _make_token_pair(expires_in=30)
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        # 갱신 응답
        new_token_data = _make_token_pair(expires_in=3600)
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/refresh",
            method="POST",
            json=new_token_data,
            status_code=200,
        )

        token = await auth_client.get_valid_token()
        assert token == new_token_data["access_token"]

    @pytest.mark.asyncio
    async def test_raises_when_not_authenticated(
        self, auth_client: AuthClient
    ):
        """인증되지 않은 상태에서 호출 시 에러."""
        with pytest.raises(AuthenticationError, match="Not authenticated"):
            await auth_client.get_valid_token()


class TestOfflineMode:
    """Requirement 1.7: 10초 타임아웃 시 오프라인 모드 전환."""

    @pytest.mark.asyncio
    async def test_offline_mode_on_connect_timeout(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """연결 타임아웃 시 오프라인 모드로 전환된다."""
        httpx_mock.add_exception(
            httpx.ConnectTimeout("Connection timed out"),
            url=f"{SERVER_URL}/auth/login",
        )

        with pytest.raises(AuthenticationError):
            await auth_client.login("user@example.com", "password123")

        assert auth_client.is_offline is True

    @pytest.mark.asyncio
    async def test_offline_cleared_on_successful_login(
        self, auth_client: AuthClient, httpx_mock: HTTPXMock
    ):
        """로그인 성공 시 오프라인 모드가 해제된다."""
        # 먼저 오프라인 상태로 만듦
        auth_client._offline = True

        token_data = _make_token_pair()
        httpx_mock.add_response(
            url=f"{SERVER_URL}/auth/login",
            method="POST",
            json=token_data,
            status_code=200,
        )
        await auth_client.login("user@example.com", "password123")

        assert auth_client.is_offline is False


class TestJwtDecode:
    """JWT 디코딩 유틸리티 테스트."""

    def test_decode_valid_jwt(self):
        """유효한 JWT payload를 디코딩한다."""
        payload = {"sub": "user-456", "exp": 1700000000}
        token = _make_jwt(payload)
        decoded = _decode_jwt_payload(token)
        assert decoded["sub"] == "user-456"
        assert decoded["exp"] == 1700000000

    def test_decode_invalid_format(self):
        """잘못된 형식의 JWT는 ValueError를 발생시킨다."""
        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("not.a.valid.jwt.token")

    def test_decode_missing_parts(self):
        """파트가 부족한 JWT는 ValueError를 발생시킨다."""
        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("only_one_part")
