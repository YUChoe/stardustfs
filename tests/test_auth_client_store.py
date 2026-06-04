"""AuthClient + CredentialStore 연동 테스트.

토큰 영속화/로딩/회전 기록/동시 갱신 직렬화(reload-skip)/key_password 보관.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from pytest_httpx import HTTPXMock

from stardustlib.auth_client import AuthClient
from stardustlib.credential_store import CredentialStore

SERVER_URL = "https://api.stardustfs.io"


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


def _token_pair(user_id: str = "user-123", expires_in: float = 3600) -> dict:
    exp = time.time() + expires_in
    return {
        "access_token": _make_jwt({"sub": user_id, "exp": exp}),
        "refresh_token": _make_jwt({"sub": user_id, "exp": exp + 86400}),
    }


def _store(tmp_path) -> CredentialStore:
    return CredentialStore(str(tmp_path / "meta.db"))


@pytest.mark.asyncio
async def test_login_persists_to_store(tmp_path, httpx_mock: HTTPXMock):
    store = _store(tmp_path)
    client = AuthClient(SERVER_URL, credential_store=store)
    httpx_mock.add_response(
        url=f"{SERVER_URL}/auth/login", method="POST",
        json=_token_pair(), status_code=200,
    )
    await client.login("u@example.com", "pw-secret")
    data = store.load()
    assert data["access_token"] is not None
    assert data["refresh_token"] is not None
    assert data["email"] == "u@example.com"
    # Property 1: 비밀번호 비저장
    assert "pw-secret" not in json.dumps(data)
    await client.close()


@pytest.mark.asyncio
async def test_load_from_store(tmp_path):
    store = _store(tmp_path)
    pair = _token_pair()
    store.save({
        "version": 1, "server_url": SERVER_URL,
        "access_token": pair["access_token"],
        "refresh_token": pair["refresh_token"],
        "access_expires_at": time.time() + 3600,
        "user_id": "user-123", "email": "u@e", "key_password": "kp",
    })
    client = AuthClient(SERVER_URL, credential_store=store)
    assert client.load_from_store() is True
    assert client.is_authenticated is True
    assert client.key_password == "kp"
    await client.close()


@pytest.mark.asyncio
async def test_refresh_persists_rotated_token(
    tmp_path, httpx_mock: HTTPXMock
):
    store = _store(tmp_path)
    client = AuthClient(SERVER_URL, credential_store=store)
    httpx_mock.add_response(
        url=f"{SERVER_URL}/auth/login", method="POST",
        json=_token_pair(expires_in=10), status_code=200,
    )
    await client.login("u@e", "pw")

    new_pair = _token_pair(expires_in=7200)
    httpx_mock.add_response(
        url=f"{SERVER_URL}/auth/refresh", method="POST",
        json=new_pair, status_code=200,
    )
    # 만료 임박 상태로 강제 → get_valid_token이 갱신
    client._token_expires_at = time.time()
    token = await client.get_valid_token()

    assert token == new_pair["access_token"]
    assert store.load()["access_token"] == new_pair["access_token"]
    await client.close()


@pytest.mark.asyncio
async def test_refresh_skips_when_store_has_fresh_token(
    tmp_path, httpx_mock: HTTPXMock
):
    """다른 프로세스가 이미 갱신했으면(저장소가 신선) HTTP 갱신을 생략한다.

    httpx_mock에 refresh 응답을 추가하지 않으므로, 갱신 시도 시 테스트가 실패한다.
    """
    store = _store(tmp_path)
    client = AuthClient(SERVER_URL, credential_store=store)
    # 메모리 토큰은 만료
    client._access_token = "stale"
    client._refresh_token_value = "r"
    client._token_expires_at = time.time()
    # 저장소에는 신선한 토큰 (다른 프로세스가 갱신해 둠)
    fresh = _make_jwt({"sub": "user-123", "exp": time.time() + 7200})
    store.save({
        "version": 1, "server_url": SERVER_URL,
        "access_token": fresh, "refresh_token": "r2",
        "access_expires_at": time.time() + 7200,
        "user_id": "user-123", "email": None, "key_password": None,
    })

    token = await client.get_valid_token()
    assert token == fresh
    await client.close()


@pytest.mark.asyncio
async def test_set_key_password_persists(tmp_path):
    store = _store(tmp_path)
    client = AuthClient(SERVER_URL, credential_store=store)
    client.set_key_password("secret-kp")
    assert store.load()["key_password"] == "secret-kp"
    assert client.key_password == "secret-kp"
    await client.close()


@pytest.mark.asyncio
async def test_no_store_is_memory_only(httpx_mock: HTTPXMock):
    """credential_store=None이면 기존 메모리 전용 동작(하위호환)."""
    client = AuthClient(SERVER_URL)
    httpx_mock.add_response(
        url=f"{SERVER_URL}/auth/login", method="POST",
        json=_token_pair(), status_code=200,
    )
    await client.login("u@e", "pw")
    assert client.is_authenticated is True
    assert client.key_password is None
    await client.close()
