"""원격 디바이스의 스토리지에 접근하는 StorageSource 구현체.

다른 PC의 P2P Server에 HTTP 요청을 보내 파일 I/O를 수행한다.
StorageSource ABC의 동기 인터페이스를 유지하면서 내부적으로
httpx.AsyncClient를 사용한다.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
import threading
from typing import Any

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError
from stardustlib.storage_source import StorageSource

logger = logging.getLogger(__name__)

# 리모트 전송 청크 크기(4 MiB). rudp 단일 메시지 한계(frag_count u16 × 1200B ≈
# 78.6MB)와 홀더 MAX_WRITE_SIZE(100MB) 내. 이를 넘는 파일은 청크로 나눠 전송한다.
REMOTE_CHUNK_SIZE = 4 * 1024 * 1024

# 직접 TCP 연결(connect) 타임아웃(초). 디바이스가 광고하는 주소는 사설(LAN) 주소이므로
# 직접 TCP는 같은 LAN에서만 성립한다. 다른 네트워크면 SYN 무응답으로 대기만 하다가
# 홀펀칭 UDP로 내려가므로 연결 단계만 짧게 잡는다(읽기/쓰기 타임아웃은 대용량 LAN
# 전송을 막지 않도록 기존 값 유지).
DIRECT_CONNECT_TIMEOUT = 2.0


def direct_tcp_viable(peer_address: str | None) -> bool:
    """직접 TCP를 시도할 가치가 있는지 판단한다.

    디바이스는 LAN 주소를 광고한다(사용자에게 포트포워딩을 기대하지 않으므로 공인
    주소 보정은 하지 않는다). 따라서 상대가 사설 주소인데 내 서브넷이 아니면 도달
    가능성이 없어, 시도하지 않고 곧바로 홀펀칭 UDP로 내려간다.

    판단이 애매하거나(호스트명 등) 상대가 공인 주소면 True를 반환한다 — 짧은 연결
    타임아웃이 안전망이 된다.
    """
    if not peer_address:
        return False
    host = peer_address.rsplit(":", 1)[0]
    try:
        peer = ipaddress.ip_address(host)
    except ValueError:
        return True  # 호스트명 등 — 판단 불가, 짧은 타임아웃으로 시도
    if peer.is_loopback:
        return True  # 같은 머신(로컬/테스트)
    if peer.is_global:
        return True  # 공인 주소(상시 홀더 등) — 시도할 가치 있음
    # 사설/CGNAT: 내 인터페이스와 같은 서브넷일 때만 도달 가능
    return _in_same_subnet(peer)


def _in_same_subnet(peer) -> bool:
    """상대 사설 주소가 내 로컬 인터페이스와 같은 /24 서브넷인지 확인한다.

    /24보다 넓은 사내망(예: /16)에서는 False가 되어 홀펀칭 UDP로 내려간다 — 느릴 수
    있으나 동작에는 문제가 없다(보수적 판단).
    """
    local = _local_ip()
    if local is None:
        return False
    try:
        my_net = ipaddress.ip_network(f"{local}/24", strict=False)
    except ValueError:
        return False
    return peer in my_net


def _local_ip() -> str | None:
    """기본 경로의 로컬 인터페이스 IP를 반환한다(실패 시 None)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except OSError:
        return None


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
        self, physical_path: str, source_id: str | None,
        file_size: int | None = None,
    ) -> bytes:
        """원격 디바이스의 특정 소스(source_id)에서 파일을 읽는다.

        source_id가 None이면 원격 첫 소스를 사용한다(구버전 호환).
        디바이스 단위 라우팅(Device_Router)에서 원격 소스 ID를 지정해 호출한다.
        file_size(평문 크기)가 REMOTE_CHUNK_SIZE를 초과하면(암호문도 초과) 범위 분할
        읽기로 받아 한계(rudp/100MB)를 피한다. 그 이하는 단일 read(하위호환).
        """
        self._check_active()
        if file_size is not None and file_size > REMOTE_CHUNK_SIZE:
            return self._read_chunked(physical_path, source_id)
        payload: dict[str, Any] = {"physical_path": physical_path}
        if source_id is not None:
            payload["source_id"] = source_id
        result = self._io.run_coroutine(
            self._p2p_request("/p2p/read", payload)
        )
        # 응답 본문에서 base64 디코딩된 data를 반환
        return base64.b64decode(result["data"])

    def _read_chunked(
        self, physical_path: str, source_id: str | None
    ) -> bytes:
        """REMOTE_CHUNK_SIZE 단위 범위 읽기로 전체를 이어붙인다.

        암호문 실제 크기를 모르므로(평문 file_size와 다름) 짧은 읽기(요청보다 적게
        반환)를 EOF로 간주한다. 정확히 배수면 마지막에 빈 청크 1회로 종료한다.
        """
        parts: list[bytes] = []
        offset = 0
        while True:
            payload: dict[str, Any] = {
                "physical_path": physical_path,
                "offset": offset,
                "length": REMOTE_CHUNK_SIZE,
            }
            if source_id is not None:
                payload["source_id"] = source_id
            result = self._io.run_coroutine(
                self._p2p_request("/p2p/read_chunk", payload)
            )
            chunk = base64.b64decode(result["data"])
            parts.append(chunk)
            offset += len(chunk)
            if len(chunk) < REMOTE_CHUNK_SIZE:
                break
        return b"".join(parts)

    def write(self, physical_path: str, data: bytes) -> None:
        """P2P POST /p2p/write 요청으로 파일을 기록한다(대용량은 청크 분할)."""
        self._check_active()
        if len(data) > REMOTE_CHUNK_SIZE:
            self._push_chunked(physical_path, data)
            return
        encoded = base64.b64encode(data).decode("ascii")
        self._io.run_coroutine(
            self._p2p_request(
                "/p2p/write",
                {"physical_path": physical_path, "data": encoded},
            )
        )

    def push_blob(self, physical_path: str, data: bytes) -> str:
        """at-rest 암호문 블록을 원격에 기록하고 사용된 원격 source_id를 반환한다.

        스필오버/evacuate에서 사용한다. 데이터는 이미 암호문이므로 재암호화하지 않는다
        (zero-knowledge 유지). REMOTE_CHUNK_SIZE 초과면 청크로 나눠 push한다.
        """
        self._check_active()
        if len(data) > REMOTE_CHUNK_SIZE:
            return self._push_chunked(physical_path, data)
        encoded = base64.b64encode(data).decode("ascii")
        result = self._io.run_coroutine(
            self._p2p_request(
                "/p2p/write",
                {"physical_path": physical_path, "data": encoded},
            )
        )
        return result.get("source_id", "")

    def _push_chunked(self, physical_path: str, data: bytes) -> str:
        """대용량 데이터를 REMOTE_CHUNK_SIZE 청크로 순차 push하고 source_id를 반환한다.

        첫 청크(offset=0)에서 홀더가 total_size로 소스를 선택하고 source_id를 응답하면,
        이후 청크는 그 source_id로 같은 파일에 이어 쓴다. 중간 실패 시 홀더의 부분
        파일을 삭제(베스트에포트)하고 예외를 전파한다(조용한 손실 없음).
        """
        total = len(data)
        source_id = ""
        try:
            for offset in range(0, total, REMOTE_CHUNK_SIZE):
                chunk = data[offset:offset + REMOTE_CHUNK_SIZE]
                payload: dict[str, Any] = {
                    "physical_path": physical_path,
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "offset": offset,
                    "total_size": total,
                }
                if source_id:
                    payload["source_id"] = source_id
                result = self._io.run_coroutine(
                    self._p2p_request("/p2p/write_chunk", payload)
                )
                if not source_id:
                    source_id = result.get("source_id", "")
        except Exception:
            try:
                self._io.run_coroutine(
                    self._p2p_request(
                        "/p2p/delete", {"physical_path": physical_path}
                    )
                )
            except Exception:  # noqa: BLE001 — 정리 실패는 무시
                pass
            raise
        return source_id

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

    def announce_backup(self, virtual_path: str) -> bool:
        """이 원격 device에 백업 수행을 위임한다(POST /p2p/backup_announce).

        청크를 보관한 기기가 직접 올리게 해서, 데이터를 갖지 않은 기기가 원본을
        릴레이로 당겨오는 왕복을 없앤다. 도달 불가·스케줄러 미가동이면 False.
        """
        self._check_active()
        try:
            self._io.run_coroutine(
                self._p2p_request(
                    "/p2p/backup_announce", {"virtual_path": virtual_path}
                )
            )
            return True
        except Exception as e:  # noqa: BLE001 — 위임 실패는 호출자가 알린다
            logger.info(
                "백업 위임 실패(device=%s): %s", self._device_id, e
            )
            return False

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

        # 도달 가능성이 없는 주소(다른 네트워크의 사설 IP)면 직접 TCP를 건너뛴다.
        if not direct_tcp_viable(self._peer_address):
            logger.debug(
                "직접 TCP 건너뜀(%s, 도달 불가 주소 %s) — UDP/릴레이로 진행: %s",
                endpoint, self._peer_address, self._device_id,
            )
            return await self._fallback(
                endpoint, request_body,
                OSError(f"direct TCP not viable for {self._peer_address}"),
            )

        # 연결 단계만 짧게(읽기/쓰기는 대용량 LAN 전송을 위해 기존 타임아웃 유지).
        timeout = httpx.Timeout(self._timeout, connect=DIRECT_CONNECT_TIMEOUT)
        try:
            response = await self._client.post(
                url, json=request_body, timeout=timeout
            )
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
        "/p2p/write_chunk": "write_chunk",
        "/p2p/read_chunk": "read_chunk",
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
