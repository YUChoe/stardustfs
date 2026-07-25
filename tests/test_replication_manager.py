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
    """실제 JBOD처럼 at-rest 암호문을 보관하는 대역(암호화 엔진은 실제 구현)."""

    def __init__(self, key: bytes) -> None:
        self.encryption_engine = EncryptionEngine(key)
        # 실제 구현과 동일하게 암호문을 저장한다(평문 저장이 아님).
        self._files: dict[str, bytes] = {}

    def put(self, virtual_path: str, data: bytes) -> None:
        self._files[virtual_path] = self.encryption_engine.encrypt(data)

    def read_file(self, virtual_path: str) -> bytes:
        return self.encryption_engine.decrypt(self._files[virtual_path])

    def read_ciphertext(self, virtual_path: str) -> bytes:
        return self._files[virtual_path]

    def write_file(self, virtual_path: str, data: bytes) -> None:
        self._files[virtual_path] = self.encryption_engine.encrypt(data)

    def write_ciphertext(
        self, virtual_path: str, encrypted: bytes, plain_size: int
    ) -> None:
        self._files[virtual_path] = encrypted


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
        # 이 주소의 홀더는 손상된 바이트를 반환한다(무결성 검증 테스트용).
        self.corrupt: set[str] = set()
        # placement가 반환할 최대 후보 수(None=무제한, 실서버는 count로 제한).
        self.cap = cap
        self.chunk_meta: dict[str, list[dict]] = {}
        self.registry: dict[str, list[str]] = {}
        self.holder_store: dict[str, dict[str, bytes]] = {}

    def attach(self, mgr: ReplicationManager) -> None:
        cloud = self

        async def register_chunk(
            token, chunk_id, file_ref, idx, size, chunk_hash=None
        ):
            cloud.chunk_meta.setdefault(file_ref, []).append(
                {"chunk_id": chunk_id, "idx": idx, "size": size,
                 "hash": chunk_hash}
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
            data = cloud.holder_store.get(address, {}).get(chunk_id)
            if data is not None and address in cloud.corrupt:
                return b"\x00" * len(data)  # 손상된 사본
            return data

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


# --- 청크 무결성 해시 ---

def test_chunk_hash_is_deterministic_and_sensitive():
    """같은 바이트는 같은 해시, 1바이트만 달라도 다른 해시(Property 1)."""
    from stardustlib import chunker

    data = b"cipher-bytes"
    assert chunker.chunk_hash(data) == chunker.chunk_hash(b"cipher-bytes")
    assert len(chunker.chunk_hash(data)) == 64
    assert chunker.chunk_hash(data) != chunker.chunk_hash(b"cipher-byteS")
    assert chunker.chunk_hash(b"") == chunker.chunk_hash(b"")


def test_replicate_registers_chunk_hashes(key):
    """복제 시 각 청크의 암호문 해시가 서버에 등록된다."""
    from stardustlib import chunker

    jbod = _FakeJbod(key)
    jbod.put("/f", b"x" * 200)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    infos = cloud.chunk_meta[mgr._file_ref("/f")]
    assert infos and all(i["hash"] for i in infos)
    # 등록된 해시가 홀더에 저장된 실제 바이트의 해시와 일치한다
    for info in infos:
        stored = cloud.holder_store["h1"][info["chunk_id"]]
        assert info["hash"] == chunker.chunk_hash(stored)


def test_recover_skips_corrupt_holder(key):
    """손상된 사본을 가진 홀더를 배제하고 정상 홀더에서 복구한다(Property 3)."""
    jbod = _FakeJbod(key)
    original = b"integrity-check" * 20
    jbod.put("/f", original)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        # 첫 홀더가 손상된 바이트를 반환하도록 만든다
        cloud.corrupt.add("h1")
        jbod.write_file("/f", b"")  # 로컬 원본 훼손
        nbytes = mgr.recover("/f")
    finally:
        mgr.close()

    assert nbytes == len(original)
    assert jbod.read_file("/f") == original


def test_recover_fails_when_all_holders_corrupt(key):
    """모든 홀더가 손상이면 누락 chunk_id를 명시한 RecoveryError를 낸다."""
    jbod = _FakeJbod(key)
    jbod.put("/f", b"y" * 200)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        cloud.corrupt.update({"h1", "h2", "h3"})
        with pytest.raises(RecoveryError) as exc:
            mgr.recover("/f")
    finally:
        mgr.close()

    assert exc.value.missing_chunks  # 어느 청크가 문제인지 특정된다


def test_recover_without_hash_skips_verification(key):
    """레거시 청크(해시 미등록)는 검증을 생략하고 기존 동작을 유지한다(Property 4)."""
    jbod = _FakeJbod(key)
    original = b"legacy-chunk" * 10
    jbod.put("/f", original)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        # 서버가 해시를 모르는 상태(구버전/레거시)로 만든다
        for info in cloud.chunk_meta[mgr._file_ref("/f")]:
            info["hash"] = None
        jbod.write_file("/f", b"")
        nbytes = mgr.recover("/f")
    finally:
        mgr.close()

    assert nbytes == len(original)
    assert jbod.read_file("/f") == original


def test_heal_does_not_copy_corrupt_chunk(key):
    """재복제가 손상된 소스를 배제하고 정상 소스만 새 홀더로 복사한다."""
    jbod = _FakeJbod(key)
    jbod.put("/f", b"z" * 100)
    meta = _FakeMeta({"/f"})
    # 홀더 3개로 복제 후, 1곳을 손상시키고 새 홀더(h4)를 추가해 보충하게 한다
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        cloud.corrupt.add("h1")          # 첫 소스는 손상
        cloud.offline.update({"h2"})     # 온라인 소스를 줄여 degraded 유발
        cloud.holders.append("h4")       # 새 홀더 후보
        mgr.ensure_replicas("/f")
    finally:
        mgr.close()

    from stardustlib import chunker

    # h4로 복사된 바이트는 손상본이 아니라 정상본이어야 한다
    for info in cloud.chunk_meta[mgr._file_ref("/f")]:
        copied = cloud.holder_store.get("h4", {}).get(info["chunk_id"])
        if copied is not None:
            assert chunker.chunk_hash(copied) == info["hash"]


def test_heal_reports_unrecoverable_when_only_source_is_corrupt(key):
    """유효한 소스가 없으면(온라인 소스가 손상뿐) unrecoverable로 보고한다."""
    jbod = _FakeJbod(key)
    jbod.put("/f", b"w" * 100)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        cloud.corrupt.update({"h1", "h2", "h3"})
        cloud.offline.update({"h2", "h3"})  # 온라인은 h1(손상)뿐
        report = mgr.ensure_replicas("/f")
    finally:
        mgr.close()

    assert report.status == "pending"
    assert report.unrecoverable  # 조용한 성공 처리 금지


# --- at-rest 암호문 재사용(이중 암호화 제거) ---

def test_replicate_uses_at_rest_ciphertext(key):
    """복제 청크를 이어붙이면 at-rest 암호문과 바이트 단위로 동일하다.

    복호화→재암호화를 거치면 nonce가 달라져 다른 바이트가 되므로, 이 단언이
    이중 암호화가 없음을 보장한다.
    """
    from stardustlib import chunker

    jbod = _FakeJbod(key)
    jbod.put("/f", b"at-rest-bytes" * 30)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    infos = sorted(cloud.chunk_meta[mgr._file_ref("/f")], key=lambda c: c["idx"])
    rejoined = chunker.join([
        (c["idx"], cloud.holder_store["h1"][c["chunk_id"]]) for c in infos
    ])
    assert rejoined == jbod.read_ciphertext("/f")


def test_recover_restores_identical_at_rest_bytes(key):
    """복구는 받은 암호문을 그대로 기록해 at-rest 바이트를 동일하게 되돌린다.

    재암호화하면 등록된 청크 해시와 at-rest가 어긋나므로, 동일성이 중요하다.
    """
    jbod = _FakeJbod(key)
    original = b"restore-identical" * 25
    jbod.put("/f", original)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    before = jbod.read_ciphertext("/f")
    try:
        mgr.replicate("/f")
        jbod.write_file("/f", b"")  # 로컬 훼손(다른 암호문으로 덮어씀)
        assert jbod.read_ciphertext("/f") != before
        mgr.recover("/f")
    finally:
        mgr.close()

    assert jbod.read_ciphertext("/f") == before  # 바이트 동일 복원
    assert jbod.read_file("/f") == original


def test_replicate_needs_no_encryption_engine(key):
    """복제는 at-rest 암호문만 다루므로 암호화 엔진 없이도 동작한다."""
    jbod = _FakeJbod(key)
    jbod.put("/f", b"opaque" * 20)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(jbod, meta, ["h1", "h2", "h3"])
    mgr._engine = None  # 복제 경로는 엔진에 의존하지 않는다
    try:
        result = mgr.replicate("/f")
    finally:
        mgr.close()
    assert result.status == "replicated"
