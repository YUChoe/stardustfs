"""P2P 파일 서버.

다른 디바이스의 파일 요청을 처리하는 aiohttp 기반 경량 HTTP 서버.
JBODManager의 첫 번째 소스를 통해 로컬 스토리지에 접근한다.
"""

from __future__ import annotations

import base64
import logging
import os

import httpx
from aiohttp import web

from stardustlib.auth_client import AuthClient
from stardustlib.jbod_manager import JBODManager
from stardustlib.parity_store import ParityStore, QuotaExceededError

logger = logging.getLogger(__name__)

# 100MB 제한
MAX_WRITE_SIZE = 100 * 1024 * 1024
# 중앙 서버 토큰 검증 타임아웃
AUTH_VERIFY_TIMEOUT = 5.0


class P2PServer:
    """aiohttp 기반 P2P 파일 서버."""

    def __init__(
        self,
        jbod_manager: JBODManager,
        auth_client: AuthClient,
        port: int,
        server_url: str,
        parity_store: ParityStore | None = None,
    ) -> None:
        self._jbod_manager = jbod_manager
        self._auth_client = auth_client
        self._port = port
        self._server_url = server_url.rstrip("/")
        # 호스트 역할: 타 사용자 청크 암호문 보관소(없으면 replica op 비활성)
        self._parity_store = parity_store
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def _source_root(self) -> str:
        """첫 번째 소스의 루트 경로를 반환한다."""
        if self._jbod_manager.sources:
            return self._jbod_manager.sources[0].path
        return ""

    async def start(self) -> None:
        """서버를 시작한다. 포트 사용 중 시 에러 로깅 후 P2P 없이 계속."""
        self._app = web.Application(client_max_size=200 * 1024 * 1024)
        self._app.router.add_post("/p2p/read", self.handle_read)
        self._app.router.add_post("/p2p/write", self.handle_write)
        self._app.router.add_post("/p2p/delete", self.handle_delete)
        self._app.router.add_post("/p2p/list", self.handle_list)
        self._app.router.add_post("/p2p/exists", self.handle_exists)
        self._app.router.add_post("/p2p/mkdir", self.handle_mkdir)
        self._app.router.add_post("/p2p/rmdir", self.handle_rmdir)
        self._app.router.add_post("/p2p/space", self.handle_space)
        # 패리티(타 사용자 청크 보관) 교차 사용자 op
        self._app.router.add_post("/p2p/replica_store", self.handle_replica_store)
        self._app.router.add_post("/p2p/replica_fetch", self.handle_replica_fetch)
        self._app.router.add_post(
            "/p2p/replica_delete", self.handle_replica_delete
        )

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        try:
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            await self._site.start()
            logger.info("P2P server started on port %d", self._port)
        except OSError as e:
            logger.error(
                "P2P server failed to start on port %d: %s. "
                "Continuing without P2P.",
                self._port,
                e,
            )
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def stop(self) -> None:
        """서버를 중지한다."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("P2P server stopped")

    # --- 엔드포인트 핸들러 ---

    async def handle_read(self, request: web.Request) -> web.Response:
        """POST /p2p/read: 파일 읽기.

        요청에 share_token이 있으면 user_id 일치 검증 대신 중앙 서버에
        공유 토큰 검증을 위임한다 (읽기 전용 교차 사용자 공유, MVP5).
        """
        body = await self._parse_and_verify(request, allow_share_token=True)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_read(body)
        return web.json_response(result, status=status)

    async def handle_write(self, request: web.Request) -> web.Response:
        """POST /p2p/write: 파일 쓰기."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_write(body)
        return web.json_response(result, status=status)

    async def handle_delete(self, request: web.Request) -> web.Response:
        """POST /p2p/delete: 파일 삭제."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_delete(body)
        return web.json_response(result, status=status)

    async def handle_list(self, request: web.Request) -> web.Response:
        """POST /p2p/list: 디렉토리 목록."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_list(body)
        return web.json_response(result, status=status)

    async def handle_exists(self, request: web.Request) -> web.Response:
        """POST /p2p/exists: 경로 존재 여부."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_exists(body)
        return web.json_response(result, status=status)

    async def handle_mkdir(self, request: web.Request) -> web.Response:
        """POST /p2p/mkdir: 디렉토리 생성."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_mkdir(body)
        return web.json_response(result, status=status)

    async def handle_rmdir(self, request: web.Request) -> web.Response:
        """POST /p2p/rmdir: 디렉토리 삭제."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_rmdir(body)
        return web.json_response(result, status=status)

    async def handle_space(self, request: web.Request) -> web.Response:
        """POST /p2p/space: 용량 정보."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body
        status, result = self._op_space(body)
        return web.json_response(result, status=status)

    # --- 패리티(교차 사용자 청크 보관) 핸들러 ---

    async def handle_replica_store(self, request: web.Request) -> web.Response:
        """POST /p2p/replica_store: 타 사용자 청크 암호문 보관."""
        result = await self._parse_and_verify_any_user(request)
        if isinstance(result, web.Response):
            return result
        body, requester = result
        status, res = self._op_replica_store(body, requester)
        return web.json_response(res, status=status)

    async def handle_replica_fetch(self, request: web.Request) -> web.Response:
        """POST /p2p/replica_fetch: 보관 중인 청크를 소유자에게 반환."""
        result = await self._parse_and_verify_any_user(request)
        if isinstance(result, web.Response):
            return result
        body, requester = result
        status, res = self._op_replica_fetch(body, requester)
        return web.json_response(res, status=status)

    async def handle_replica_delete(self, request: web.Request) -> web.Response:
        """POST /p2p/replica_delete: 보관 중인 청크를 소유자 요청으로 삭제."""
        result = await self._parse_and_verify_any_user(request)
        if isinstance(result, web.Response):
            return result
        body, requester = result
        status, res = self._op_replica_delete(body, requester)
        return web.json_response(res, status=status)

    # --- 작업 디스패치 (릴레이 워커가 인증 없이 직접 호출) ---

    def dispatch(self, op: str, payload: dict) -> tuple[int, dict]:
        """op 이름으로 작업 로직을 실행하고 (status, result)를 반환한다.

        릴레이 워커가 사용한다. 인가는 중앙 서버(릴레이)가 user_id 일치로
        이미 보장하므로 여기서는 토큰 검증을 하지 않는다.
        share_token이 payload에 있어도 dispatch 경로에서는 무시한다(릴레이는
        같은 유저 디바이스 간만 허용되므로 공유 토큰 경로가 필요 없음).
        """
        # 패리티(복제본) op: 릴레이는 같은 user 간만 중개하므로 요청자=로컬 user.
        # 소유자=요청자 인가는 ParityStore가 청크 단위로 집행한다.
        if op in ("replica_store", "replica_fetch", "replica_delete"):
            requester = self._auth_client.user_id
            replica_map = {
                "replica_store": self._op_replica_store,
                "replica_fetch": self._op_replica_fetch,
                "replica_delete": self._op_replica_delete,
            }
            try:
                return replica_map[op](payload, requester)
            except Exception as e:  # noqa: BLE001
                logger.error("Relay replica op=%s 실패: %s", op, e, exc_info=True)
                return 500, {"error": "Internal error"}

        op_map = {
            "read": self._op_read,
            "write": self._op_write,
            "delete": self._op_delete,
            "list": self._op_list,
            "exists": self._op_exists,
            "mkdir": self._op_mkdir,
            "rmdir": self._op_rmdir,
            "space": self._op_space,
        }
        handler = op_map.get(op)
        if handler is None:
            return 400, {"error": f"Unknown op: {op}"}
        try:
            return handler(payload)
        except Exception as e:
            logger.error("Relay dispatch op=%s 실패: %s", op, e, exc_info=True)
            return 500, {"error": "Internal error"}

    async def dispatch_async(self, op: str, payload: dict) -> tuple[int, dict]:
        """릴레이 워커용 비동기 디스패치.

        복제본 op(replica_*)는 상호 호스팅이라 요청자가 로컬 사용자와 다를 수 있으므로,
        payload의 auth_token을 중앙 서버에 검증(same_user=False)해 요청자 user_id를
        도출하고 그 요청자로 ParityStore 인가를 집행한다(없거나 무효면 401). 그 외
        파일 op는 같은-user 릴레이가 서버에서 보장되므로 동기 dispatch에 위임한다.
        """
        replica_map = {
            "replica_store": self._op_replica_store,
            "replica_fetch": self._op_replica_fetch,
            "replica_delete": self._op_replica_delete,
        }
        handler = replica_map.get(op)
        if handler is None:
            return self.dispatch(op, payload)
        requester = await self._resolve_token_user(payload.get("auth_token"))
        if requester is None:
            return 401, {"error": "Invalid or missing auth_token"}
        try:
            return handler(payload, requester)
        except Exception as e:  # noqa: BLE001
            logger.error("Relay replica op=%s 실패: %s", op, e, exc_info=True)
            return 500, {"error": "Internal error"}

    async def _resolve_token_user(self, token: str | None) -> str | None:
        """auth_token을 /auth/verify로 검증해 user_id를 반환한다(무효/없음/도달불가면 None).

        교차 사용자 복제본 op의 요청자(=소유자) 신원 도출용. user_id 일치는 요구하지
        않는다(소유자=요청자 인가는 ParityStore가 청크 단위로 집행).
        """
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=AUTH_VERIFY_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._server_url}/auth/verify", json={"token": token}
                )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning("토큰 검증 실패(서버 도달 불가): %s", e)
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("valid"):
            return None
        return data.get("user_id")

    # --- 작업 로직 (handle_* 와 dispatch 가 공유) ---

    def _op_read(self, body: dict) -> tuple[int, dict]:
        """파일 읽기 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            return source

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        # 파일 존재 확인은 소스에 위임한다. LoopbackSource는 실제 데이터를
        # 동반 디렉토리(path + '.d')에 저장하므로 source.path로 직접 isfile을
        # 검사하면 안 된다(항상 404). source.read의 FileNotFoundError로 판별.
        try:
            data = source.read(physical_path)
        except FileNotFoundError:
            return 404, {"error": "File not found"}

        encoded = base64.b64encode(data).decode("ascii")
        return 200, {"data": encoded}

    def _op_write(self, body: dict) -> tuple[int, dict]:
        """파일 쓰기 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            source = self._jbod_manager.sources[0] if self._jbod_manager.sources else None
            if source is None:
                return 404, {"error": "No source available"}

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        data_b64 = body.get("data", "")
        try:
            data = base64.b64decode(data_b64)
        except Exception:
            return 400, {"error": "Invalid base64 data"}

        if len(data) > MAX_WRITE_SIZE:
            return 413, {"error": "Payload too large (max 100MB)"}

        # 상위 디렉토리 생성은 소스 구현에 위임한다(LoopbackSource는 동반
        # 디렉토리에 기록하며 내부에서 부모를 생성).
        source.write(physical_path, data)
        # 사용된 소스 id를 알려준다(evacuate가 메타데이터 source_id 갱신에 사용).
        return 200, {"bytes_written": len(data), "source_id": source.source_id}

    def _op_delete(self, body: dict) -> tuple[int, dict]:
        """파일 삭제 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            source = self._jbod_manager.sources[0] if self._jbod_manager.sources else None
            if source is None:
                return 404, {"error": "No source available"}

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        if not source.exists(physical_path):
            return 404, {"error": "File not found"}

        try:
            source.delete(physical_path)
        except FileNotFoundError:
            return 404, {"error": "File not found"}

        return 200, {"status": "deleted"}

    def _op_list(self, body: dict) -> tuple[int, dict]:
        """디렉토리 목록 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            return source

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        full_path = os.path.join(self._source_data_root(source), physical_path)
        if not os.path.isdir(full_path):
            return 404, {"error": "Directory not found"}

        entries = source.list_dir(physical_path)
        return 200, {"entries": entries}

    def _op_exists(self, body: dict) -> tuple[int, dict]:
        """경로 존재 여부 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            return source

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        exists = source.exists(physical_path)
        return 200, {"exists": exists}

    def _op_mkdir(self, body: dict) -> tuple[int, dict]:
        """디렉토리 생성 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            source = self._jbod_manager.sources[0] if self._jbod_manager.sources else None
            if source is None:
                return 404, {"error": "No source available"}

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        source.mkdir(physical_path)
        return 200, {"status": "created"}

    def _op_rmdir(self, body: dict) -> tuple[int, dict]:
        """디렉토리 삭제 로직."""
        source = self._select_source_or_error(body)
        if isinstance(source, tuple):
            source = self._jbod_manager.sources[0] if self._jbod_manager.sources else None
            if source is None:
                return 404, {"error": "No source available"}

        physical_path = body.get("physical_path", "")
        err = self._validate_path_err(physical_path, self._source_data_root(source))
        if err is not None:
            return err

        full_path = os.path.join(self._source_data_root(source), physical_path)
        if not os.path.isdir(full_path):
            return 404, {"error": "Directory not found"}

        source.rmdir(physical_path)
        return 200, {"status": "removed"}

    def _op_space(self, body: dict) -> tuple[int, dict]:
        """용량 정보 로직."""
        source = self._jbod_manager.sources[0]
        return 200, {
            "available": source.get_available_space(),
            "total": source.get_total_space(),
        }

    # --- 패리티 작업 로직 (소유자 = requester 인가는 ParityStore가 집행) ---

    def _op_replica_store(
        self, body: dict, requester: str
    ) -> tuple[int, dict]:
        """타 사용자 청크 암호문을 보관한다(소유자=requester)."""
        if self._parity_store is None:
            return 503, {"error": "Parity hosting not enabled"}
        chunk_id = body.get("chunk_id", "")
        try:
            data = base64.b64decode(body.get("data", ""))
        except Exception:
            return 400, {"error": "Invalid base64 data"}
        if len(data) > MAX_WRITE_SIZE:
            return 413, {"error": "Payload too large (max 100MB)"}
        try:
            self._parity_store.store(chunk_id, requester, data)
        except ValueError:
            return 400, {"error": "Invalid chunk_id"}
        except PermissionError:
            return 403, {"error": "Chunk owner mismatch"}
        except QuotaExceededError:
            return 507, {"error": "Insufficient storage (quota exceeded)"}
        return 200, {"bytes_written": len(data)}

    def _op_replica_fetch(
        self, body: dict, requester: str
    ) -> tuple[int, dict]:
        """보관 중인 청크 암호문을 소유자에게만 반환한다."""
        if self._parity_store is None:
            return 503, {"error": "Parity hosting not enabled"}
        chunk_id = body.get("chunk_id", "")
        try:
            data = self._parity_store.fetch(chunk_id, requester)
        except ValueError:
            return 400, {"error": "Invalid chunk_id"}
        except FileNotFoundError:
            return 404, {"error": "Chunk not found"}
        except PermissionError:
            return 403, {"error": "Not chunk owner"}
        return 200, {"data": base64.b64encode(data).decode("ascii")}

    def _op_replica_delete(
        self, body: dict, requester: str
    ) -> tuple[int, dict]:
        """보관 중인 청크를 소유자 요청으로 삭제한다(멱등)."""
        if self._parity_store is None:
            return 503, {"error": "Parity hosting not enabled"}
        chunk_id = body.get("chunk_id", "")
        try:
            self._parity_store.delete(chunk_id, requester)
        except ValueError:
            return 400, {"error": "Invalid chunk_id"}
        except PermissionError:
            return 403, {"error": "Not chunk owner"}
        return 200, {"status": "deleted"}

    # --- 내부 헬퍼 ---

    async def _parse_and_verify(
        self, request: web.Request, allow_share_token: bool = False
    ) -> dict | web.Response:
        """요청 본문을 파싱하고 인가를 검증한다.

        기본 인가: auth_token JWT를 중앙 서버에 위임 검증 + user_id 일치 확인.
        allow_share_token=True이고 요청에 share_token이 있으면, user_id 일치
        검증 대신 중앙 서버에 공유 토큰을 검증 위임한다 (요청 physical_path가
        토큰에 묶인 경로와 일치해야 함).

        성공 시 파싱된 body dict를 반환, 실패 시 에러 Response를 반환.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400
            )

        # 공유 토큰 경로 (읽기 전용): user_id 검증 우회, 경로 격리 검증
        share_token = body.get("share_token")
        if allow_share_token and share_token:
            physical_path = body.get("physical_path", "")
            verify_result = await self._verify_share_token(
                share_token, physical_path
            )
            if isinstance(verify_result, web.Response):
                return verify_result
            return body

        auth_token = body.get("auth_token")
        if not auth_token:
            return web.json_response(
                {"error": "Missing auth_token"}, status=401
            )

        # 중앙 서버에 토큰 검증 위임
        verify_result = await self._verify_token(auth_token)
        if isinstance(verify_result, web.Response):
            return verify_result

        return body

    async def _parse_and_verify_any_user(
        self, request: web.Request
    ) -> tuple[dict, str] | web.Response:
        """패리티 op용: 토큰을 검증하되 user_id 일치는 요구하지 않는다.

        호스트는 타 사용자의 청크를 보관하므로 요청자가 로컬 사용자와 달라도
        된다. 소유자=요청자 인가는 ParityStore가 청크 단위로 집행한다.
        성공 시 (body, requester_user_id), 실패 시 에러 Response를 반환.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        auth_token = body.get("auth_token")
        if not auth_token:
            return web.json_response({"error": "Missing auth_token"}, status=401)
        verify_result = await self._verify_token(auth_token, same_user=False)
        if isinstance(verify_result, web.Response):
            return verify_result
        requester = verify_result.get("user_id")
        if not requester:
            return web.json_response(
                {"error": "Invalid token (no user_id)"}, status=401
            )
        return body, requester

    async def _verify_share_token(
        self, share_token: str, physical_path: str
    ) -> dict | web.Response:
        """중앙 서버 POST /shares/{token}/verify로 공유 토큰을 검증한다.

        유효(존재·미만료)하고 physical_path가 토큰에 묶인 경로와 일치하면
        검증 결과 dict, 아니면 에러 Response를 반환한다.
        - 무효/만료: 401
        - 경로 불일치: 403
        """
        try:
            async with httpx.AsyncClient(
                timeout=AUTH_VERIFY_TIMEOUT
            ) as client:
                resp = await client.post(
                    f"{self._server_url}/shares/{share_token}/verify",
                    json={"physical_path": physical_path},
                )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(
                "Share verification failed (server unreachable): %s", e
            )
            return web.json_response(
                {"error": "Auth service unavailable"}, status=503
            )

        if resp.status_code != 200:
            return web.json_response(
                {"error": "Invalid or expired share token"}, status=401
            )

        data = resp.json()
        if not data.get("valid"):
            # device_id가 응답에 있으면 토큰은 존재하나 경로 불일치 → 403
            if data.get("device_id"):
                return web.json_response(
                    {"error": "Share token path mismatch"}, status=403
                )
            return web.json_response(
                {"error": "Invalid or expired share token"}, status=401
            )

        return data

    async def _verify_token(
        self, token: str, same_user: bool = True
    ) -> dict | web.Response:
        """중앙 서버 POST /auth/verify로 토큰을 검증한다.

        same_user=True(기본)이면 토큰의 user_id가 로컬 사용자와 일치해야 한다.
        패리티 op처럼 타 사용자 접근이 정당한 경우 same_user=False로 호출한다.
        성공 시 검증 결과 dict, 실패 시 에러 Response를 반환.
        """
        try:
            async with httpx.AsyncClient(
                timeout=AUTH_VERIFY_TIMEOUT
            ) as client:
                resp = await client.post(
                    f"{self._server_url}/auth/verify",
                    json={"token": token},
                )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(
                "Auth verification failed (server unreachable): %s", e
            )
            return web.json_response(
                {"error": "Auth service unavailable"}, status=503
            )

        if resp.status_code != 200:
            return web.json_response(
                {"error": "Invalid or expired token"}, status=401
            )

        data = resp.json()
        if not data.get("valid"):
            return web.json_response(
                {"error": "Invalid or expired token"}, status=401
            )

        # user_id 일치 확인 (패리티 op 등 교차 사용자 접근에서는 생략)
        if same_user:
            remote_user_id = data.get("user_id")
            local_user_id = self._auth_client.user_id
            if remote_user_id != local_user_id:
                return web.json_response(
                    {"error": "User ID mismatch"}, status=403
                )

        return data

    def _source_data_root(self, source) -> str:
        """소스의 실제 데이터 루트를 반환한다.

        LoopbackSource는 동반 디렉토리(path + '.d')에 실제 파일을 저장하므로
        traversal 검증의 기준 루트도 그 디렉토리여야 한다. 그 외 소스는
        source.path를 사용한다.
        """
        companion = getattr(source, "_companion_dir", None)
        if companion:
            return companion
        return source.path

    def _select_source_or_error(self, body: dict):
        """요청 body의 source_id로 소스를 선택한다.

        source_id가 있으면 그 소스를, 없으면 첫 소스(구버전 호환)를 반환한다.
        실패 시 (status, result) 튜플을 반환한다.
        """
        source_id = body.get("source_id")
        if source_id:
            src = self._jbod_manager._get_source_by_id(source_id)
            if src is None:
                return 404, {"error": "Source not found"}
            return src
        if self._jbod_manager.sources:
            return self._jbod_manager.sources[0]
        return 404, {"error": "No source available"}

    def _validate_path_err(
        self, physical_path: str, source_root: str | None = None
    ) -> tuple[int, dict] | None:
        """Path traversal 방지 검증.

        ".." 세그먼트 포함 또는 정규화 후 소스 루트 외부 참조 시 (400, ...)을
        반환한다. source_root가 None이면 첫 소스 루트를 사용한다.
        유효하면 None 반환.
        """
        if not physical_path:
            return 400, {"error": "Missing physical_path"}

        # ".." 세그먼트 검사
        if ".." in physical_path.replace("\\", "/").split("/"):
            return 400, {"error": "Path traversal detected"}

        # 정규화 후 소스 루트 내부인지 확인
        root = source_root if source_root is not None else self._source_root
        source_root_norm = os.path.normpath(root)
        resolved = os.path.normpath(
            os.path.join(source_root_norm, physical_path)
        )

        if not (
            resolved == source_root_norm
            or resolved.startswith(source_root_norm + os.sep)
        ):
            return 400, {"error": "Path traversal detected"}

        return None

    # 하위 호환 별칭 (기존 테스트가 직접 참조)
    def _validate_path(
        self, physical_path: str, source_root: str | None = None
    ) -> tuple[int, dict] | None:
        """_validate_path_err의 별칭 (유효 시 None, 거부 시 (status, dict))."""
        return self._validate_path_err(physical_path, source_root)
