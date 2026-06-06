"""RelayWorker 단위 테스트.

폴링→디스패치→응답 업로드 한 주기를 mock으로 검증한다.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from stardustlib.auth_client import AuthClient
from stardustlib.relay_worker import RelayWorker


@pytest.fixture
def auth_client():
    client = AsyncMock(spec=AuthClient)
    client.get_valid_token = AsyncMock(return_value="tok")
    return client


@pytest.fixture
def p2p_server():
    """dispatch_async만 사용하는 P2PServer mock."""
    server = MagicMock()
    server.dispatch_async = AsyncMock(return_value=(200, {"data": "QUJD"}))
    return server


@pytest.fixture
def worker(p2p_server, auth_client):
    return RelayWorker(
        p2p_server=p2p_server,
        auth_client=auth_client,
        server_url="https://api.stardustfs.io",
        device_id="dev-target",
    )


@pytest.mark.asyncio
async def test_poll_once_dispatches_and_responds(worker, p2p_server):
    """요청 수신 시 dispatch 후 결과를 /relay/response로 업로드한다."""
    posted = {}

    async def fake_get(url, params=None, headers=None):
        return httpx.Response(
            200,
            json={
                "request_id": "req-9",
                "op": "read",
                "payload": {"physical_path": "f.bin", "source_id": "loop-001"},
            },
            request=httpx.Request("GET", url),
        )

    async def fake_post(url, json=None, headers=None):
        posted["url"] = url
        posted["json"] = json
        return httpx.Response(200, json={"delivered": True},
                              request=httpx.Request("POST", url))

    worker._client.get = fake_get  # type: ignore[assignment]
    worker._client.post = fake_post  # type: ignore[assignment]

    await worker._poll_once()

    # dispatch_async가 폴링된 op/payload로 호출됨
    p2p_server.dispatch_async.assert_called_once_with(
        "read", {"physical_path": "f.bin", "source_id": "loop-001"}
    )
    # 결과가 /relay/response/req-9로 업로드됨
    assert "/relay/response/req-9" in posted["url"]
    assert posted["json"]["status"] == 200
    assert posted["json"]["result"] == {"data": "QUJD"}


@pytest.mark.asyncio
async def test_poll_once_empty_204_noop(worker, p2p_server):
    """204(빈 대기열)면 dispatch하지 않고 즉시 반환한다."""

    async def fake_get(url, params=None, headers=None):
        return httpx.Response(204, request=httpx.Request("GET", url))

    worker._client.get = fake_get  # type: ignore[assignment]

    await worker._poll_once()

    p2p_server.dispatch_async.assert_not_called()


@pytest.mark.asyncio
async def test_poll_once_error_status_propagates_to_response(
    worker, p2p_server
):
    """dispatch가 오류 상태를 내면 그 상태가 응답으로 업로드된다."""
    p2p_server.dispatch_async.return_value = (404, {"error": "File not found"})
    posted = {}

    async def fake_get(url, params=None, headers=None):
        return httpx.Response(
            200,
            json={"request_id": "req-x", "op": "read", "payload": {}},
            request=httpx.Request("GET", url),
        )

    async def fake_post(url, json=None, headers=None):
        posted["json"] = json
        return httpx.Response(200, json={"delivered": True},
                              request=httpx.Request("POST", url))

    worker._client.get = fake_get  # type: ignore[assignment]
    worker._client.post = fake_post  # type: ignore[assignment]

    await worker._poll_once()

    assert posted["json"]["status"] == 404
    assert posted["json"]["result"]["error"] == "File not found"
