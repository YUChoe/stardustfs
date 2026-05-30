"""디바이스 등록 및 heartbeat 관리.

중앙 서버에 디바이스를 등록하고, 주기적으로 heartbeat를 전송하여
온라인 상태를 유지한다. UPnP NAT 트래버설을 통해 외부 접근을 지원한다.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import socket

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import DeviceRegistrationError

try:
    import miniupnpc  # type: ignore[import-untyped]
    _HAS_MINIUPNPC = True
except ImportError:
    _HAS_MINIUPNPC = False

logger = logging.getLogger(__name__)

# 상수
_REGISTER_RETRY_INTERVAL = 10  # 초
_REGISTER_MAX_RETRIES = 5
_HEARTBEAT_NORMAL_INTERVAL = 60  # 초
_HEARTBEAT_DEGRADED_INTERVAL = 120  # 초
_HEARTBEAT_FAILURE_THRESHOLD = 3
_REQUEST_TIMEOUT = 10.0  # 초
_UPNP_DISCOVER_DELAY = 10000  # 밀리초 (10초)
_UPNP_LEASE_DESCRIPTION = "StardustFS P2P"


def _get_local_ip() -> str:
    """로컬 네트워크 IP를 반환한다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _get_os_info() -> str:
    """운영체제 정보 문자열을 반환한다."""
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


