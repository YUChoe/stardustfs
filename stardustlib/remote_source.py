"""원격 디바이스의 스토리지에 접근하는 StorageSource 구현체.

다른 PC의 P2P Server에 HTTP 요청을 보내 파일 I/O를 수행한다.
StorageSource ABC의 동기 인터페이스를 유지하면서 내부적으로
httpx.AsyncClient를 사용한다.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from typing import Any

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError
from stardustlib.storage_source import StorageSource

logger = logging.getLogger(__name__)


class _EventLoopThread:
    """백그라운드 스레드에서 asyncio 이벤트 루프를 실행한다.

    동기 메서드에서 비동기 httpx 호출을 수행하기 위한 헬퍼.
    """

    _instance: _EventLoopThread | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="remote-source-io"
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_coroutine(self, coro: Any) -> Any:
        """코루틴을 이벤트 루프에서 실행하고 결과를 동기적으로 반환한다."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    @classmethod
    def get_instance(cls) -> _EventLoopThread:
        """싱글턴 인스턴스를 반환한다."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance


class RemoteSource(StorageSource):
    """원격 디바이스의 스토리지에 접근하는 소스.

    Central Server에서 대상 디바이스의 접속 주소를 조회한 뒤,
    해당 디바이스의 P2P Server에 HTTP POST 요청을 보내
    파일 읽기/쓰기/삭제 등을 수행한다.
    """

    def __init__(
        self,
        source_id: str,
        device_id: str,
        auth_client: AuthClient,
        server_url: str,
        timeout: float = 10.0,
    ) -> None:
        # RemoteSource는 로컬 path가 없으므로 빈 문자열 전달
        super().__init__(source_id, path="")
        self._device_id = device_id
        self._auth_client = auth_client
        self._server_url = server_url.rstrip("/")
        self._timeout = timeout
        self._peer_address: str | None = None
        self._client = httpx.AsyncClient(timeout=timeout)
        self._io = _EventLoopThread.get_instance()
        # 재네고시에이션(refresh) throttle: 마지막 시도 시각, 최소 간격(초)
        self._last_refresh_at: float = 0.0
        self._refresh_min_interval: float = 30.0
        # 직접 연결 실패 시 릴레이 fallback 사용 여부
        self._relay_enabled: bool = True
        # 홀펀칭 UDP 전송 콜백: async (device_id, op, payload) -> (status, result).
        # 데몬이 HolePunchService.send_op를 주입. 직접 TCP 실패 시 릴레이 전에 시도.
        self._udp_send = None

    def set_udp_transport(self, fn) -> None:
        """홀펀칭 UDP 전송 콜백을 설정한다(직접 TCP→UDP→릴레이 캐스케이드의 UDP 단계)."""
        self._udp_send = fn

    @property
    def device_id(self) -> str:
        """대상 디바이스 ID."""
        return self._device_id

    @property
    def is_remote(self) -> bool:
        """원격 프록시 소스이므로 항상 True (로컬 용량/쓰기 대상에서 제외)."""
        return True

    @property
    def peer_address(self) -> str | None:
        """대상 디바이스의 P2P 접속 주소 (IP:port)."""
        return self._peer_address

    # ------------------------------------------------------------------
    # 초기화
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Central Server에서 대상 디바이스 접속 주소를 조회하고 활성화한다."""
        try:
            self._io.run_coroutine(self._async_initialize())
        except OSError:
            raise
        except Exception as e:
            self._deactivate(f"Initialisation failed: {e}")

    def refresh(self, *, force: bool = False) -> bool:
        """routing을 재조회하여 활성/주소 상태를 갱신한다 (재네고시에이션).

        오프라인으로 비활성화됐던 디바이스가 다시 온라인이 되면 활성으로
        전환하고, 반대로 온라인이던 디바이스가 오프라인이 되면 비활성으로
        전환한다. 갱신 후 활성 여부를 반환한다.

        잦은 호출을 막기 위해 최소 간격(_refresh_min_interval) 내 재호출은
        건너뛴다. force=True이면 간격을 무시하고 즉시 재조회한다.

        실패(서버 도달 불가 등) 시 예외 없이 현재 상태를 유지하고 활성
        여부를 반환한다.
        """
        import time

        now = time.monotonic()
        if not force and (now - self._last_refresh_at) < self._refresh_min_interval:
            return self._active
        self._last_refresh_at = now

        try:
            self._io.run_coroutine(self._async_initialize())
        except Exception as e:
            logger.warning(
                "RemoteSource '%s' refresh 실패: %s", self._source_id, e
            )
        return self._active

    async def _async_initialize(self) -> None:
        """GET /routing/{device_id}로 접속 주소를 조회한다.

        재호출(refresh) 시에도 동작하도록 매 호출마다 활성 상태를 재평가한다.
        """
        try:
            token = await self._auth_client.get_valid_token()
        except AuthenticationError as e:
            self._deactivate(f"Authentication failed: {e}")
            return

        try:
            response = await self._client.get(
                f"{self._server_url}/routing/{self._device_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as e:
            self._deactivate(f"Routing request timed out: {e}")
            return
        except httpx.ConnectError as e:
            self._deactivate(f"Cannot connect to server: {e}")
            return

        if response.status_code >= 400:
            self._deactivate(
                f"Routing request failed: HTTP {response.status_code}"
            )
            return

        data = response.json()
        # 서버 RoutingResponse는 connection_address 필드를 반환한다
        # (구버전 호환을 위해 address도 fallback으로 허용)
        address = data.get("connection_address") or data.get("address")
        if not address:
            self._deactivate(
                "Routing response missing 'connection_address' field"
            )
            return

        self._peer_address = address

        # 대상 디바이스가 오프라인(heartbeat 만료)이면 비활성으로 마운트한다.
        # 활성으로 두면 매 읽기마다 무의미한 P2P 연결 시도(타임아웃)를 반복하므로,
        # 오프라인 placeholder(503)로 즉시 응답하도록 한다.
        # is_online 필드가 없는 구버전 서버는 도달 가능성을 알 수 없으므로 활성 유지.
        is_online = data.get("is_online")
        if is_online is False:
            self._deactivate(
                f"Target device offline (heartbeat expired): {self._device_id}"
            )
            return

        self._active = True
        logger.info(
            "RemoteSource '%s' initialised: device=%s, address=%s",
            self._source_id,
            self._device_id,
            self._peer_address,
        )

    # ------------------------------------------------------------------
    # StorageSource 메서드 구현
    # ------------------------------------------------------------------

    def read(self, physical_path: str) -> bytes:
        """P2P POST /p2p/read 요청으로 파일 데이터를 읽는다."""
        return self.read_from_source(physical_path, None)

    def read_from_source(
        self, physical_path: str, source_id: str | None
    ) -> bytes:
        """원격 디바이스의 특정 소스(source_id)에서 파일을 읽는다.

        source_id가 None이면 원격 첫 소스를 사용한다(구버전 호환).
        디바이스 단위 라우팅(Device_Router)에서 원격 소스 ID를 지정해 호출한다.
        """
        self._check_active()
        payload: dict[str, Any] = {"physical_path": physical_path}
        if source_id is not None:
            payload["source_id"] = source_id
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/read", payload)
        )
        # 응답 본문에서 base64 디코딩된 data를 반환
        return base64.b64decode(result["data"])

    def write(self, physical_path: str, data: bytes) -> None:
        """P2P POST /p2p/write 요청으로 파일을 기록한다."""
        self._check_active()
        encoded = base64.b64encode(data).decode("ascii")
        self._io.run_coroutine(
            self._p2p_request(
                "/p2p/write",
                {"physical_path": physical_path, "data": encoded},
            )
        )

    def push_blob(self, physical_path: str, data: bytes) -> str:
        """at-rest 암호문 블록을 원격에 기록하고 사용된 원격 source_id를 반환한다.

        evacuate(스토리지 분리 시 파일을 원격으로 이동)에서 사용한다. 데이터는 이미
        암호문이므로 재암호화하지 않는다(zero-knowledge 유지).
        """
        self._check_active()
        encoded = base64.b64encode(data).decode("ascii")
        result = self._io.run_coroutine(
            self._p2p_request(
                "/p2p/write",
                {"physical_path": physical_path, "data": encoded},
            )
        )
        return result.get("source_id", "")

    def delete(self, physical_path: str) -> None:
        """P2P POST /p2p/delete 요청으로 파일을 삭제한다."""
        self._check_active()
        self._io.run_coroutine(
            self._p2p_request("/p2p/delete", {"physical_path": physical_path})
        )

    def exists(self, physical_path: str) -> bool:
        """P2P POST /p2p/exists 요청으로 파일 존재 여부를 확인한다."""
        return self.exists_on_source(physical_path, None)

    def exists_on_source(
        self, physical_path: str, source_id: str | None
    ) -> bool:
        """원격 디바이스의 특정 소스에서 파일 존재 여부를 확인한다."""
        self._check_active()
        payload: dict[str, Any] = {"physical_path": physical_path}
        if source_id is not None:
            payload["source_id"] = source_id
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/exists", payload)
        )
        return bool(result.get("exists", False))

    def mkdir(self, physical_path: str) -> None:
        """P2P POST /p2p/mkdir 요청으로 디렉토리를 생성한다."""
        self._check_active()
        self._io.run_coroutine(
            self._p2p_request("/p2p/mkdir", {"physical_path": physical_path})
        )

    def rmdir(self, physical_path: str) -> None:
        """P2P POST /p2p/rmdir 요청으로 디렉토리를 삭제한다."""
        self._check_active()
        self._io.run_coroutine(
            self._p2p_request("/p2p/rmdir", {"physical_path": physical_path})
        )

    def list_dir(self, physical_path: str) -> list[str]:
        """P2P POST /p2p/list 요청으로 디렉토리 엔트리 목록을 반환한다."""
        return self.list_dir_on_source(physical_path, None)

    def list_dir_on_source(
        self, physical_path: str, source_id: str | None
    ) -> list[str]:
        """원격 디바이스의 특정 소스에서 디렉토리 엔트리 목록을 반환한다."""
        self._check_active()
        payload: dict[str, Any] = {"physical_path": physical_path}
        if source_id is not None:
            payload["source_id"] = source_id
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/list", payload)
        )
        return list(result.get("entries", []))

    def get_available_space(self) -> int:
        """P2P POST /p2p/space 요청으로 가용 공간을 반환한다."""
        self._check_active()
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/space", {})
        )
        return int(result["available"])

    def get_total_space(self) -> int:
        """P2P POST /p2p/space 요청으로 전체 공간을 반환한다."""
        self._check_active()
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/space", {})
        )
        return int(result["total"])

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _check_active(self) -> None:
        """비활성 상태에서 호출 시 OSError를 발생시킨다."""
        if not self._active:
            raise OSError(
                f"RemoteSource '{self._source_id}' is not active "
                f"(device: {self._device_id})"
            )

    async def _p2p_request(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """P2P Server에 POST 요청을 보낸다.

        토큰 만료(401) 시 갱신 후 1회 재시도한다.
        """
        return await self._do_p2p_request(endpoint, payload, retry=True)

    async def _do_p2p_request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        retry: bool = True,
    ) -> dict[str, Any]:
        """실제 P2P 요청 수행. retry=True이면 401 시 토큰 갱신 후 재시도."""
        try:
            token = await self._auth_client.get_valid_token()
        except AuthenticationError as e:
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"authentication failed: {e}"
            ) from e

        request_body = {**payload, "auth_token": token}
        url = f"http://{self._peer_address}{endpoint}"

        try:
            response = await self._client.post(url, json=request_body)
        except httpx.TimeoutException as e:
            logger.info(
                "직접 P2P 타임아웃(%s) — UDP/릴레이 fallback 시도: %s",
                endpoint, self._device_id,
            )
            return await self._fallback(endpoint, request_body, e)
        except (httpx.ConnectError, httpx.NetworkError) as e:
            logger.info(
                "직접 P2P 연결 실패(%s) — UDP/릴레이 fallback 시도: %s",
                endpoint, self._device_id,
            )
            return await self._fallback(endpoint, request_body, e)

        # 401 시 토큰 갱신 후 1회 재시도
        if response.status_code == 401 and retry:
            try:
                await self._auth_client.refresh_token()
            except AuthenticationError as e:
                raise OSError(
                    f"RemoteSource '{self._source_id}': "
                    f"token refresh failed: {e}"
                ) from e
            return await self._do_p2p_request(
                endpoint, payload, retry=False
            )

        if response.status_code >= 400:
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"P2P request failed ({endpoint}): "
                f"HTTP {response.status_code}"
            )

        return response.json()

    # endpoint("/p2p/read") → op("read") 매핑
    _ENDPOINT_OP = {
        "/p2p/read": "read",
        "/p2p/write": "write",
        "/p2p/delete": "delete",
        "/p2p/list": "list",
        "/p2p/exists": "exists",
        "/p2p/mkdir": "mkdir",
        "/p2p/rmdir": "rmdir",
        "/p2p/space": "space",
    }

    async def _fallback(
        self,
        endpoint: str,
        payload: dict[str, Any],
        direct_error: Exception,
    ) -> dict[str, Any]:
        """직접 TCP 실패 시 (1) 홀펀칭 UDP → (2) 릴레이 순으로 fallback한다.

        payload는 auth_token을 포함한다(홀더가 UDP/릴레이에서 토큰 검증). UDP가 200을
        반환하면 그 result를, 비-200/예외면 릴레이로 넘어간다.
        """
        op = self._ENDPOINT_OP.get(endpoint)
        # (1) 홀펀칭 UDP
        if self._udp_send is not None and op is not None:
            status = None
            result: dict[str, Any] = {}
            logger.info("홀펀칭 UDP 전송 시도(%s) → device=%s", op, self._device_id)
            try:
                status, result = await self._udp_send(
                    self._device_id, op, payload
                )
            except Exception as e:  # noqa: BLE001 — 펀치/전송 실패 → 릴레이로
                logger.info("홀펀칭 UDP 실패(%s) → 릴레이 시도: %s", op, e)
                status = None
            if status == 200:
                logger.info("홀펀칭 UDP 전송 성공(%s) device=%s", op, self._device_id)
                return result
            if status is not None:
                # 홀더의 확정 응답(권한/없음/쿼터 등)은 릴레이해도 동일 → 오류 종결.
                raise OSError(
                    f"RemoteSource '{self._source_id}': UDP op {op} HTTP {status}"
                )
        # (2) 릴레이
        return await self._relay_fallback(endpoint, payload, direct_error)

    async def _relay_fallback(
        self,
        endpoint: str,
        payload: dict[str, Any],
        direct_error: Exception,
    ) -> dict[str, Any]:
        """직접 연결 실패 시 중앙 서버 릴레이로 동일 작업을 전달한다.

        릴레이가 비활성이거나 릴레이도 실패하면 원래의 직접 연결 오류 맥락을
        담아 OSError를 발생시킨다(조용한 건너뛰기 금지).
        """
        if not self._relay_enabled:
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"P2P direct failed ({endpoint}): {direct_error}"
            ) from direct_error

        op = self._ENDPOINT_OP.get(endpoint)
        if op is None:
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"relay unsupported endpoint {endpoint}"
            ) from direct_error

        from stardustlib.relay_client import RelayClient

        relay = RelayClient(
            self._auth_client,
            self._server_url,
            self._device_id,
            self._io,
        )
        try:
            # 이미 _io 이벤트 루프 컨텍스트에서 실행 중이므로 비동기 메서드를
            # 직접 await한다 (동기 래퍼는 같은 루프 재진입이라 사용 불가).
            return await relay.request_async(op, payload)
        except OSError as relay_error:
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"P2P direct failed ({endpoint}: {direct_error}); "
                f"relay also failed: {relay_error}"
            ) from relay_error
