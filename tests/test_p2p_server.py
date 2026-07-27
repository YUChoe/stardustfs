"""P2PServer 단위 테스트."""

from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aiohttp import web

from stardustlib.auth_client import AuthClient
from stardustlib.storage_pool import StoragePool
from stardustlib.p2p_server import P2PServer
from stardustlib.storage_source import DirectorySource, LoopbackSource


@pytest.fixture
def tmp_source_dir(tmp_path):
    """임시 소스 디렉토리를 생성한다."""
    source_dir = tmp_path / "source_root"
    source_dir.mkdir()
    return str(source_dir)


@pytest.fixture
def directory_source(tmp_source_dir):
    """DirectorySource를 생성하고 초기화한다."""
    source = DirectorySource(source_id="vol1", path=tmp_source_dir)
    source.initialize()
    return source


@pytest.fixture
def mock_metadata_store():
    """MetadataStore mock."""
    return MagicMock()


@pytest.fixture
def mock_auth_client():
    """AuthClient mock."""
    client = MagicMock(spec=AuthClient)
    client.user_id = "test-user-123"
    return client


@pytest.fixture
def storage_pool(directory_source, mock_metadata_store):
    """StoragePool를 생성한다."""
    return StoragePool(
        sources=[directory_source],
        metadata_store=mock_metadata_store,
    )


@pytest.fixture
def p2p_server(storage_pool, mock_auth_client):
    """P2PServer 인스턴스를 생성한다."""
    return P2PServer(
        storage_pool=storage_pool,
        auth_client=mock_auth_client,
        port=9999,
        server_url="http://localhost:8000",
    )


@pytest.fixture
def app(p2p_server):
    """aiohttp Application을 생성한다."""
    # client_max_size를 200MB로 설정하여 413 테스트가 서버 로직에서 처리되도록 함
    application = web.Application(client_max_size=200 * 1024 * 1024)
    application.router.add_post("/p2p/read", p2p_server.handle_read)
    application.router.add_post("/p2p/write", p2p_server.handle_write)
    application.router.add_post("/p2p/delete", p2p_server.handle_delete)
    application.router.add_post("/p2p/list", p2p_server.handle_list)
    application.router.add_post("/p2p/exists", p2p_server.handle_exists)
    application.router.add_post("/p2p/mkdir", p2p_server.handle_mkdir)
    application.router.add_post("/p2p/rmdir", p2p_server.handle_rmdir)
    application.router.add_post("/p2p/space", p2p_server.handle_space)
    return application


@pytest_asyncio.fixture
async def client(app, aiohttp_client):
    """테스트 클라이언트를 생성한다."""
    return await aiohttp_client(app)


def _mock_verify_success():
    """토큰 검증 성공 mock을 반환한다."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "valid": True,
        "user_id": "test-user-123",
    }
    return mock_resp


def _mock_verify_invalid():
    """토큰 검증 실패 (invalid) mock."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"valid": False}
    return mock_resp


def _mock_verify_wrong_user():
    """토큰 검증 성공이지만 user_id 불일치 mock."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "valid": True,
        "user_id": "other-user-456",
    }
    return mock_resp


# --- 인증 테스트 ---


@pytest.mark.asyncio
async def test_missing_auth_token(client):
    """auth_token 누락 시 401 반환."""
    resp = await client.post(
        "/p2p/read", json={"physical_path": "test.txt"}
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_invalid_token(client):
    """유효하지 않은 토큰 시 401 반환."""
    import httpx as _httpx

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_invalid()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={"physical_path": "test.txt", "auth_token": "bad-token"},
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_user_id_mismatch(client):
    """user_id 불일치 시 403 반환."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_wrong_user()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={"physical_path": "test.txt", "auth_token": "some-token"},
        )
        assert resp.status == 403


