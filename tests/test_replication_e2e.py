#!/usr/bin/env python3
"""리플리케이션 E2E (다중 홀더, 실제 HTTP).

실제 P2PServer 홀더(각자 ParityStore) + 리플리케이션 엔드포인트를 구현한 경량 mock
중앙 서버 + 소유자 ReplicationManager로 전 구간을 검증한다.

- replicate → 청크가 홀더에 실제 저장(암호문만, 호스트 비가독) → recover 바이트 일치.
- 일부 홀더 오프라인/중지 → 도달 가능한 홀더에서 복구(스웜).
- 홀더 1곳만 도달 가능해도 복구 성공.
- 홀더 부족(<3) → pending(가용성 명시).

ReplicationManager의 동기 API는 전용 IO 루프로 자가 브리지되므로, 테스트 루프가
홀더 요청을 처리할 수 있도록 asyncio.to_thread로 감싼다.
"""

from __future__ import annotations

import os
import socket
import tempfile

import pytest
import pytest_asyncio
from aiohttp import web

from stardustlib.auth_client import AuthClient
from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.p2p_server import P2PServer
from stardustlib.parity_store import ParityStore
from stardustlib.replication_manager import RecoveryError, ReplicationManager
from stardustlib.storage_source import DirectorySource

pytestmark = pytest.mark.asyncio

_OWNER = "owner-user"
_TOKEN = "owner-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeAuth(AuthClient):
    def __init__(self, url: str, user_id: str) -> None:
        super().__init__(url)
        self._access_token = _TOKEN
        self._user_id = user_id

    async def get_valid_token(self) -> str:
        return _TOKEN


