"""리플리케이션 매니저(replicate/recover) 오케스트레이션 테스트.

서버 제어 평면과 홀더 직접 전송은 인메모리 fake로 대체하고, 암호화 라운드트립은
실제 EncryptionEngine으로 검증한다(Property 3).
"""
from __future__ import annotations

import base64
import os

import httpx
import pytest

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.replication_manager import (
    RecoveryError,
    ReplicationManager,
)


class _FakeAuth:
    def __init__(self) -> None:
        self.user_id = "user-1"

    async def get_valid_token(self) -> str:
        return "tok"


class _FakeJbod:
    """평문 저장 + 실제 암호화 엔진을 가진 JBOD 대역."""

    def __init__(self, key: bytes) -> None:
        self.encryption_engine = EncryptionEngine(key)
        self._files: dict[str, bytes] = {}

    def put(self, virtual_path: str, data: bytes) -> None:
        self._files[virtual_path] = data

    def read_file(self, virtual_path: str) -> bytes:
        return self._files[virtual_path]

    def write_file(self, virtual_path: str, data: bytes) -> None:
        self._files[virtual_path] = data


class _FakeMeta:
    def __init__(self, present: set[str]) -> None:
        self._present = present
        self.status: dict[str, str] = {}

    def lookup(self, virtual_path: str):
        return object() if virtual_path in self._present else None

    def set_replication_status(self, virtual_path: str, status: str) -> None:
        self.status[virtual_path] = status


class _Cloud:
    """서버 레지스트리 + 홀더 저장소 인메모리 모형."""

    def __init__(self, holders: list[str], offline_addresses: set[str] | None = None,
                 store_fail_addresses: set[str] | None = None,
                 cap: int | None = None) -> None:
        # device_id == address 로 단순화
        self.holders = holders
        self.offline = offline_addresses or set()
        self.store_fail = store_fail_addresses or set()
        # placement가 반환할 최대 후보 수(None=무제한, 실서버는 count로 제한).
        self.cap = cap
        self.chunk_meta: dict[str, list[dict]] = {}
        self.registry: dict[str, list[str]] = {}
        self.holder_store: dict[str, dict[str, bytes]] = {}

    def attach(self, mgr: ReplicationManager) -> None:
        cloud = self

        async def register_chunk(token, chunk_id, file_ref, idx, size):
            cloud.chunk_meta.setdefault(file_ref, []).append(
                {"chunk_id": chunk_id, "idx": idx, "size": size}
            )

        async def placement(token, size, exclude):
            avail = [
                {"device_id": h, "connection_address": h}
                for h in cloud.holders if h not in exclude
            ]
            return avail if cloud.cap is None else avail[: cloud.cap]

        async def holder_store(device_id, address, chunk_id, data, token):
            if address in cloud.store_fail:
                return False
            cloud.holder_store.setdefault(address, {})[chunk_id] = data
            return True

        async def record_replica(token, chunk_id, device_id):
            cloud.registry.setdefault(chunk_id, []).append(device_id)
            return True

        async def list_chunks(token, file_ref):
            return list(cloud.chunk_meta.get(file_ref, []))

        async def list_replicas(token, chunk_id):
            return [
                {"device_id": d, "connection_address": d,
                 "is_online": d not in cloud.offline}
                for d in cloud.registry.get(chunk_id, [])
            ]

        async def holder_fetch(device_id, address, chunk_id, token):
            return cloud.holder_store.get(address, {}).get(chunk_id)

        mgr._register_chunk = register_chunk
        mgr._placement = placement
        mgr._holder_store = holder_store
        mgr._record_replica = record_replica
        mgr._list_chunks = list_chunks
        mgr._list_replicas = list_replicas
        mgr._holder_fetch = holder_fetch


def _manager(jbod, meta, holders, **cloud_kwargs):
    mgr = ReplicationManager(
        _FakeAuth(), "http://server", meta, jbod,
        chunk_size=64, min_replicas=3,
    )
    cloud = _Cloud(holders, **cloud_kwargs)
    cloud.attach(mgr)
    return mgr, cloud


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def test_replicate_then_recover_roundtrip(key):
    content = ("한글 데이터 — binary " * 40).encode("utf-8") + os.urandom(50)
    jbod = _FakeJbod(key)
    jbod.put("/a/file.bin", content)
    meta = _FakeMeta({"/a/file.bin"})
    mgr, _cloud = _manager(jbod, meta, ["h1", "h2", "h3"])

    result = mgr.replicate("/a/file.bin")
    assert result.status == "replicated"
    assert result.chunk_count > 1  # chunk_size=64 → 다중 청크
    assert all(n == 3 for n in result.replicas_per_chunk)
    assert meta.status["/a/file.bin"] == "replicated"

    # 복구: 원본과 정확히 일치 (Property 3)
    jbod.write_file("/a/file.bin", b"corrupted")  # 로컬 훼손 후 복구
    n = mgr.recover("/a/file.bin")
    assert n == len(content)
    assert jbod.read_file("/a/file.bin") == content


