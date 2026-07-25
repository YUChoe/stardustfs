"""DeviceManager 단위 테스트.

등록 성공/재시도/실패, heartbeat 간격 변경을 검증한다.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from stardustlib.auth_client import AuthClient
from stardustlib.device_manager import (
    DeviceManager,
    _HEARTBEAT_DEGRADED_INTERVAL,
    _HEARTBEAT_NORMAL_INTERVAL,
    _REGISTER_MAX_RETRIES,
)
from stardustlib.exceptions import DeviceRegistrationError


@pytest_asyncio.fixture
async def auth_client():
    """모의 AuthClient를 생성한다."""
    client = AsyncMock(spec=AuthClient)
    client.get_valid_token = AsyncMock(return_value="fake-token")
    return client


@pytest_asyncio.fixture
async def device_manager(auth_client):
    """DeviceManager 인스턴스를 생성한다."""
    dm = DeviceManager(
        auth_client=auth_client,
        server_url="https://api.example.com",
        device_name="test-device",
        p2p_port=9090,
    )
    yield dm
    await dm.stop()


class TestRegister:
    """register() 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_register_success(self, device_manager, auth_client):
        """정상 등록 시 device_id를 반환한다."""
        mock_response = httpx.Response(
            200,
            json={"device_id": "dev-123"},
            request=httpx.Request("POST", "https://api.example.com/devices"),
        )
        with patch.object(
            device_manager._client, "post", return_value=mock_response
        ):
            result = await device_manager.register()

        assert result == "dev-123"
        assert device_manager.device_id == "dev-123"
        assert not device_manager.is_offline

    @pytest.mark.asyncio
    async def test_register_cached_device_id(self, device_manager):
        """기존 device_id 캐시 시 재등록을 생략한다."""
        device_manager.device_id = "cached-id"
        result = await device_manager.register()
        assert result == "cached-id"

    @pytest.mark.asyncio
    async def test_register_retry_then_success(
        self, device_manager, auth_client
    ):
        """처음 실패 후 재시도에서 성공한다."""
        fail_response = httpx.Response(
            500,
            json={"error": "internal"},
            request=httpx.Request("POST", "https://api.example.com/devices"),
        )
        success_response = httpx.Response(
            200,
            json={"device_id": "dev-456"},
            request=httpx.Request("POST", "https://api.example.com/devices"),
        )

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return fail_response
            return success_response

        with patch.object(device_manager._client, "post", side_effect=mock_post):
            with patch("stardustlib.device_manager.asyncio.sleep", new_callable=AsyncMock):
                result = await device_manager.register()

        assert result == "dev-456"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_register_all_retries_fail(self, device_manager):
        """모든 재시도 실패 시 오프라인 모드로 전환한다."""
        fail_response = httpx.Response(
            503,
            json={"error": "unavailable"},
            request=httpx.Request("POST", "https://api.example.com/devices"),
        )

        with patch.object(
            device_manager._client, "post", return_value=fail_response
        ):
            with patch("stardustlib.device_manager.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(DeviceRegistrationError):
                    await device_manager.register()

        assert device_manager.is_offline
        assert device_manager.device_id is None

    @pytest.mark.asyncio
    async def test_register_network_error(self, device_manager):
        """네트워크 오류 시 재시도 후 오프라인 모드로 전환한다."""
        with patch.object(
            device_manager._client,
            "post",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            with patch("stardustlib.device_manager.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(DeviceRegistrationError):
                    await device_manager.register()

        assert device_manager.is_offline

    @pytest.mark.asyncio
    async def test_register_sends_correct_payload(
        self, device_manager, auth_client
    ):
        """등록 시 name, os, connection_address를 전송한다."""
        captured_kwargs = {}

        async def capture_post(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return httpx.Response(
                200,
                json={"id": "dev-789"},
                request=httpx.Request("POST", "https://api.example.com/devices"),
            )

        with patch.object(device_manager._client, "post", side_effect=capture_post):
            await device_manager.register()

        payload = captured_kwargs["json"]
        assert payload["name"] == "test-device"
        assert "os" in payload
        assert "connection_address" in payload
        assert captured_kwargs["headers"]["Authorization"] == "Bearer fake-token"


class TestListDevices:
    """list_devices() 테스트."""

    @pytest.mark.asyncio
    async def test_list_devices_success(self, device_manager):
        """디바이스 목록을 정상 조회한다."""
        devices_json = [
            {"id": "dev-1", "name": "PC-A", "is_online": True},
            {"id": "dev-2", "name": "PC-B", "is_online": False},
        ]
        mock_response = httpx.Response(
            200,
            json=devices_json,
            request=httpx.Request("GET", "https://api.example.com/devices"),
        )
        with patch.object(
            device_manager._client, "get", return_value=mock_response
        ):
            result = await device_manager.list_devices()

        assert len(result) == 2
        assert result[0]["id"] == "dev-1"
        assert result[1]["name"] == "PC-B"

    @pytest.mark.asyncio
    async def test_list_devices_http_error_returns_empty(self, device_manager):
        """HTTP 오류 시 빈 리스트를 반환한다 (예외 없음)."""
        mock_response = httpx.Response(
            500,
            json={"error": "internal"},
            request=httpx.Request("GET", "https://api.example.com/devices"),
        )
        with patch.object(
            device_manager._client, "get", return_value=mock_response
        ):
            result = await device_manager.list_devices()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_devices_network_error_returns_empty(self, device_manager):
        """네트워크 오류 시 빈 리스트를 반환한다 (예외 없음)."""
        with patch.object(
            device_manager._client, "get",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = await device_manager.list_devices()
        assert result == []


class TestHeartbeat:
    """heartbeat 관련 테스트."""

    @pytest.mark.asyncio
    async def test_heartbeat_success_resets_interval(self, device_manager):
        """heartbeat 성공 시 간격이 60초로 유지/복원된다."""
        device_manager._device_id = "dev-123"
        device_manager._consecutive_failures = 3
        device_manager._heartbeat_interval = _HEARTBEAT_DEGRADED_INTERVAL

        mock_response = httpx.Response(
            200,
            json={"status": "ok"},
            request=httpx.Request(
                "PUT",
                "https://api.example.com/devices/dev-123/heartbeat",
            ),
        )
        with patch.object(
            device_manager._client, "put", return_value=mock_response
        ):
            await device_manager._send_heartbeat()

        assert device_manager.consecutive_failures == 0
        assert device_manager.heartbeat_interval == _HEARTBEAT_NORMAL_INTERVAL

    @pytest.mark.asyncio
    async def test_heartbeat_failure_increments_counter(self, device_manager):
        """heartbeat 실패 시 연속 실패 카운트가 증가한다."""
        device_manager._device_id = "dev-123"

        mock_response = httpx.Response(
            500,
            json={"error": "internal"},
            request=httpx.Request(
                "PUT",
                "https://api.example.com/devices/dev-123/heartbeat",
            ),
        )
        with patch.object(
            device_manager._client, "put", return_value=mock_response
        ):
            await device_manager._send_heartbeat()

        assert device_manager.consecutive_failures == 1
        # 아직 3회 미만이므로 간격 유지
        assert device_manager.heartbeat_interval == _HEARTBEAT_NORMAL_INTERVAL

    @pytest.mark.asyncio
    async def test_heartbeat_3_failures_increases_interval(
        self, device_manager
    ):
        """heartbeat 3회 연속 실패 시 간격이 120초로 증가한다."""
        device_manager._device_id = "dev-123"

        mock_response = httpx.Response(
            500,
            json={"error": "internal"},
            request=httpx.Request(
                "PUT",
                "https://api.example.com/devices/dev-123/heartbeat",
            ),
        )
        with patch.object(
            device_manager._client, "put", return_value=mock_response
        ):
            for _ in range(3):
                await device_manager._send_heartbeat()

        assert device_manager.consecutive_failures == 3
        assert device_manager.heartbeat_interval == _HEARTBEAT_DEGRADED_INTERVAL

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_counts_as_failure(self, device_manager):
        """heartbeat 타임아웃도 실패로 카운트된다."""
        device_manager._device_id = "dev-123"

        with patch.object(
            device_manager._client,
            "put",
            side_effect=httpx.TimeoutException("timeout"),
        ):
            await device_manager._send_heartbeat()

        assert device_manager.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_start_heartbeat_without_device_id(self, device_manager):
        """device_id 없이 start_heartbeat 호출 시 태스크가 생성되지 않는다."""
        await device_manager.start_heartbeat()
        assert device_manager._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_start_heartbeat_creates_task(self, device_manager):
        """device_id가 있으면 heartbeat 태스크가 생성된다."""
        device_manager._device_id = "dev-123"
        await device_manager.start_heartbeat()
        assert device_manager._heartbeat_task is not None
        assert not device_manager._heartbeat_task.done()
        # 정리
        await device_manager.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_heartbeat(self, device_manager):
        """stop() 호출 시 heartbeat 태스크가 취소된다."""
        device_manager._device_id = "dev-123"
        await device_manager.start_heartbeat()
        assert device_manager._heartbeat_task is not None

        await device_manager.stop()
        assert device_manager._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_heartbeat_sends_connection_address(self, device_manager):
        """heartbeat 전송 시 connection_address를 포함한다."""
        device_manager._device_id = "dev-123"
        device_manager._connection_address = "192.168.1.100:9090"

        captured_kwargs = {}

        async def capture_put(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return httpx.Response(
                200,
                json={"status": "ok"},
                request=httpx.Request(
                    "PUT",
                    "https://api.example.com/devices/dev-123/heartbeat",
                ),
            )

        with patch.object(device_manager._client, "put", side_effect=capture_put):
            await device_manager._send_heartbeat()

        assert captured_kwargs["json"]["connection_address"] == "192.168.1.100:9090"


class TestConnectionAddress:
    """connection_address 관련 테스트."""

    def test_get_connection_address(self, device_manager):
        """기본 connection_address는 로컬IP:port 형식이다."""
        addr = device_manager.get_connection_address()
        assert ":" in addr
        assert addr.endswith(":9090")

    def test_set_connection_address(self, device_manager):
        """set_connection_address로 주소를 변경할 수 있다."""
        device_manager.set_connection_address("1.2.3.4:9090")
        assert device_manager.get_connection_address() == "1.2.3.4:9090"


class TestHeartbeatPublishesAddress:
    """_send_heartbeat가 현재 connection_address를 서버에 전송하는지 검증."""

    @pytest.mark.asyncio
    async def test_heartbeat_sends_current_address(
        self, device_manager
    ):
        """보정된 connection_address가 heartbeat payload로 전송된다.

        reflexive 공인 IP 보정 후 _send_heartbeat를 호출하면 즉시 갱신되는 흐름의 핵심.
        """
        device_manager._device_id = "dev-123"
        # 주소 보정이 일어난 상황을 모사: 주소를 공인 IP로 교체
        device_manager._connection_address = "113.10.5.125:9090"

        captured = {}

        async def fake_put(url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(
                200,
                request=httpx.Request("PUT", url),
            )

        with patch.object(device_manager._client, "put", side_effect=fake_put):
            await device_manager._send_heartbeat()

        assert captured["json"]["connection_address"] == "113.10.5.125:9090"
        assert "dev-123/heartbeat" in captured["url"]


class TestSourceReportLoop:
    """start_source_report — 주기적 소스 인벤토리 재신고."""

    @pytest.mark.asyncio
    async def test_periodic_report_invokes_provider_and_report(
        self, device_manager
    ):
        """짧은 간격으로 inventory_provider 결과를 report_sources로 신고한다."""
        device_manager._device_id = "dev-1"
        reported = []
        device_manager.report_sources = AsyncMock(
            side_effect=lambda inv: reported.append(inv) or True
        )
        inv = [{"source_id": "loop-1", "type": "loopback",
                "capacity_bytes": 10, "used_bytes": 1}]

        await device_manager.start_source_report(lambda: inv, interval=0.01)
        await asyncio.sleep(0.05)
        await device_manager.stop()

        assert reported and reported[0] == inv

    def test_default_interval_is_60s(self):
        from stardustlib.device_manager import _SOURCE_REPORT_INTERVAL
        assert _SOURCE_REPORT_INTERVAL == 60

    @pytest.mark.asyncio
    async def test_no_device_id_is_noop(self, device_manager):
        """device_id가 없으면 루프를 시작하지 않는다."""
        device_manager._device_id = None
        device_manager.report_sources = AsyncMock(return_value=True)
        await device_manager.start_source_report(lambda: [], interval=0.01)
        await asyncio.sleep(0.03)
        assert device_manager._source_report_task is None
        device_manager.report_sources.assert_not_called()
