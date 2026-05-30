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

    @property
    def device_id(self) -> str:
        """대상 디바이스 ID."""
        return self._device_id

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

    async def _async_initialize(self) -> None:
        """GET /routing/{device_id}로 접속 주소를 조회한다."""
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
        address = data.get("address")
        if not address:
            self._deactivate("Routing response missing 'address' field")
            return

        self._peer_address = address
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
        self._check_active()
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/read", {"physical_path": physical_path})
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

    def delete(self, physical_path: str) -> None:
        """P2P POST /p2p/delete 요청으로 파일을 삭제한다."""
        self._check_active()
        self._io.run_coroutine(
            self._p2p_request("/p2p/delete", {"physical_path": physical_path})
        )

    def exists(self, physical_path: str) -> bool:
        """P2P POST /p2p/exists 요청으로 파일 존재 여부를 확인한다."""
        self._check_active()
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/exists", {"physical_path": physical_path})
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
        self._check_active()
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/list", {"physical_path": physical_path})
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
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"P2P request timed out ({endpoint}): {e}"
            ) from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise OSError(
                f"RemoteSource '{self._source_id}': "
                f"P2P connection failed ({endpoint}): {e}"
            ) from e

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
