#!/usr/bin/env python3
"""새 디바이스 key 복원 실환경 통합 테스트.

멀티디바이스의 가장 기본 전제(새 PC에서 같은 계정으로 데이터 접근)를 검증한다.
시나리오:
  1. PC-A(최초 디바이스): master_key를 STARDUST_KEY_PASSWORD로 암호화하여
     서버 /sync/key에 백업하고, 같은 key로 metadata를 암호화하여 /sync/metadata에 업로드
  2. PC-B(새 디바이스, key_file 없음): 서버에서 key를 복원하고, 복원한 key로
     PC-A가 올린 metadata를 정상 복호화

실서버 의존을 없애기 위해 로컬에 실제 stardustfs-server(FastAPI)를 띄우는 대신,
key 백업/복원 로직(stardustfs.py의 _backup_key_to_server / _restore_key_from_server)을
실제 HTTP로 구동하는 경량 mock 서버(/sync/key PUT·GET)를 같은 프로세스에서 운영한다.

실행: source .venv/Scripts/activate && pytest tests/test_key_recovery_integration.py -v
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import tempfile

import pytest
import pytest_asyncio
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stardustfs
from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.exceptions import KeyMismatchError
from stardustlib.key_backup_engine import KeyBackupEngine
from stardustlib.metadata_store import MetadataStore
from stardustlib.sync_client import SyncClient

pytestmark = pytest.mark.asyncio

_KEY_PASSWORD = "user-key-password-1234"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeAuthClient:
    """key 복원/백업 함수가 요구하는 최소 인터페이스만 갖춘 AuthClient 대역.

    _backup_key_to_server / _restore_key_from_server는
    auth_client.get_valid_token()과 auth_client._server_url만 사용한다.
    """

    def __init__(self, server_url: str) -> None:
        self._server_url = server_url.rstrip("/")

    async def get_valid_token(self) -> str:
        return "test-token"


class _MockKeyServer:
    """서버의 /sync/key PUT·GET·status를 흉내내는 경량 서버 (user당 1개 blob 보관)."""

    def __init__(self) -> None:
        self._port = _free_port()
        self._runner: web.AppRunner | None = None
        self._key_blob: bytes | None = None
        self._metadata_blob: bytes | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        app = web.Application(client_max_size=200 * 1024 * 1024)
        app.router.add_put("/sync/key", self._put_key)
        app.router.add_get("/sync/key", self._get_key)
        app.router.add_put("/sync/metadata", self._put_metadata)
        app.router.add_get("/sync/metadata", self._get_metadata)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _put_key(self, request: web.Request) -> web.Response:
        self._key_blob = await request.read()
        return web.json_response({"status": "ok"})

    async def _get_key(self, request: web.Request) -> web.Response:
        if self._key_blob is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.Response(
            body=self._key_blob, content_type="application/octet-stream"
        )

    async def _put_metadata(self, request: web.Request) -> web.Response:
        self._metadata_blob = await request.read()
        return web.json_response({"version": 1})

    async def _get_metadata(self, request: web.Request) -> web.Response:
        if self._metadata_blob is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.Response(
            body=self._metadata_blob, content_type="application/octet-stream"
        )


@pytest_asyncio.fixture
async def key_server():
    server = _MockKeyServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def _key_password_env():
    """테스트 동안 STARDUST_KEY_PASSWORD를 설정한다."""
    old = os.environ.get("STARDUST_KEY_PASSWORD")
    os.environ["STARDUST_KEY_PASSWORD"] = _KEY_PASSWORD
    yield
    if old is None:
        os.environ.pop("STARDUST_KEY_PASSWORD", None)
    else:
        os.environ["STARDUST_KEY_PASSWORD"] = old


async def test_backup_then_restore_roundtrip(key_server, _key_password_env):
    """PC-A가 백업한 key를 PC-B가 복원하면 동일한 master_key를 얻는다."""
    logger = logging.getLogger("key-recovery-test")
    tmp = tempfile.mkdtemp()

    # PC-A: master_key 생성 후 key_file 기록
    master_key = os.urandom(32)
    key_file_a = os.path.join(tmp, "a", "master.key")
    os.makedirs(os.path.dirname(key_file_a), exist_ok=True)
    with open(key_file_a, "wb") as f:
        f.write(master_key)

    auth = _FakeAuthClient(key_server.url)

    # PC-A: 서버에 key 백업 업로드 (서버에 없으면 업로드)
    await stardustfs._backup_key_to_server(auth, key_file_a, logger)
    assert key_server._key_blob is not None

    # PC-B: key_file이 없는 새 디바이스 → 서버에서 복원
    key_file_b = os.path.join(tmp, "b", "master.key")
    await stardustfs._restore_key_from_server(auth, key_file_b, logger)

    # 복원된 key_file이 PC-A의 master_key와 바이트 단위로 동일해야 함
    assert os.path.isfile(key_file_b)
    with open(key_file_b, "rb") as f:
        restored = f.read()
    assert restored == master_key


async def test_backup_does_not_overwrite_existing(key_server, _key_password_env):
    """서버에 이미 key 백업이 있으면 다른 디바이스가 덮어쓰지 않는다."""
    logger = logging.getLogger("key-recovery-test")
    tmp = tempfile.mkdtemp()

    # PC-A가 먼저 백업
    master_key_a = os.urandom(32)
    key_file_a = os.path.join(tmp, "a.key")
    with open(key_file_a, "wb") as f:
        f.write(master_key_a)
    auth = _FakeAuthClient(key_server.url)
    await stardustfs._backup_key_to_server(auth, key_file_a, logger)
    first_blob = key_server._key_blob

    # PC-A2가 다른 master_key로 백업 시도 → 이미 존재하므로 덮어쓰지 않음
    master_key_a2 = os.urandom(32)
    key_file_a2 = os.path.join(tmp, "a2.key")
    with open(key_file_a2, "wb") as f:
        f.write(master_key_a2)
    await stardustfs._backup_key_to_server(auth, key_file_a2, logger)

    assert key_server._key_blob == first_blob, "기존 key 백업이 덮어쓰였음"


async def test_restored_key_decrypts_metadata(key_server, _key_password_env):
    """PC-B가 복원한 key로 PC-A가 올린 metadata를 복호화할 수 있다 (end-to-end)."""
    logger = logging.getLogger("key-recovery-test")
    tmp = tempfile.mkdtemp()

    # PC-A: master_key 생성, key_file 기록, 서버 백업
    master_key = os.urandom(32)
    key_file_a = os.path.join(tmp, "a", "master.key")
    os.makedirs(os.path.dirname(key_file_a), exist_ok=True)
    with open(key_file_a, "wb") as f:
        f.write(master_key)
    auth = _FakeAuthClient(key_server.url)
    await stardustfs._backup_key_to_server(auth, key_file_a, logger)

    # PC-A: metadata DB에 파일 기록 후 SyncClient로 암호화 업로드
    #       (metadata 암호화 키는 master_key에서 HKDF 파생 — 실제 흐름과 동일)
    db_key = _derive_db_key(master_key)
    db_a = os.path.join(tmp, "a", "metadata.db")
    store_a = MetadataStore(db_a, db_key)
    store_a.initialize()
    import time
    store_a.insert("/shared.txt", "vol1", "phys/shared.txt", 123, time.time(), time.time())

    resolver_a = ConflictResolver(store_a, "device-A")
    sync_a = SyncClient(auth, key_server.url, store_a, resolver_a,
                        encryption_key=db_key)
    await sync_a._force_upload()
    assert key_server._metadata_blob is not None

    # PC-B: 새 디바이스 — 서버에서 key 복원
    key_file_b = os.path.join(tmp, "b", "master.key")
    await stardustfs._restore_key_from_server(auth, key_file_b, logger)
    with open(key_file_b, "rb") as f:
        restored_master = f.read()

    # PC-B: 복원한 master_key로 db_key 파생 후 SyncClient로 다운로드·병합
    db_key_b = _derive_db_key(restored_master)
    db_b = os.path.join(tmp, "b", "metadata.db")
    store_b = MetadataStore(db_b, db_key_b)
    store_b.initialize()
    resolver_b = ConflictResolver(store_b, "device-B")
    sync_b = SyncClient(auth, key_server.url, store_b, resolver_b,
                        encryption_key=db_key_b)

    # 복호화·병합이 성공하면 PC-A의 파일이 보여야 함 (KeyMismatchError 없이)
    await sync_b.initial_sync()
    rec = store_b.lookup("/shared.txt")
    assert rec is not None, "복원한 key로 PC-A의 metadata를 복호화하지 못함"
    assert rec.file_size == 123

    await sync_a.stop()
    await sync_b.stop()
    store_a.close()
    store_b.close()


async def test_wrong_password_fails_restore(key_server, _key_password_env):
    """잘못된 STARDUST_KEY_PASSWORD로는 key 복원이 실패한다 (IntegrityError)."""
    logger = logging.getLogger("key-recovery-test")
    tmp = tempfile.mkdtemp()

    master_key = os.urandom(32)
    key_file_a = os.path.join(tmp, "a.key")
    with open(key_file_a, "wb") as f:
        f.write(master_key)
    auth = _FakeAuthClient(key_server.url)
    await stardustfs._backup_key_to_server(auth, key_file_a, logger)

    # 비밀번호를 틀린 값으로 변경
    os.environ["STARDUST_KEY_PASSWORD"] = "wrong-password"
    key_file_b = os.path.join(tmp, "b.key")
    from stardustlib.exceptions import IntegrityError
    with pytest.raises(IntegrityError):
        await stardustfs._restore_key_from_server(auth, key_file_b, logger)
    # 복원 실패 시 key_file이 생성되지 않아야 함
    assert not os.path.isfile(key_file_b)


async def test_backup_survives_unresponsive_server(key_server, _key_password_env):
    """서버가 응답하지 않아도 key 백업이 예외를 올리지 않는다(daemon 기동 보호).

    서버가 TCP는 받고 HTTP 응답을 주지 않으면 httpx.ReadTimeout이 난다. 이때
    예외가 daemon startup까지 전파되면 데몬이 아예 뜨지 못한다 — 오프라인 모드로
    계속 진행할 수 있어야 한다.
    """
    import httpx

    logger = logging.getLogger("key-recovery-test")
    tmp = tempfile.mkdtemp()
    key_file = os.path.join(tmp, "a.key")
    with open(key_file, "wb") as f:
        f.write(os.urandom(32))
    auth = _FakeAuthClient(key_server.url)

    async def _timeout(*args, **kwargs):
        raise httpx.ReadTimeout("server did not respond")

    # 존재 확인(GET) 단계에서 무응답
    orig_get = httpx.AsyncClient.get
    httpx.AsyncClient.get = _timeout
    try:
        await stardustfs._backup_key_to_server(auth, key_file, logger)
    finally:
        httpx.AsyncClient.get = orig_get
    assert key_server._key_blob is None, "무응답인데 업로드가 일어났음"

    # 업로드(PUT) 단계에서 무응답
    orig_put = httpx.AsyncClient.put
    httpx.AsyncClient.put = _timeout
    try:
        await stardustfs._backup_key_to_server(auth, key_file, logger)
    finally:
        httpx.AsyncClient.put = orig_put


async def test_backup_survives_unreachable_server(_key_password_env):
    """서버에 연결조차 되지 않아도 key 백업이 예외를 올리지 않는다."""
    logger = logging.getLogger("key-recovery-test")
    tmp = tempfile.mkdtemp()
    key_file = os.path.join(tmp, "a.key")
    with open(key_file, "wb") as f:
        f.write(os.urandom(32))

    # 아무도 듣지 않는 포트
    auth = _FakeAuthClient(f"http://127.0.0.1:{_free_port()}")
    await stardustfs._backup_key_to_server(auth, key_file, logger)


def _derive_db_key(master_key: bytes) -> bytes:
    """master_key에서 metadata DB 암호화 키를 파생한다 (stardustfs.py와 동일)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"stardustfs-metadata-db",
        info=b"db-encryption-key",
    )
    return hkdf.derive(master_key)