class DeviceManager:
    """디바이스 등록 및 heartbeat 관리."""

    def __init__(
        self,
        auth_client: AuthClient,
        server_url: str,
        device_name: str,
        p2p_port: int,
    ) -> None:
        self._auth_client = auth_client
        self._server_url = server_url.rstrip("/")
        self._device_name = device_name
        self._p2p_port = p2p_port

        self._device_id: str | None = None
        self._heartbeat_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._heartbeat_interval: int = _HEARTBEAT_NORMAL_INTERVAL
        self._consecutive_failures: int = 0
        self._connection_address: str = f"{_get_local_ip()}:{p2p_port}"
        self._offline: bool = False
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)

        # UPnP 상태
        self._upnp_mapped: bool = False
        self._upnp_external_port: int | None = None

    async def register(self) -> str:
        """디바이스를 중앙 서버에 등록하고 device_id를 반환한다.

        기존 device_id가 캐시되어 있으면 재등록을 생략한다.
        등록 실패 시 10초 간격으로 최대 5회 재시도하며,
        모두 실패하면 오프라인 모드로 전환한다.

        Returns:
            등록된 device_id 문자열.

        Raises:
            DeviceRegistrationError: 모든 재시도 실패 시.
        """
        # 기존 device_id 캐시 시 재등록 생략
        if self._device_id is not None:
            logger.info(
                "기존 device_id 캐시 사용: %s", self._device_id
            )
            return self._device_id

        token = await self._auth_client.get_valid_token()
        payload = {
            "name": self._device_name,
            "os": _get_os_info(),
            "connection_address": self._connection_address,
        }
        headers = {"Authorization": f"Bearer {token}"}

        last_error: Exception | None = None
        for attempt in range(1, _REGISTER_MAX_RETRIES + 1):
            try:
                response = await self._client.post(
                    f"{self._server_url}/devices",
                    json=payload,
                    headers=headers,
                )
                if response.status_code < 400:
                    data = response.json()
                    self._device_id = data.get("device_id") or data.get("id")
                    self._offline = False
                    logger.info(
                        "디바이스 등록 성공: device_id=%s",
                        self._device_id,
                    )
                    return self._device_id

                # 4xx/5xx 응답
                last_error = DeviceRegistrationError(
                    f"서버 응답 {response.status_code}"
                )
                logger.warning(
                    "디바이스 등록 실패 (시도 %d/%d): HTTP %d",
                    attempt,
                    _REGISTER_MAX_RETRIES,
                    response.status_code,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e  # type: ignore[assignment]
                logger.warning(
                    "디바이스 등록 실패 (시도 %d/%d): %s",
                    attempt,
                    _REGISTER_MAX_RETRIES,
                    e,
                )

            if attempt < _REGISTER_MAX_RETRIES:
                await asyncio.sleep(_REGISTER_RETRY_INTERVAL)

        # 모든 재시도 실패 → 오프라인 모드
        self._offline = True
        logger.error(
            "디바이스 등록 %d회 모두 실패, 오프라인 모드 전환",
            _REGISTER_MAX_RETRIES,
        )
        raise DeviceRegistrationError(
            f"디바이스 등록 {_REGISTER_MAX_RETRIES}회 재시도 후 실패: "
            f"{last_error}"
        )

    async def start_heartbeat(self) -> None:
        """백그라운드 heartbeat 루프를 시작한다.

        device_id가 없으면 아무 동작도 하지 않는다.
        """
        if self._device_id is None:
            logger.warning("device_id 없음, heartbeat 시작 불가")
            return

        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            logger.debug("heartbeat 이미 실행 중")
            return

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("heartbeat 루프 시작 (간격: %ds)", self._heartbeat_interval)

    async def stop(self) -> None:
        """heartbeat 루프를 중지하고 리소스를 정리한다."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
            logger.info("heartbeat 루프 중지")

        await self.teardown_upnp()
        await self._client.aclose()

    async def setup_upnp(self) -> None:
        """UPnP 포트 매핑을 시도한다.

        P2P 서버 시작 후 호출한다. 성공 시 외부 IP:port를
        connection_address로 설정하고, 실패 시 로컬 IP:port를 유지한다.
        어떤 경우에도 예외를 발생시키지 않는다.
        """
        if not _HAS_MINIUPNPC:
            logger.warning(
                "miniupnpc 라이브러리 미설치, UPnP 포트 매핑 건너뜀"
            )
            return

        try:
            u = miniupnpc.UPnP()
            u.discoverdelay = _UPNP_DISCOVER_DELAY

            devices_found = await asyncio.to_thread(u.discover)
            if devices_found == 0:
                logger.warning("UPnP 게이트웨이를 찾을 수 없음")
                return

            await asyncio.to_thread(u.selectigd)
            external_ip = await asyncio.to_thread(u.externalipaddress)

            local_ip = _get_local_ip()
            result = await asyncio.to_thread(
                u.addportmapping,
                self._p2p_port,
                "TCP",
                local_ip,
                self._p2p_port,
                _UPNP_LEASE_DESCRIPTION,
                "",
            )

            if result:
                self._upnp_mapped = True
                self._upnp_external_port = self._p2p_port
                self._connection_address = f"{external_ip}:{self._p2p_port}"
                logger.info(
                    "UPnP 포트 매핑 성공: %s:%d → %s:%d",
                    external_ip,
                    self._p2p_port,
                    local_ip,
                    self._p2p_port,
                )
            else:
                logger.warning(
                    "UPnP 포트 매핑 실패: addportmapping 반환값 falsy"
                )
        except Exception as e:
            logger.warning("UPnP 포트 매핑 실패: %s", e)

    async def teardown_upnp(self) -> None:
        """UPnP 포트 매핑을 해제한다.

        클라이언트 종료 시 호출한다. 해제 실패 시 WARNING 로그만
        기록하고 종료 절차를 계속 진행한다.
        """
        if not self._upnp_mapped:
            return

        if not _HAS_MINIUPNPC:
            return

        try:
            u = miniupnpc.UPnP()
            u.discoverdelay = _UPNP_DISCOVER_DELAY
            await asyncio.to_thread(u.discover)
            await asyncio.to_thread(u.selectigd)
            await asyncio.to_thread(
                u.deleteportmapping,
                self._upnp_external_port,
                "TCP",
            )
            self._upnp_mapped = False
            logger.info(
                "UPnP 포트 매핑 해제 성공: port %d",
                self._upnp_external_port,
            )
        except Exception as e:
            logger.warning("UPnP 포트 매핑 해제 실패: %s", e)

    def get_connection_address(self) -> str:
        """현재 P2P 접속 주소 (IP:port)를 반환한다."""
        return self._connection_address

    def set_connection_address(self, address: str) -> None:
        """P2P 접속 주소를 외부에서 설정한다 (UPnP 성공 시 사용)."""
        self._connection_address = address

    @property
    def device_id(self) -> str | None:
        """등록된 device_id를 반환한다."""
        return self._device_id

    @device_id.setter
    def device_id(self, value: str | None) -> None:
        """device_id를 외부에서 설정한다 (캐시 복원 시 사용)."""
        self._device_id = value

    @property
    def is_offline(self) -> bool:
        """오프라인 모드 여부."""
        return self._offline

    @property
    def heartbeat_interval(self) -> int:
        """현재 heartbeat 간격 (초)."""
        return self._heartbeat_interval

    @property
    def consecutive_failures(self) -> int:
        """연속 heartbeat 실패 횟수."""
        return self._consecutive_failures

    async def _heartbeat_loop(self) -> None:
        """heartbeat를 주기적으로 전송하는 백그라운드 루프."""
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        """단일 heartbeat를 전송하고 결과에 따라 상태를 갱신한다."""
        try:
            token = await self._auth_client.get_valid_token()
            headers = {"Authorization": f"Bearer {token}"}
            payload = {"connection_address": self._connection_address}

            response = await self._client.put(
                f"{self._server_url}/devices/{self._device_id}/heartbeat",
                json=payload,
                headers=headers,
            )

            if response.status_code < 400:
                self._on_heartbeat_success()
            else:
                self._on_heartbeat_failure(
                    f"HTTP {response.status_code}"
                )
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            Exception,
        ) as e:
            self._on_heartbeat_failure(str(e))

    def _on_heartbeat_success(self) -> None:
        """heartbeat 성공 시 상태를 복원한다."""
        if self._consecutive_failures > 0:
            logger.info(
                "heartbeat 성공, 정상 간격(%ds) 복원",
                _HEARTBEAT_NORMAL_INTERVAL,
            )
        self._consecutive_failures = 0
        self._heartbeat_interval = _HEARTBEAT_NORMAL_INTERVAL

    def _on_heartbeat_failure(self, reason: str) -> None:
        """heartbeat 실패 시 연속 실패 카운트를 증가시키고 간격을 조정한다."""
        self._consecutive_failures += 1
        logger.warning(
            "heartbeat 실패 (%d회 연속): %s",
            self._consecutive_failures,
            reason,
        )

        if self._consecutive_failures >= _HEARTBEAT_FAILURE_THRESHOLD:
            if self._heartbeat_interval != _HEARTBEAT_DEGRADED_INTERVAL:
                self._heartbeat_interval = _HEARTBEAT_DEGRADED_INTERVAL
                logger.warning(
                    "heartbeat %d회 연속 실패, 간격 %ds로 증가",
                    _HEARTBEAT_FAILURE_THRESHOLD,
                    _HEARTBEAT_DEGRADED_INTERVAL,
                )