@pytest.mark.asyncio
async def test_auth_server_unreachable(client):
    """중앙 서버 접근 불가 시 503 반환."""
    import httpx as _httpx

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = _httpx.TimeoutException("timeout")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={"physical_path": "test.txt", "auth_token": "some-token"},
        )
        assert resp.status == 503


# --- Path traversal 테스트 ---


@pytest.mark.asyncio
async def test_path_traversal_dotdot(client):
    """.. 포함 경로 시 400 반환."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={
                "physical_path": "../etc/passwd",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_path_traversal_embedded(client):
    """중간에 .. 포함된 경로 시 400 반환."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={
                "physical_path": "subdir/../../secret.txt",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 400


# --- /p2p/read 테스트 ---


@pytest.mark.asyncio
async def test_read_success(client, tmp_source_dir):
    """정상 파일 읽기."""
    # 테스트 파일 생성
    test_file = os.path.join(tmp_source_dir, "hello.txt")
    with open(test_file, "wb") as f:
        f.write(b"Hello, P2P!")

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={"physical_path": "hello.txt", "auth_token": "valid-token"},
        )
        assert resp.status == 200
        data = await resp.json()
        decoded = base64.b64decode(data["data"])
        assert decoded == b"Hello, P2P!"


@pytest.mark.asyncio
async def test_read_file_not_found(client):
    """존재하지 않는 파일 읽기 시 404."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/read",
            json={
                "physical_path": "nonexistent.txt",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 404


# --- /p2p/write 테스트 ---


@pytest.mark.asyncio
async def test_write_success(client, tmp_source_dir):
    """정상 파일 쓰기."""
    content = b"Written via P2P"
    encoded = base64.b64encode(content).decode("ascii")

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/write",
            json={
                "physical_path": "subdir/new_file.txt",
                "data": encoded,
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["bytes_written"] == len(content)

    # 파일이 실제로 생성되었는지 확인
    written_path = os.path.join(tmp_source_dir, "subdir", "new_file.txt")
    assert os.path.isfile(written_path)
    with open(written_path, "rb") as f:
        assert f.read() == content


@pytest.mark.asyncio
async def test_write_payload_too_large(client):
    """100MB 초과 시 413 반환."""
    # 100MB + 1 byte 데이터 (base64 인코딩)
    large_data = base64.b64encode(b"x" * (100 * 1024 * 1024 + 1)).decode(
        "ascii"
    )

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/write",
            json={
                "physical_path": "big_file.bin",
                "data": large_data,
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 413


# --- /p2p/delete 테스트 ---


@pytest.mark.asyncio
async def test_delete_success(client, tmp_source_dir):
    """정상 파일 삭제."""
    test_file = os.path.join(tmp_source_dir, "to_delete.txt")
    with open(test_file, "wb") as f:
        f.write(b"delete me")

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/delete",
            json={
                "physical_path": "to_delete.txt",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 200

    assert not os.path.exists(test_file)


@pytest.mark.asyncio
async def test_delete_not_found(client):
    """존재하지 않는 파일 삭제 시 404."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/delete",
            json={
                "physical_path": "ghost.txt",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 404


# --- /p2p/list 테스트 ---


@pytest.mark.asyncio
async def test_list_success(client, tmp_source_dir):
    """디렉토리 목록 조회."""
    # 파일 생성
    with open(os.path.join(tmp_source_dir, "a.txt"), "wb") as f:
        f.write(b"a")
    with open(os.path.join(tmp_source_dir, "b.txt"), "wb") as f:
        f.write(b"b")
    os.makedirs(os.path.join(tmp_source_dir, "subdir"), exist_ok=True)

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/list",
            json={"physical_path": ".", "auth_token": "valid-token"},
        )
        assert resp.status == 200
        data = await resp.json()
        entries = sorted(data["entries"])
        assert "a.txt" in entries
        assert "b.txt" in entries
        assert "subdir" in entries


# --- /p2p/exists 테스트 ---


