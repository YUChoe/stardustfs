#!/usr/bin/env python3
"""메타데이터 버전 롱폴링 E2E (실서버 대상).

서버 /sync/metadata/wait 엔드포인트가 업로드로 인한 version 증가를 즉시 통지하는지
실서버로 검증한다. 롱폴 미지원 서버(404)에서는 skip한다.

실행:
  STARDUST_TEST_SERVER_URL=http://127.0.0.1:8000 \
  source .venv/Scripts/activate && pytest tests/test_sync_longpoll_e2e.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

SERVER_URL = os.environ.get("STARDUST_TEST_SERVER_URL", "https://stardustfs.noizze.net")
EMAIL = os.environ.get("STARDUST_TEST_EMAIL", "e2e-test@example.com")
PASSWORD = os.environ.get("STARDUST_TEST_PASSWORD", "e2e-test-password-2026")

pytestmark = pytest.mark.asyncio


async def _login(client: httpx.AsyncClient) -> str:
    try:
        await client.post(
            f"{SERVER_URL}/auth/register",
            json={"email": EMAIL, "password": PASSWORD},
        )
    except Exception:
        pass
    resp = await client.post(
        f"{SERVER_URL}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def token():
    async with httpx.AsyncClient(timeout=10.0) as client:
        # 롱폴 미지원 서버면 skip
        try:
            probe = await client.get(f"{SERVER_URL}/sync/metadata/wait")
            if probe.status_code == 404:
                pytest.skip("서버가 버전 롱폴링 미지원(미배포)")
        except Exception:
            pytest.skip("서버에 연결할 수 없음")
        yield await _login(client)


async def _current_version(client, headers) -> int:
    resp = await client.get(
        f"{SERVER_URL}/sync/metadata/status", headers=headers
    )
    resp.raise_for_status()
    v = resp.json().get("version")
    return v if v is not None else 0


async def test_wait_wakes_on_upload(token):
    """업로드로 version이 오르면 대기 중 롱폴러가 즉시 깨어난다."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=40.0) as client:
        base = await _current_version(client, headers)

        async def waiter():
            return await client.get(
                f"{SERVER_URL}/sync/metadata/wait",
                params={"known_version": base},
                headers=headers,
            )

        async def uploader():
            await asyncio.sleep(0.5)
            await client.put(
                f"{SERVER_URL}/sync/metadata",
                headers={**headers, "X-Base-Version": str(base)},
                content=b"e2e-longpoll-blob",
            )

        wait_resp, _ = await asyncio.gather(waiter(), uploader())
        assert wait_resp.status_code == 200
        data = wait_resp.json()
        assert data["changed"] is True
        assert data["version"] == base + 1


async def test_wait_returns_immediately_when_behind(token):
    """known_version이 서버보다 낮으면 즉시 changed=true."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=40.0) as client:
        current = await _current_version(client, headers)
        if current == 0:
            # 메타데이터가 없으면 업로드로 1 만든다
            await client.put(
                f"{SERVER_URL}/sync/metadata",
                headers={**headers, "X-Base-Version": "0"},
                content=b"seed",
            )
            current = await _current_version(client, headers)

        resp = await client.get(
            f"{SERVER_URL}/sync/metadata/wait",
            params={"known_version": max(0, current - 1)},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["changed"] is True
