"""OnlineRecoveryManager 단위 테스트."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stardustlib.exceptions import AuthenticationError, DeviceRegistrationError
from stardustlib.online_recovery import OnlineRecoveryManager


@pytest.fixture
def auth_client():
    mock = AsyncMock()
    mock.login = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def device_mgr():
    mock = AsyncMock()
    mock.register = AsyncMock(return_value="device-123")
    mock.start_heartbeat = AsyncMock()
    mock.stop = AsyncMock()
    return mock


@pytest.fixture
def sync_client():
    mock = AsyncMock()
    mock.upload_metadata = AsyncMock()
    mock.initial_sync = AsyncMock()
    mock.start_periodic_sync = AsyncMock()
    mock.stop = AsyncMock()
    return mock


@pytest.fixture
def p2p_server():
    mock = AsyncMock()
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_recovery_success(auth_client, device_mgr, sync_client, p2p_server):
    """복구 성공 시 모든 단계가 순서대로 호출된다."""
    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, p2p_server,
        check_interval=1,
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        result = await mgr._attempt_recovery()

    assert result is True
    auth_client.login.assert_awaited_once_with("a@b.c", "pw")
    device_mgr.register.assert_awaited_once()
    sync_client.upload_metadata.assert_awaited_once()
    sync_client.initial_sync.assert_awaited_once()
    p2p_server.start.assert_awaited_once()
    device_mgr.start_heartbeat.assert_awaited_once()
    sync_client.start_periodic_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_auth_failure(auth_client, device_mgr, sync_client, p2p_server):
    """인증 실패 시 False를 반환하고 이후 단계를 호출하지 않는다."""
    auth_client.login.side_effect = AuthenticationError("timeout")

    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, p2p_server,
        check_interval=1,
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        result = await mgr._attempt_recovery()

    assert result is False
    device_mgr.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_device_registration_failure(
    auth_client, device_mgr, sync_client, p2p_server
):
    """디바이스 등록 실패 시 False를 반환한다."""
    device_mgr.register.side_effect = DeviceRegistrationError("failed")

    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, p2p_server,
        check_interval=1,
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        result = await mgr._attempt_recovery()

    assert result is False
    sync_client.upload_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_upload_failure(
    auth_client, device_mgr, sync_client, p2p_server
):
    """pending 업로드 실패 시 False를 반환한다."""
    sync_client.upload_metadata.side_effect = Exception("network error")

    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, p2p_server,
        check_interval=1,
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        result = await mgr._attempt_recovery()

    assert result is False
    sync_client.initial_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_loop_stops_on_success(
    auth_client, device_mgr, sync_client, p2p_server
):
    """복구 성공 시 루프가 종료된다."""
    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, p2p_server,
        check_interval=0,  # 즉시 재시도
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        await mgr.start()
        # 짧은 대기 후 복구 완료 확인
        await asyncio.sleep(0.1)

    assert mgr.is_recovered is True
    assert mgr._running is False


@pytest.mark.asyncio
async def test_recovery_no_p2p_server(auth_client, device_mgr, sync_client):
    """P2P 서버가 None이어도 복구가 성공한다."""
    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, None,
        check_interval=1,
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        result = await mgr._attempt_recovery()

    assert result is True
    device_mgr.start_heartbeat.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_cancels_task(auth_client, device_mgr, sync_client, p2p_server):
    """stop() 호출 시 백그라운드 태스크가 취소된다."""
    # 인증 실패로 루프가 계속 재시도하도록 설정
    auth_client.login.side_effect = AuthenticationError("offline")

    mgr = OnlineRecoveryManager(
        auth_client, device_mgr, sync_client, p2p_server,
        check_interval=0,
    )

    with patch.dict("os.environ", {"STARDUST_EMAIL": "a@b.c", "STARDUST_PASSWORD": "pw"}):
        await mgr.start()
        await asyncio.sleep(0.05)
        await mgr.stop()

    assert mgr._running is False
    assert mgr._task is None