@pytest.mark.asyncio
async def test_exists_true(client, tmp_source_dir):
    """존재하는 파일 확인."""
    with open(os.path.join(tmp_source_dir, "exists.txt"), "wb") as f:
        f.write(b"yes")

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/exists",
            json={
                "physical_path": "exists.txt",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["exists"] is True


@pytest.mark.asyncio
async def test_exists_false(client):
    """존재하지 않는 파일 확인."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/exists",
            json={
                "physical_path": "nope.txt",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["exists"] is False


# --- /p2p/mkdir 테스트 ---


@pytest.mark.asyncio
async def test_mkdir_success(client, tmp_source_dir):
    """디렉토리 생성."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/mkdir",
            json={
                "physical_path": "new_dir/nested",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 200

    assert os.path.isdir(os.path.join(tmp_source_dir, "new_dir", "nested"))


# --- /p2p/rmdir 테스트 ---


@pytest.mark.asyncio
async def test_rmdir_success(client, tmp_source_dir):
    """디렉토리 삭제."""
    dir_path = os.path.join(tmp_source_dir, "remove_me")
    os.makedirs(dir_path)
    with open(os.path.join(dir_path, "file.txt"), "wb") as f:
        f.write(b"content")

    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/rmdir",
            json={
                "physical_path": "remove_me",
                "auth_token": "valid-token",
            },
        )
        assert resp.status == 200

    assert not os.path.exists(dir_path)


# --- /p2p/space 테스트 ---


@pytest.mark.asyncio
async def test_space_info(client):
    """용량 정보 조회."""
    with patch("stardustlib.p2p_server.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_verify_success()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/p2p/space",
            json={"auth_token": "valid-token"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "available" in data
        assert "total" in data
        assert data["available"] > 0
        assert data["total"] > 0


class TestDispatch:
    """dispatch(op, payload) — 릴레이 워커용 작업 디스패치."""

    def test_dispatch_read(self, p2p_server, tmp_source_dir):
        """read op가 파일 데이터를 base64로 반환한다."""
        data = b"dispatch read"
        with open(os.path.join(tmp_source_dir, "d.bin"), "wb") as f:
            f.write(data)

        status, result = p2p_server.dispatch(
            "read", {"physical_path": "d.bin", "source_id": "vol1"}
        )
        assert status == 200
        assert base64.b64decode(result["data"]) == data

    def test_dispatch_read_not_found(self, p2p_server):
        """없는 파일은 404."""
        status, result = p2p_server.dispatch(
            "read", {"physical_path": "missing.bin"}
        )
        assert status == 404
        assert "error" in result

    def test_dispatch_write_then_read(self, p2p_server, tmp_source_dir):
        """write op로 기록 후 read로 동일 데이터를 읽는다."""
        payload_b64 = base64.b64encode(b"written via dispatch").decode("ascii")
        status, result = p2p_server.dispatch(
            "write", {"physical_path": "w.bin", "data": payload_b64}
        )
        assert status == 200
        assert result["bytes_written"] == len(b"written via dispatch")

        status2, result2 = p2p_server.dispatch(
            "read", {"physical_path": "w.bin"}
        )
        assert status2 == 200
        assert base64.b64decode(result2["data"]) == b"written via dispatch"

    def test_dispatch_unknown_op(self, p2p_server):
        """알 수 없는 op는 400."""
        status, result = p2p_server.dispatch("frobnicate", {})
        assert status == 400
        assert "error" in result

    def test_dispatch_path_traversal(self, p2p_server):
        """traversal 경로는 400."""
        status, result = p2p_server.dispatch(
            "read", {"physical_path": "../escape.bin"}
        )
        assert status == 400

    def test_dispatch_exists(self, p2p_server, tmp_source_dir):
        """exists op."""
        with open(os.path.join(tmp_source_dir, "e.bin"), "wb") as f:
            f.write(b"x")
        status, result = p2p_server.dispatch(
            "exists", {"physical_path": "e.bin", "source_id": "vol1"}
        )
        assert status == 200
        assert result["exists"] is True


class TestDispatchAsyncReplica:
    """dispatch_async — 교차 사용자 복제본 op의 요청자(소유자) 도출 + ParityStore 인가."""

    _CHUNK = "a" * 64  # 유효한 SHA-256 형식 chunk_id

    @pytest.fixture
    def parity_server(self, directory_source, mock_auth_client, tmp_path):
        """보관 청크는 실제 소스+DB에 놓이므로 진짜 MetadataStore를 쓴다."""
        from stardustlib.metadata_store import MetadataStore
        from stardustlib.parity_store import ParityStore

        meta = MetadataStore(str(tmp_path / "parity-meta.db"), b"k" * 32)
        meta.initialize()
        pool = StoragePool(sources=[directory_source], metadata_store=meta)
        store = ParityStore(pool, meta)
        return P2PServer(
            storage_pool=pool,
            auth_client=mock_auth_client,
            port=9999,
            server_url="http://localhost:8000",
            parity_store=store,
        )

    def _set_requester(self, server, user):
        async def _resolve(_token):
            return user
        server._resolve_token_user = _resolve

    @pytest.mark.asyncio
    async def test_store_then_fetch_as_owner(self, parity_server):
        data = base64.b64encode(b"cipher-chunk").decode("ascii")
        self._set_requester(parity_server, "ownerA")
        status, _ = await parity_server.dispatch_async(
            "replica_store",
            {"chunk_id": self._CHUNK, "data": data, "auth_token": "tokA"},
        )
        assert status == 200
        status, result = await parity_server.dispatch_async(
            "replica_fetch", {"chunk_id": self._CHUNK, "auth_token": "tokA"}
        )
        assert status == 200
        assert base64.b64decode(result["data"]) == b"cipher-chunk"

    @pytest.mark.asyncio
    async def test_fetch_by_non_owner_forbidden(self, parity_server):
        data = base64.b64encode(b"x").decode("ascii")
        self._set_requester(parity_server, "ownerA")
        await parity_server.dispatch_async(
            "replica_store",
            {"chunk_id": self._CHUNK, "data": data, "auth_token": "tokA"},
        )
        # 다른 요청자(소유자 불일치) → 403
        self._set_requester(parity_server, "ownerB")
        status, _ = await parity_server.dispatch_async(
            "replica_fetch", {"chunk_id": self._CHUNK, "auth_token": "tokB"}
        )
        assert status == 403

    @pytest.mark.asyncio
    async def test_missing_token_401(self, parity_server):
        # 토큰 검증 실패(None) → 401, ParityStore 미호출
        self._set_requester(parity_server, None)
        status, _ = await parity_server.dispatch_async(
            "replica_store",
            {"chunk_id": self._CHUNK, "data": "QQ==", "auth_token": "bad"},
        )
        assert status == 401

    @pytest.mark.asyncio
    async def test_file_op_same_user_delegates(self, parity_server):
        # 파일 op는 토큰 user_id가 로컬 user(test-user-123)와 같으면 dispatch 위임.
        self._set_requester(parity_server, "test-user-123")
        status, result = await parity_server.dispatch_async(
            "exists", {"physical_path": "x", "source_id": "vol1",
                       "auth_token": "tok"}
        )
        assert status == 200 and result["exists"] is False

    @pytest.mark.asyncio
    async def test_file_op_other_user_forbidden(self, parity_server):
        # 타 사용자 토큰의 파일 op는 403(임의 피어 read/write 차단).
        self._set_requester(parity_server, "intruder")
        status, _ = await parity_server.dispatch_async(
            "read", {"physical_path": "x", "auth_token": "tok"}
        )
        assert status == 403

    @pytest.mark.asyncio
    async def test_file_op_no_token_401(self, parity_server):
        self._set_requester(parity_server, None)
        status, _ = await parity_server.dispatch_async("read", {})
        assert status == 401

    @pytest.mark.asyncio
    async def test_unknown_op_after_auth_400(self, parity_server):
        self._set_requester(parity_server, "test-user-123")
        status, _ = await parity_server.dispatch_async("frobnicate", {})
        assert status == 400


class TestDispatchLoopback:
    """LoopbackSource에서 dispatch가 동반 디렉토리 경로로 올바르게 동작하는지 검증.

    회귀: LoopbackSource는 실제 파일을 path + '.d' 동반 디렉토리에 저장한다.
    dispatch가 source.path 기준으로 os.path.isfile을 검사하면 항상 404가 났다
    (실서버 릴레이 read에서 발견된 버그).
    """

    @pytest.fixture
    def loopback_p2p(self, tmp_path):
        img = str(tmp_path / "vol.img")
        source = LoopbackSource("loop-001", img, 10 * 1024 * 1024)
        source.initialize()
        store = MagicMock()
        storage_pool = StoragePool(sources=[source], metadata_store=store)
        auth = MagicMock(spec=AuthClient)
        auth.user_id = "u1"
        return P2PServer(storage_pool, auth, 9999, "http://localhost:8000"), source

    def test_dispatch_read_loopback(self, loopback_p2p):
        """동반 디렉토리에 기록한 파일을 dispatch read로 읽는다."""
        server, source = loopback_p2p
        data = b"loopback relayed content"
        source.write("abc_file.txt", data)

        status, result = server.dispatch(
            "read", {"physical_path": "abc_file.txt", "source_id": "loop-001"}
        )
        assert status == 200
        assert base64.b64decode(result["data"]) == data

    def test_dispatch_read_loopback_missing(self, loopback_p2p):
        """없는 파일은 404."""
        server, _source = loopback_p2p
        status, result = server.dispatch(
            "read", {"physical_path": "nope.txt", "source_id": "loop-001"}
        )
        assert status == 404

    def test_dispatch_exists_loopback(self, loopback_p2p):
        """exists도 동반 디렉토리 기준으로 동작한다."""
        server, source = loopback_p2p
        source.write("present.txt", b"x")
        status, result = server.dispatch(
            "exists", {"physical_path": "present.txt", "source_id": "loop-001"}
        )
        assert status == 200
        assert result["exists"] is True


class TestBackupAnnounce:
    """백업 위임 op: 청크를 보관한 device가 자기 몫을 올리게 예약한다."""

    @pytest.fixture
    def server(self, storage_pool, mock_auth_client):
        return P2PServer(
            storage_pool=storage_pool,
            auth_client=mock_auth_client,
            port=9998,
            server_url="http://localhost:8000",
        )

    def test_announce_calls_scheduler(self, server):
        received = []
        server.set_backup_announcer(received.append)

        status, result = server._op_backup_announce(
            {"virtual_path": "/docs/a.txt"}
        )
        assert status == 200
        assert result["status"] == "announced"
        assert received == ["/docs/a.txt"]

    def test_announce_without_scheduler_is_503(self, server):
        status, _ = server._op_backup_announce({"virtual_path": "/a.txt"})
        assert status == 503

    def test_announce_requires_path(self, server):
        server.set_backup_announcer(lambda _p: None)
        status, _ = server._op_backup_announce({})
        assert status == 400

    @pytest.mark.asyncio
    async def test_announce_via_relay_requires_same_user(self, server):
        """릴레이/UDP 경로도 같은 사용자 토큰이어야 한다(파일 op와 동일 인가)."""
        received = []
        server.set_backup_announcer(received.append)

        async def _other_user(_token):
            return "intruder"

        server._resolve_token_user = _other_user
        status, _ = await server.dispatch_async(
            "backup_announce", {"virtual_path": "/a.txt", "auth_token": "t"}
        )
        assert status == 403
        assert received == []