def test_replicate_insufficient_holders_is_pending(key):
    jbod = _FakeJbod(key)
    jbod.put("/f", b"data")
    meta = _FakeMeta({"/f"})
    mgr, _cloud = _manager(jbod, meta, ["h1", "h2"])  # 2 < 3

    result = mgr.replicate("/f")
    assert result.status == "pending"
    assert all(n == 2 for n in result.replicas_per_chunk)
    assert meta.status["/f"] == "pending"


def test_replicate_skips_failed_holder(key):
    jbod = _FakeJbod(key)
    jbod.put("/f", b"data")
    meta = _FakeMeta({"/f"})
    # 4 홀더 중 1곳 store 실패 → 3곳 확보 → replicated
    mgr, _cloud = _manager(
        jbod, meta, ["h1", "h2", "h3", "h4"], store_fail_addresses={"h2"}
    )
    result = mgr.replicate("/f")
    assert result.status == "replicated"
    assert all(n == 3 for n in result.replicas_per_chunk)


def test_recover_missing_when_no_chunks(key):
    jbod = _FakeJbod(key)
    meta = _FakeMeta(set())
    mgr, _cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    with pytest.raises(RecoveryError) as ei:
        mgr.recover("/never-replicated")
    assert ei.value.missing_chunks == []


def test_recover_missing_when_all_holders_offline(key):
    content = ("x" * 200).encode("utf-8")
    jbod = _FakeJbod(key)
    jbod.put("/f", content)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    mgr.replicate("/f")
    # 모든 홀더 오프라인 → 복구 불가
    cloud.offline = {"h1", "h2", "h3"}
    with pytest.raises(RecoveryError) as ei:
        mgr.recover("/f")
    assert len(ei.value.missing_chunks) >= 1


def test_recover_succeeds_with_one_reachable_holder(key):
    """스웜: 홀더 1곳만 도달 가능해도 복구 성공."""
    content = ("y" * 300).encode("utf-8")
    jbod = _FakeJbod(key)
    jbod.put("/f", content)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    mgr.replicate("/f")
    cloud.offline = {"h1", "h2"}  # h3만 온라인
    n = mgr.recover("/f")
    assert n == len(content)
    assert jbod.read_file("/f") == content


def test_ensure_replicas_tops_up_degraded_chunk(key):
    """홀더 1곳 오프라인 → 새 홀더로 복제본을 채워 healthy 회복."""
    content = ("z" * 400).encode("utf-8")
    jbod = _FakeJbod(key)
    jbod.put("/f", content)
    meta = _FakeMeta({"/f"})
    # cap=3: replicate는 h1~h3에 배치, h4는 재복제 예비.
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3", "h4"], cap=3)
    assert mgr.replicate("/f").status == "replicated"

    cloud.offline = {"h1"}  # 1곳 오프라인 → 청크별 online 2 < 3
    report = mgr.ensure_replicas("/f")
    assert report.status == "replicated"
    assert report.repaired == report.chunk_count  # 모든 청크가 1개씩 보충
    assert report.unrecoverable == []
    # h4가 각 청크의 새 홀더로 추가됨
    for holders in cloud.registry.values():
        assert "h4" in holders


def test_ensure_replicas_noop_when_healthy(key):
    jbod = _FakeJbod(key)
    jbod.put("/f", b"data")
    meta = _FakeMeta({"/f"})
    mgr, _cloud = _manager(jbod, meta, ["h1", "h2", "h3"], cap=3)
    mgr.replicate("/f")
    report = mgr.ensure_replicas("/f")
    assert report.status == "replicated"
    assert report.repaired == 0


def test_ensure_replicas_unrecoverable_when_no_online_source(key):
    content = ("q" * 200).encode("utf-8")
    jbod = _FakeJbod(key)
    jbod.put("/f", content)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"], cap=3)
    mgr.replicate("/f")
    cloud.offline = {"h1", "h2", "h3"}  # 소스 없음 → 복구 불가
    report = mgr.ensure_replicas("/f")
    assert report.status == "pending"
    assert len(report.unrecoverable) == report.chunk_count
    assert meta.status["/f"] == "pending"


def _bare_manager(key):
    jbod = _FakeJbod(key)
    return ReplicationManager(
        _FakeAuth(), "http://server", _FakeMeta(set()), jbod, min_replicas=2
    )