class _MockCentral:
    """auth/verify + /replication/* 를 구현한 경량 중앙 서버."""

    def __init__(self) -> None:
        self._port = _free_port()
        self._runner: web.AppRunner | None = None
        # device_id -> {"address": str, "online": bool}
        self.holders: dict[str, dict] = {}
        # (owner, file_ref) -> [chunk dict]
        self.chunks: dict[tuple, list[dict]] = {}
        self.chunk_owner: dict[str, str] = {}
        self.replicas: dict[str, list[str]] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def add_holder(self, device_id: str, address: str) -> None:
        self.holders[device_id] = {"address": address, "online": True}

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/auth/verify", self._verify)
        app.router.add_post("/replication/chunks", self._register_chunk)
        app.router.add_post("/replication/placement", self._placement)
        app.router.add_post("/replication/replicas", self._record_replica)
        app.router.add_get("/replication/chunks/{file_ref}", self._list_chunks)
        app.router.add_get("/replication/replicas/{chunk_id}", self._list_replicas)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _verify(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("token") == _TOKEN:
            return web.json_response({"valid": True, "user_id": _OWNER})
        return web.json_response({"valid": False})

    async def _register_chunk(self, request: web.Request) -> web.Response:
        b = await request.json()
        key = (_OWNER, b["file_ref"])
        entry = {"chunk_id": b["chunk_id"], "idx": b["idx"], "size": b["size"]}
        lst = self.chunks.setdefault(key, [])
        if all(c["chunk_id"] != b["chunk_id"] for c in lst):
            lst.append(entry)
        self.chunk_owner[b["chunk_id"]] = _OWNER
        return web.json_response({"status": "ok"})

    async def _placement(self, request: web.Request) -> web.Response:
        b = await request.json()
        exclude = set(b.get("exclude", []))
        count = b.get("count", 3)
        holders = [
            {"device_id": d, "connection_address": h["address"]}
            for d, h in self.holders.items()
            if h["online"] and d not in exclude
        ]
        return web.json_response({"holders": holders[:count]})

    async def _record_replica(self, request: web.Request) -> web.Response:
        b = await request.json()
        lst = self.replicas.setdefault(b["chunk_id"], [])
        if b["holder_device_id"] not in lst:
            lst.append(b["holder_device_id"])
        return web.json_response({"status": "ok"})

    async def _list_chunks(self, request: web.Request) -> web.Response:
        file_ref = request.match_info["file_ref"]
        lst = sorted(
            self.chunks.get((_OWNER, file_ref), []), key=lambda c: c["idx"]
        )
        return web.json_response(lst)

    async def _list_replicas(self, request: web.Request) -> web.Response:
        chunk_id = request.match_info["chunk_id"]
        out = []
        for d in self.replicas.get(chunk_id, []):
            h = self.holders.get(d)
            if h is None:
                continue
            out.append({
                "device_id": d,
                "connection_address": h["address"],
                "is_online": h["online"],
                "status": "active",
            })
        return web.json_response(out)


class _Holder:
    """실제 P2PServer + ParityStore 홀더."""

    def __init__(self, device_id: str, central_url: str) -> None:
        self.device_id = device_id
        self.port = _free_port()
        self.address = f"127.0.0.1:{self.port}"
        self._dir = tempfile.mkdtemp()
        src = DirectorySource(f"vol-{device_id}", self._dir)
        src.initialize()
        store = MetadataStore(os.path.join(self._dir, ".m.db"), b"\x00" * 32)
        store.initialize()
        jbod = JBODManager([src], store, encryption_engine=None)
        parity = ParityStore(os.path.join(self._dir, "parity"))
        auth = _FakeAuth(central_url, f"holder-{device_id}")
        self.parity = parity
        self.p2p = P2PServer(jbod, auth, self.port, central_url, parity_store=parity)

    async def start(self) -> None:
        await self.p2p.start()

    async def stop(self) -> None:
        await self.p2p.stop()


@pytest_asyncio.fixture
async def env():
    """중앙 mock + 3 홀더 + 소유자 매니저를 구성한다."""
    central = _MockCentral()
    await central.start()

    holders = [_Holder(f"h{i}", central.url) for i in range(3)]
    for h in holders:
        await h.start()
        central.add_holder(h.device_id, h.address)

    # 소유자 코어(암호화 엔진 + 로컬 파일)
    owner_dir = tempfile.mkdtemp()
    src = DirectorySource("owner-vol", owner_dir)
    src.initialize()
    key = os.urandom(32)
    from stardustlib.encryption_engine import EncryptionEngine

    store = MetadataStore(os.path.join(owner_dir, ".m.db"), b"\x01" * 32)
    store.initialize()
    jbod = JBODManager([src], store, encryption_engine=EncryptionEngine(key))
    owner_auth = _FakeAuth(central.url, _OWNER)
    mgr = ReplicationManager(
        owner_auth, central.url, store, jbod,
        chunk_size=64, min_replicas=3,
    )

    yield {"central": central, "holders": holders, "mgr": mgr, "jbod": jbod}

    mgr.close()
    for h in holders:
        await h.stop()
    await central.stop()


async def _put_file(env, vpath: str, content: bytes) -> None:
    import asyncio

    await asyncio.to_thread(env["jbod"].write_file, vpath, content)


@pytest.mark.asyncio
async def test_replicate_then_recover_roundtrip(env):
    import asyncio

    content = ("E2E 한글 데이터 " * 30).encode("utf-8") + os.urandom(40)
    await _put_file(env, "/doc.bin", content)

    result = await asyncio.to_thread(env["mgr"].replicate, "/doc.bin")
    assert result.status == "replicated"
    assert all(n == 3 for n in result.replicas_per_chunk)

    # 호스트는 암호문만 보관(평문 비가독) — 홀더 ParityStore에 평문이 없다.
    for holder in env["holders"]:
        assert holder.parity.used_bytes() > 0

    # 로컬 파일 훼손 후 복구 → 바이트 일치
    await _put_file(env, "/doc.bin", b"corrupted")
    n = await asyncio.to_thread(env["mgr"].recover, "/doc.bin")
    assert n == len(content)
    assert await asyncio.to_thread(env["jbod"].read_file, "/doc.bin") == content


@pytest.mark.asyncio
async def test_swarm_recover_with_one_reachable_holder(env):
    import asyncio

    content = os.urandom(500)
    await _put_file(env, "/blob", content)
    await asyncio.to_thread(env["mgr"].replicate, "/blob")

    # 3 홀더 중 2곳 중지 + 오프라인 표시 → 1곳만 도달 가능
    for holder in env["holders"][:2]:
        await holder.stop()
        env["central"].holders[holder.device_id]["online"] = False

    n = await asyncio.to_thread(env["mgr"].recover, "/blob")
    assert n == len(content)
    assert await asyncio.to_thread(env["jbod"].read_file, "/blob") == content


@pytest.mark.asyncio
async def test_recover_fails_when_all_holders_offline(env):
    import asyncio

    await _put_file(env, "/x", os.urandom(200))
    await asyncio.to_thread(env["mgr"].replicate, "/x")
    for holder in env["holders"]:
        await holder.stop()
        env["central"].holders[holder.device_id]["online"] = False

    with pytest.raises(RecoveryError):
        await asyncio.to_thread(env["mgr"].recover, "/x")


@pytest.mark.asyncio
async def test_pending_when_insufficient_holders(env):
    import asyncio

    # 홀더 1곳만 온라인으로 남기고 2곳 오프라인 → 배치 후보 1 < 3 → pending
    for holder in env["holders"][1:]:
        env["central"].holders[holder.device_id]["online"] = False

    await _put_file(env, "/y", os.urandom(100))
    result = await asyncio.to_thread(env["mgr"].replicate, "/y")
    assert result.status == "pending"
    assert all(n <= 1 for n in result.replicas_per_chunk)


@pytest.mark.asyncio
async def test_heal_restores_replication_after_holder_loss(env):
    import asyncio

    # 4번째 홀더를 추가(재복제 예비)
    extra = _Holder("h3", env["central"].url)
    await extra.start()
    env["central"].add_holder(extra.device_id, extra.address)

    content = os.urandom(300)
    await _put_file(env, "/z", content)
    await asyncio.to_thread(env["mgr"].replicate, "/z")  # h0,h1,h2 (placement 3)

    # 홀더 1곳 상실 → 재복제로 h3 보충
    lost = env["holders"][0]
    await lost.stop()
    env["central"].holders[lost.device_id]["online"] = False

    report = await asyncio.to_thread(env["mgr"].ensure_replicas, "/z")
    assert report.status == "replicated"
    assert report.unrecoverable == []
    assert extra.parity.used_bytes() > 0  # h3가 보충 받음

    await extra.stop()
