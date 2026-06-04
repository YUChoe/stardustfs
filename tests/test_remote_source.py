"""RemoteSource 단위 테스트.

pytest-httpx를 사용하여 Central Server 및 P2P Server 응답을 모킹한다.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError
from stardustlib.remote_source import RemoteSource, _EventLoopThread


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def auth_client():
    """인증된 상태의 AuthClient mock."""
    client = AsyncMock(spec=AuthClient)
    client.get_valid_token = AsyncMock(return_value="test-token-123")
    client.refresh_token = AsyncMock()
    client.is_authenticated = True
    return client


@pytest.fixture
def remote_source(auth_client):
    """기본 RemoteSource 인스턴스 (비활성 상태)."""
    return RemoteSource(
        source_id="remote-vol1",
        device_id="550e8400-e29b-41d4-a716-446655440000",
        auth_client=auth_client,
        server_url="https://api.stardustfs.io",
        timeout=10.0,
    )


# ------------------------------------------------------------------
# 초기화 테스트
# ------------------------------------------------------------------


class TestInitialize:
    """initialize() 메서드 테스트."""

    def test_initialize_success(self, remote_source, httpx_mock):
        """정상 초기화: routing 응답에서 address를 받아 활성화."""
        httpx_mock.add_response(
            url="https://api.stardustfs.io/routing/550e8400-e29b-41d4-a716-446655440000",
            json={"address": "192.168.1.100:9090", "status": "online"},
        )

        remote_source.initialize()

        assert remote_source.is_active is True
        assert remote_source.peer_address == "192.168.1.100:9090"

    def test_initialize_is_online_true_activates(self, remote_source, httpx_mock):
        """is_online=True면 활성화한다."""
        httpx_mock.add_response(
            url="https://api.stardustfs.io/routing/550e8400-e29b-41d4-a716-446655440000",
            json={"connection_address": "192.168.1.100:9090", "is_online": True},
        )

        remote_source.initialize()

        assert remote_source.is_active is True

    def test_initialize_is_online_false_deactivates(
        self, remote_source, httpx_mock
    ):
        """is_online=False면 주소를 받아도 비활성으로 마운트한다.

        오프라인 디바이스를 활성으로 두면 매 읽기마다 무의미한 P2P 타임아웃이
        반복되므로, 오프라인 placeholder로 즉시 응답하도록 비활성화한다.
        """
        httpx_mock.add_response(
            url="https://api.stardustfs.io/routing/550e8400-e29b-41d4-a716-446655440000",
            json={
                "connection_address": "10.100.9.213:9090",
                "is_online": False,
            },
        )

        remote_source.initialize()

        assert remote_source.is_active is False

    def test_initialize_device_offline(self, remote_source, httpx_mock):
        """대상 디바이스 오프라인 시 비활성 상태."""
        httpx_mock.add_response(
            url="https://api.stardustfs.io/routing/550e8400-e29b-41d4-a716-446655440000",
            status_code=404,
            json={"error": "device offline"},
        )

        remote_source.initialize()

        assert remote_source.is_active is False

    def test_initialize_timeout(self, remote_source, httpx_mock):
        """타임아웃 시 비활성 상태."""
        httpx_mock.add_exception(
            httpx.TimeoutException("Connection timed out"),
            url="https://api.stardustfs.io/routing/550e8400-e29b-41d4-a716-446655440000",
        )

        remote_source.initialize()

        assert remote_source.is_active is False

    def test_initialize_auth_failure(self, remote_source, auth_client):
        """인증 실패 시 비활성 상태."""
        auth_client.get_valid_token = AsyncMock(
            side_effect=AuthenticationError("Not authenticated")
        )

        remote_source.initialize()

        assert remote_source.is_active is False


class TestRefresh:
    """refresh() 재네고시에이션 테스트."""

    _URL = (
        "https://api.stardustfs.io/routing/"
        "550e8400-e29b-41d4-a716-446655440000"
    )

    def test_offline_then_online_reactivates(self, remote_source, httpx_mock):
        """오프라인으로 비활성됐다가 디바이스 재온라인 시 refresh로 재활성화."""
        # 1차: 오프라인
        httpx_mock.add_response(
            url=self._URL,
            json={"connection_address": "10.0.0.5:9090", "is_online": False},
        )
        remote_source.initialize()
        assert remote_source.is_active is False

        # 2차: 온라인 (force로 throttle 우회)
        httpx_mock.add_response(
            url=self._URL,
            json={"connection_address": "10.0.0.5:9090", "is_online": True},
        )
        result = remote_source.refresh(force=True)
        assert result is True
        assert remote_source.is_active is True

    def test_online_then_offline_deactivates(self, remote_source, httpx_mock):
        """온라인이던 디바이스가 오프라인이 되면 refresh로 비활성화."""
        httpx_mock.add_response(
            url=self._URL,
            json={"connection_address": "10.0.0.5:9090", "is_online": True},
        )
        remote_source.initialize()
        assert remote_source.is_active is True

        httpx_mock.add_response(
            url=self._URL,
            json={"connection_address": "10.0.0.5:9090", "is_online": False},
        )
        result = remote_source.refresh(force=True)
        assert result is False
        assert remote_source.is_active is False

    def test_throttle_skips_frequent_calls(self, remote_source, httpx_mock):
        """최소 간격 내 연속 refresh 재호출은 routing 요청 없이 상태를 반환한다."""
        httpx_mock.add_response(
            url=self._URL,
            json={"connection_address": "10.0.0.5:9090", "is_online": False},
        )
        remote_source.initialize()
        assert remote_source.is_active is False

        # 첫 refresh: throttle 타임스탬프를 갱신 (응답 1건 등록)
        httpx_mock.add_response(
            url=self._URL,
            json={"connection_address": "10.0.0.5:9090", "is_online": False},
        )
        remote_source.refresh()

        # 두 번째 refresh: 최소 간격 내이므로 routing 요청 없이 건너뛴다.
        # 추가 응답을 등록하지 않았으므로, 요청이 나가면 httpx_mock이 실패한다.
        result = remote_source.refresh()
        assert result is False


# ------------------------------------------------------------------
# 비활성 상태 테스트
# ------------------------------------------------------------------


class TestInactiveState:
    """비활성 상태에서 메서드 호출 시 OSError 발생."""

    def test_read_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.read("some/file.enc")

    def test_write_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.write("some/file.enc", b"data")

    def test_delete_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.delete("some/file.enc")

    def test_exists_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.exists("some/file.enc")

    def test_mkdir_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.mkdir("some/dir")

    def test_rmdir_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.rmdir("some/dir")

    def test_list_dir_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.list_dir("some/dir")

    def test_get_available_space_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.get_available_space()

    def test_get_total_space_inactive(self, remote_source):
        with pytest.raises(OSError, match="not active"):
            remote_source.get_total_space()


# ------------------------------------------------------------------
# P2P 요청 테스트 (활성 상태)
# ------------------------------------------------------------------


def _activate(source: RemoteSource) -> None:
    """테스트용으로 소스를 활성 상태로 전환."""
    source._active = True
    source._peer_address = "192.168.1.100:9090"


class TestRead:
    """read() 메서드 테스트."""

    def test_read_success(self, remote_source, httpx_mock):
        _activate(remote_source)
        file_data = b"hello world"
        encoded = base64.b64encode(file_data).decode("ascii")

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            json={"data": encoded},
        )

        result = remote_source.read("dir/file.enc")
        assert result == file_data

    def test_read_timeout(self, remote_source, httpx_mock):
        _activate(remote_source)
        # 릴레이 비활성: 직접 연결 timeout이 곧바로 OSError가 되는지 검증
        remote_source._relay_enabled = False

        httpx_mock.add_exception(
            httpx.TimeoutException("timed out"),
            url="http://192.168.1.100:9090/p2p/read",
        )

        with pytest.raises(OSError, match="timed out"):
            remote_source.read("dir/file.enc")

    def test_read_server_error(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            status_code=500,
            json={"error": "internal error"},
        )

        with pytest.raises(OSError, match="HTTP 500"):
            remote_source.read("dir/file.enc")

    def test_read_not_found(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            status_code=404,
            json={"error": "not found"},
        )

        with pytest.raises(OSError, match="HTTP 404"):
            remote_source.read("dir/missing.enc")


class TestRelayFallback:
    """직접 연결 실패 시 릴레이 fallback 테스트."""

    _RELAY_REQ = "https://api.stardustfs.io/relay/request"

    def test_direct_timeout_falls_back_to_relay(
        self, remote_source, httpx_mock
    ):
        """직접 연결 timeout이면 릴레이로 전환해 read에 성공한다."""
        _activate(remote_source)
        file_data = b"relayed bytes"
        encoded = base64.b64encode(file_data).decode("ascii")

        # 직접 연결: timeout
        httpx_mock.add_exception(
            httpx.TimeoutException("timed out"),
            url="http://192.168.1.100:9090/p2p/read",
        )
        # 릴레이: 요청 적재 → request_id
        httpx_mock.add_response(
            url=self._RELAY_REQ,
            method="POST",
            json={"request_id": "req-1"},
        )
        # 릴레이: 응답 long-poll → 대상 처리 결과
        httpx_mock.add_response(
            url="https://api.stardustfs.io/relay/response/req-1",
            method="GET",
            json={"status": 200, "result": {"data": encoded}},
        )

        result = remote_source.read("dir/file.enc")
        assert result == file_data

    def test_direct_connect_error_falls_back_to_relay(
        self, remote_source, httpx_mock
    ):
        """직접 연결 실패(ConnectError)도 릴레이로 전환한다."""
        _activate(remote_source)

        httpx_mock.add_exception(
            httpx.ConnectError("refused"),
            url="http://192.168.1.100:9090/p2p/exists",
        )
        httpx_mock.add_response(
            url=self._RELAY_REQ,
            method="POST",
            json={"request_id": "req-2"},
        )
        httpx_mock.add_response(
            url="https://api.stardustfs.io/relay/response/req-2",
            method="GET",
            json={"status": 200, "result": {"exists": True}},
        )

        assert remote_source.exists("dir/file.enc") is True

    def test_relay_op_error_raises(self, remote_source, httpx_mock):
        """릴레이로 갔으나 대상이 오류 상태를 반환하면 OSError."""
        _activate(remote_source)

        httpx_mock.add_exception(
            httpx.TimeoutException("timed out"),
            url="http://192.168.1.100:9090/p2p/read",
        )
        httpx_mock.add_response(
            url=self._RELAY_REQ,
            method="POST",
            json={"request_id": "req-3"},
        )
        httpx_mock.add_response(
            url="https://api.stardustfs.io/relay/response/req-3",
            method="GET",
            json={"status": 404, "result": {"error": "File not found"}},
        )

        with pytest.raises(OSError, match="status=404"):
            remote_source.read("dir/missing.enc")

    def test_relay_disabled_raises_direct_error(
        self, remote_source, httpx_mock
    ):
        """릴레이 비활성 시 직접 연결 실패가 곧바로 OSError."""
        _activate(remote_source)
        remote_source._relay_enabled = False

        httpx_mock.add_exception(
            httpx.ConnectError("refused"),
            url="http://192.168.1.100:9090/p2p/read",
        )

        with pytest.raises(OSError, match="connection failed|direct failed"):
            remote_source.read("dir/file.enc")


class TestWrite:
    """write() 메서드 테스트."""

    def test_write_success(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/write",
            json={"bytes_written": 11},
        )

        # write는 예외 없이 완료되어야 함
        remote_source.write("dir/file.enc", b"hello world")

        # 요청 본문 검증
        request = httpx_mock.get_request()
        body = json.loads(request.content)
        assert body["physical_path"] == "dir/file.enc"
        assert body["auth_token"] == "test-token-123"
        assert base64.b64decode(body["data"]) == b"hello world"


class TestDelete:
    """delete() 메서드 테스트."""

    def test_delete_success(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/delete",
            json={"success": True},
        )

        remote_source.delete("dir/file.enc")


class TestExists:
    """exists() 메서드 테스트."""

    def test_exists_true(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/exists",
            json={"exists": True},
        )

        assert remote_source.exists("dir/file.enc") is True

    def test_exists_false(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/exists",
            json={"exists": False},
        )

        assert remote_source.exists("dir/missing.enc") is False


class TestMkdir:
    """mkdir() 메서드 테스트."""

    def test_mkdir_success(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/mkdir",
            json={"success": True},
        )

        remote_source.mkdir("new/dir")


class TestRmdir:
    """rmdir() 메서드 테스트."""

    def test_rmdir_success(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/rmdir",
            json={"success": True},
        )

        remote_source.rmdir("old/dir")


class TestListDir:
    """list_dir() 메서드 테스트."""

    def test_list_dir_success(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/list",
            json={"entries": ["file1.enc", "file2.enc", "subdir"]},
        )

        result = remote_source.list_dir("some/dir")
        assert result == ["file1.enc", "file2.enc", "subdir"]

    def test_list_dir_empty(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/list",
            json={"entries": []},
        )

        result = remote_source.list_dir("empty/dir")
        assert result == []


class TestSpace:
    """get_available_space() / get_total_space() 테스트."""

    def test_get_available_space(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/space",
            json={"available": 5368709120, "total": 10737418240},
        )

        assert remote_source.get_available_space() == 5368709120

    def test_get_total_space(self, remote_source, httpx_mock):
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/space",
            json={"available": 5368709120, "total": 10737418240},
        )

        assert remote_source.get_total_space() == 10737418240


# ------------------------------------------------------------------
# 토큰 갱신 재시도 테스트
# ------------------------------------------------------------------


class TestTokenRefreshRetry:
    """토큰 만료 시 갱신 후 1회 재시도."""

    def test_retry_on_401(self, remote_source, auth_client, httpx_mock):
        """401 응답 시 토큰 갱신 후 재시도하여 성공."""
        _activate(remote_source)
        file_data = b"retried data"
        encoded = base64.b64encode(file_data).decode("ascii")

        # 첫 번째 요청: 401
        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            status_code=401,
            json={"error": "token expired"},
        )
        # 두 번째 요청 (재시도): 200
        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            json={"data": encoded},
        )

        result = remote_source.read("dir/file.enc")
        assert result == file_data
        auth_client.refresh_token.assert_called_once()

    def test_retry_fails_after_refresh_failure(
        self, remote_source, auth_client, httpx_mock
    ):
        """토큰 갱신 실패 시 OSError 발생."""
        _activate(remote_source)

        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            status_code=401,
            json={"error": "token expired"},
        )

        auth_client.refresh_token = AsyncMock(
            side_effect=AuthenticationError("Refresh failed")
        )

        with pytest.raises(OSError, match="token refresh failed"):
            remote_source.read("dir/file.enc")

    def test_no_retry_on_second_401(
        self, remote_source, auth_client, httpx_mock
    ):
        """재시도 후에도 401이면 OSError 발생 (무한 루프 방지)."""
        _activate(remote_source)

        # 첫 번째: 401
        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            status_code=401,
            json={"error": "token expired"},
        )
        # 재시도: 또 401
        httpx_mock.add_response(
            url="http://192.168.1.100:9090/p2p/read",
            status_code=401,
            json={"error": "still expired"},
        )

        with pytest.raises(OSError, match="HTTP 401"):
            remote_source.read("dir/file.enc")