@pytest.mark.asyncio
async def test_holder_store_relay_fallback_on_connect_error(key):
    """직접 연결 실패(NAT) 시 같은 사용자 릴레이로 store가 성공한다."""
    mgr = _bare_manager(key)
    seen = {}

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def fake_relay(device_id, op, payload):
        seen.update(device_id=device_id, op=op, chunk=payload["chunk_id"],
                    token=payload.get("auth_token"))
        return {"bytes_written": 5}

    mgr._client.post = boom
    mgr._relay_op = fake_relay
    ok = await mgr._holder_store("devX", "1.2.3.4:9090", "c1", b"cipher", "tok")
    assert ok is True
    # 교차 사용자 홀더 인가용 소유자 토큰이 릴레이 payload에 포함돼야 한다
    assert seen == {"device_id": "devX", "op": "replica_store",
                    "chunk": "c1", "token": "tok"}


@pytest.mark.asyncio
async def test_holder_fetch_relay_fallback_on_connect_error(key):
    mgr = _bare_manager(key)

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    captured = {}

    async def fake_relay(device_id, op, payload):
        captured["token"] = payload.get("auth_token")
        return {"data": base64.b64encode(b"cipher").decode("ascii")}

    mgr._client.post = boom
    mgr._relay_op = fake_relay
    data = await mgr._holder_fetch("devX", "1.2.3.4:9090", "c1", "tok")
    assert data == b"cipher"
    assert captured["token"] == "tok"  # 릴레이 payload에 소유자 토큰 포함


@pytest.mark.asyncio
async def test_holder_store_no_relay_on_non_connection_error(key):
    """직접 비-200(쿼터 등)은 릴레이하지 않고 False(릴레이해도 동일 홀더)."""
    mgr = _bare_manager(key)
    relayed = {"called": False}

    class _Resp:
        status_code = 507

    async def post(*a, **k):
        return _Resp()

    async def fake_relay(device_id, op, payload):
        relayed["called"] = True
        return {}

    mgr._client.post = post
    mgr._relay_op = fake_relay
    ok = await mgr._holder_store("devX", "1.2.3.4:9090", "c1", b"x", "tok")
    assert ok is False and relayed["called"] is False


@pytest.mark.asyncio
async def test_holder_store_udp_before_relay(key):
    """직접 TCP 도달 불가 시 UDP(홀펀칭)를 릴레이보다 먼저 시도, UDP 성공이면 릴레이 안 함."""
    mgr = _bare_manager(key)
    seen = {}

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def udp(device_id, op, payload):
        seen.update(device_id=device_id, op=op, token=payload.get("auth_token"))
        return (200, {"bytes_written": 1})

    async def fake_relay(device_id, op, payload):
        seen["relay"] = True
        return {}

    mgr._client.post = boom
    mgr.set_udp_transport(udp)
    mgr._relay_op = fake_relay
    ok = await mgr._holder_store("devX", "1.2.3.4:9090", "c1", b"cipher", "tok")
    assert ok is True
    assert seen == {"device_id": "devX", "op": "replica_store", "token": "tok"}
    assert "relay" not in seen  # UDP 성공 → 릴레이 미사용


@pytest.mark.asyncio
async def test_holder_store_relay_after_udp_fails(key):
    """UDP가 예외(펀치 실패)면 릴레이로 fallback한다."""
    mgr = _bare_manager(key)
    relayed = {"called": False}

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def udp(device_id, op, payload):
        raise OSError("punch failed")

    async def fake_relay(device_id, op, payload):
        relayed["called"] = True
        return {"bytes_written": 1}

    mgr._client.post = boom
    mgr.set_udp_transport(udp)
    mgr._relay_op = fake_relay
    ok = await mgr._holder_store("devX", "1.2.3.4:9090", "c1", b"x", "tok")
    assert ok is True and relayed["called"] is True


@pytest.mark.asyncio
async def test_holder_fetch_udp_before_relay(key):
    mgr = _bare_manager(key)

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def udp(device_id, op, payload):
        return (200, {"data": base64.b64encode(b"cipher").decode("ascii")})

    relayed = {"called": False}

    async def fake_relay(device_id, op, payload):
        relayed["called"] = True
        return {}

    mgr._client.post = boom
    mgr.set_udp_transport(udp)
    mgr._relay_op = fake_relay
    data = await mgr._holder_fetch("devX", "1.2.3.4:9090", "c1", "tok")
    assert data == b"cipher" and relayed["called"] is False


def test_file_ref_does_not_leak_path(key):
    jbod = _FakeJbod(key)
    meta = _FakeMeta(set())
    mgr, _cloud = _manager(jbod, meta, [])
    ref = mgr._file_ref("/secret/path/document.txt")
    assert "secret" not in ref and "document" not in ref
    assert len(ref) == 64 and all(c in "0123456789abcdef" for c in ref)
