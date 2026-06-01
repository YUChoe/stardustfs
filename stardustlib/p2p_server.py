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
    ) -> None:
        self._jbod_manager = jbod_manager
        self._auth_client = auth_client
        self._port = port
        self._server_url = server_url.rstrip("/")
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

        source = self._select_source(body)
        if isinstance(source, web.Response):
            return source

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path, source.path)
        if err is not None:
            return err

        full_path = os.path.join(source.path, physical_path)

        if not os.path.isfile(full_path):
            return web.json_response(
                {"error": "File not found"}, status=404
            )

        try:
            data = source.read(physical_path)
        except FileNotFoundError:
            return web.json_response(
                {"error": "File not found"}, status=404
            )

        encoded = base64.b64encode(data).decode("ascii")
        return web.json_response({"data": encoded})

    async def handle_write(self, request: web.Request) -> web.Response:
        """POST /p2p/write: 파일 쓰기."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path)
        if err is not None:
            return err

        data_b64 = body.get("data", "")
        try:
            data = base64.b64decode(data_b64)
        except Exception:
            return web.json_response(
                {"error": "Invalid base64 data"}, status=400
            )

        if len(data) > MAX_WRITE_SIZE:
            return web.json_response(
                {"error": "Payload too large (max 100MB)"}, status=413
            )

        source = self._jbod_manager.sources[0]
        # 상위 디렉토리 자동 생성
        full_path = os.path.join(source.path, physical_path)
        parent = os.path.dirname(full_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        source.write(physical_path, data)
        return web.json_response({"bytes_written": len(data)})

    async def handle_delete(self, request: web.Request) -> web.Response:
        """POST /p2p/delete: 파일 삭제."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path)
        if err is not None:
            return err

        source = self._jbod_manager.sources[0]
        full_path = os.path.join(source.path, physical_path)

        if not os.path.exists(full_path):
            return web.json_response(
                {"error": "File not found"}, status=404
            )

        try:
            source.delete(physical_path)
        except FileNotFoundError:
            return web.json_response(
                {"error": "File not found"}, status=404
            )

        return web.json_response({"status": "deleted"})

    async def handle_list(self, request: web.Request) -> web.Response:
        """POST /p2p/list: 디렉토리 목록."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        source = self._select_source(body)
        if isinstance(source, web.Response):
            return source

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path, source.path)
        if err is not None:
            return err

        full_path = os.path.join(source.path, physical_path)

        if not os.path.isdir(full_path):
            return web.json_response(
                {"error": "Directory not found"}, status=404
            )

        entries = source.list_dir(physical_path)
        return web.json_response({"entries": entries})

    async def handle_exists(self, request: web.Request) -> web.Response:
        """POST /p2p/exists: 경로 존재 여부."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        source = self._select_source(body)
        if isinstance(source, web.Response):
            return source

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path, source.path)
        if err is not None:
            return err

        exists = source.exists(physical_path)
        return web.json_response({"exists": exists})

    async def handle_mkdir(self, request: web.Request) -> web.Response:
        """POST /p2p/mkdir: 디렉토리 생성."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path)
        if err is not None:
            return err

        source = self._jbod_manager.sources[0]
        source.mkdir(physical_path)
        return web.json_response({"status": "created"})

    async def handle_rmdir(self, request: web.Request) -> web.Response:
        """POST /p2p/rmdir: 디렉토리 삭제."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        physical_path = body.get("physical_path", "")
        err = self._validate_path(physical_path)
        if err is not None:
            return err

        source = self._jbod_manager.sources[0]
        full_path = os.path.join(source.path, physical_path)

        if not os.path.isdir(full_path):
            return web.json_response(
                {"error": "Directory not found"}, status=404
            )

        source.rmdir(physical_path)
        return web.json_response({"status": "removed"})

    async def handle_space(self, request: web.Request) -> web.Response:
        """POST /p2p/space: 용량 정보."""
        body = await self._parse_and_verify(request)
        if isinstance(body, web.Response):
            return body

        source = self._jbod_manager.sources[0]
        return web.json_response({
            "available": source.get_available_space(),
            "total": source.get_total_space(),
        })

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
        self, token: str
    ) -> dict | web.Response:
        """중앙 서버 POST /auth/verify로 토큰을 검증한다.

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

        # user_id 일치 확인
        remote_user_id = data.get("user_id")
        local_user_id = self._auth_client.user_id
        if remote_user_id != local_user_id:
            return web.json_response(
                {"error": "User ID mismatch"}, status=403
            )

        return data

    def _select_source(self, body: dict):
        """요청 body의 source_id로 소스를 선택한다.

        source_id가 있으면 그 소스를, 없으면 첫 소스(구버전 호환)를 반환한다.
        존재하지 않는 source_id이거나 소스가 하나도 없으면 에러 Response를 반환한다.
        """
        source_id = body.get("source_id")
        if source_id:
            src = self._jbod_manager._get_source_by_id(source_id)
            if src is None:
                return web.json_response(
                    {"error": "Source not found"}, status=404
                )
            return src
        if self._jbod_manager.sources:
            return self._jbod_manager.sources[0]
        return web.json_response({"error": "No source available"}, status=404)

    def _validate_path(
        self, physical_path: str, source_root: str | None = None
    ) -> web.Response | None:
        """Path traversal 방지 검증.

        ".." 세그먼트 포함 또는 정규화 후 소스 루트 외부 참조 시 400 반환.
        source_root가 None이면 첫 소스 루트(_source_root)를 사용한다.
        유효하면 None 반환.
        """
        if not physical_path:
            return web.json_response(
                {"error": "Missing physical_path"}, status=400
            )

        # ".." 세그먼트 검사
        if ".." in physical_path.replace("\\", "/").split("/"):
            return web.json_response(
                {"error": "Path traversal detected"}, status=400
            )

        # 정규화 후 소스 루트 내부인지 확인
        root = source_root if source_root is not None else self._source_root
        source_root_norm = os.path.normpath(root)
        resolved = os.path.normpath(
            os.path.join(source_root_norm, physical_path)
        )

        # 소스 루트로 시작하는지 확인 (os.sep 추가로 정확한 접두사 매칭)
        if not (
            resolved == source_root_norm
            or resolved.startswith(source_root_norm + os.sep)
        ):
            return web.json_response(
                {"error": "Path traversal detected"}, status=400
            )

        return None
