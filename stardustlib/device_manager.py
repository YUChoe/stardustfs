"""디바이스 등록 및 heartbeat 관리.

중앙 서버에 디바이스를 등록하고, 주기적으로 heartbeat를 전송하여
온라인 상태를 유지한다. 광고하는 connection_address는 로컬(LAN) 주소이며, 직접 TCP는
같은 LAN에서만 성립한다. 다른 네트워크로의 직접 연결은 UDP 홀펀칭(holepunch)이
담당한다(UPnP·reflexive 공인 IP 보정은 폐지 — 사용자 포트포워딩을 전제하지 않는다).
"""

from __future__ import annotations

import asyncio
import logging
import platform
import socket

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import DeviceRegistrationError

logger = logging.getLogger(__name__)

# 상수
_REGISTER_RETRY_INTERVAL = 10  # 초
_REGISTER_MAX_RETRIES = 5
_HEARTBEAT_NORMAL_INTERVAL = 60  # 초
_HEARTBEAT_DEGRADED_INTERVAL = 120  # 초
_HEARTBEAT_FAILURE_THRESHOLD = 3
_SOURCE_REPORT_INTERVAL = 60  # 초 — 소스 인벤토리 주기 재신고(용량 변동 반영)
_REQUEST_TIMEOUT = 10.0  # 초


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


def build_local_source_inventory(jbod_manager) -> list[dict]:
    """JBOD의 로컬 소스에서 서버 신고용 인벤토리를 만든다.

    원격 소스(is_remote=True)는 제외한다. 각 항목은 식별자/타입/용량/사용 바이트만
    포함하고 물리 경로·파일명은 포함하지 않는다(zero-knowledge).
    type은 클래스명에서 'Source'를 떼고 소문자로 매핑한다(LoopbackSource→loopback).
    """
    inventory: list[dict] = []
    for source in jbod_manager.sources:
        if getattr(source, "is_remote", False):
            continue
        try:
            total = int(source.get_total_space())
            available = int(source.get_available_space())
        except Exception:  # noqa: BLE001 — 용량 조회 실패 시 0으로 신고
            total = available = 0
        used = max(0, total - available)
        stype = type(source).__name__
        if stype.endswith("Source"):
            stype = stype[: -len("Source")]
        # 소스 단위 상태: 활성(FAT 마운트/접근 가능)이면 ready, 아니면 initializing
        # (스토리지 추가 직후 포맷 전 등). 모든 디바이스가 동일 상태를 보도록 신고한다.
        state = "ready" if getattr(source, "is_active", False) else "initializing"
        inventory.append({
            "source_id": source.source_id,
            "type": stype.lower(),
            "capacity_bytes": total,
            "used_bytes": used,
            "state": state,
        })
    return inventory


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
        self._source_report_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._heartbeat_interval: int = _HEARTBEAT_NORMAL_INTERVAL
        self._consecutive_failures: int = 0
        self._connection_address: str = f"{_get_local_ip()}:{p2p_port}"
        self._offline: bool = False
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)

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

    async def list_devices(self) -> list[dict]:
        """서버에서 내 계정에 등록된 디바이스 목록을 조회한다.

        GET /devices. 각 항목은 id, name, os, connection_address,
        is_online 등을 포함한다. 실패 시 빈 리스트를 반환한다(예외 없음).
        """
        try:
            token = await self._auth_client.get_valid_token()
            response = await self._client.get(
                f"{self._server_url}/devices",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code < 400:
                data = response.json()
                return data if isinstance(data, list) else []
            logger.warning(
                "디바이스 목록 조회 실패: HTTP %d", response.status_code
            )
            return []
        except Exception as e:
            logger.warning("디바이스 목록 조회 중 예외: %s", e)
            return []

    async def report_sources(self, sources: list[dict]) -> bool:
        """이 디바이스의 소스 인벤토리를 서버에 신고한다(전량 교체).

        PUT /devices/{device_id}/sources. device_id가 없거나 실패하면 False를
        반환한다(예외 없음 — 시작 차단 금지).
        """
        if self._device_id is None:
            logger.warning("device_id 없음 — 소스 신고를 건너뜁니다")
            return False
        try:
            token = await self._auth_client.get_valid_token()
            response = await self._client.put(
                f"{self._server_url}/devices/{self._device_id}/sources",
                json={"sources": sources},
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code < 400:
                return True
            logger.warning("소스 신고 실패: HTTP %d", response.status_code)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("소스 신고 중 예외: %s", e)
            return False

    async def list_all_sources(self) -> list[dict]:
        """내 모든 디바이스의 소스 인벤토리를 서버에서 조회한다.

        GET /devices/sources. 각 항목은 device_id, device_name, source_id, type,
        capacity_bytes, used_bytes, is_online, updated_at을 포함한다. 실패 시 빈
        리스트를 반환한다(예외 없음).
        """
        try:
            token = await self._auth_client.get_valid_token()
            response = await self._client.get(
                f"{self._server_url}/devices/sources",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code < 400:
                data = response.json()
                return data if isinstance(data, list) else []
            logger.warning("소스 목록 조회 실패: HTTP %d", response.status_code)
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("소스 목록 조회 중 예외: %s", e)
            return []

    async def start_source_report(
        self, inventory_provider, interval: int = _SOURCE_REPORT_INTERVAL
    ) -> None:
        """소스 인벤토리를 주기적으로 재신고하는 백그라운드 루프를 시작한다.

        리모트 디바이스 GUI의 스토리지 용량/사용량이 시간이 지나도 최신값을 보도록
        한다(시작 1회 신고만으로는 용량 변동이 반영되지 않음). device_id가 없으면
        시작하지 않는다. inventory_provider는 매 주기 호출되어 신고할 인벤토리
        목록(build_local_source_inventory 결과)을 반환한다.
        """
        if self._device_id is None:
            logger.warning("device_id 없음, 소스 주기 신고 시작 불가")
            return
        if self._source_report_task is not None and not self._source_report_task.done():
            return
        self._source_report_task = asyncio.create_task(
            self._source_report_loop(inventory_provider, interval)
        )
        logger.info("소스 주기 신고 루프 시작 (간격: %ds)", interval)

    async def _source_report_loop(self, inventory_provider, interval: int) -> None:
        """interval마다 inventory_provider()를 신고한다(실패는 로깅 후 계속)."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.report_sources(inventory_provider())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 신고 실패는 다음 주기 재시도
                logger.warning("소스 주기 신고 실패: %s", e)

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
        """heartbeat·소스 신고 루프를 중지하고 리소스를 정리한다."""
        for attr, name in (
            ("_heartbeat_task", "heartbeat"),
            ("_source_report_task", "소스 주기 신고"),
        ):
            task = getattr(self, attr)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
                logger.info("%s 루프 중지", name)

        await self._client.aclose()

    def get_connection_address(self) -> str:
        """현재 P2P 접속 주소 (IP:port)를 반환한다."""
        return self._connection_address

    def set_connection_address(self, address: str) -> None:
        """P2P 접속 주소를 외부에서 설정한다 (테스트에서 대상 주소 지정 등)."""
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
