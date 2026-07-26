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


class _FakeStoragePool:
    """실제 스토리지 풀처럼 at-rest 암호문을 보관하는 대역(암호화 엔진은 실제 구현)."""

    def __init__(self, key: bytes, chunk_size: int = 64) -> None:
        self.encryption_engine = EncryptionEngine(key)
        # 실제 구현과 동일하게 암호문을 저장한다(평문 저장이 아님).
        self._files: dict[str, bytes] = {}
        self.chunk_size = chunk_size
        self.device_id: str | None = None
        # chunk_index → 보관 device_id. 미지정이면 로컬 보관으로 본다.
        self.chunk_devices: dict[int, str] = {}
        # read_chunks가 실제로 읽은 청크 index(원격 읽기 없음 검증용).
        self.read_indices: list[int] = []

    def read_chunks(
        self, virtual_path: str, local_only: bool = False, on_progress=None
    ) -> list:
        """at-rest 암호문을 청크 경계로 잘라 (idx, bytes)로 돌려준다.

        local_only=True면 이 device 보관 청크만 반환한다(실제 StoragePool과 동일).
        on_progress(done, total)는 청크마다 호출한다.
        """
        from stardustlib import chunker

        parts = chunker.split(self._files[virtual_path], self.chunk_size)
        if local_only and self.device_id:
            parts = [
                (i, d) for i, d in parts
                if self.chunk_devices.get(i, self.device_id) == self.device_id
            ]
        self.read_indices.extend(i for i, _d in parts)
        if on_progress is not None:
            for done in range(1, len(parts) + 1):
                on_progress(done, len(parts))
        return parts

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
    def __init__(
        self, present: set[str], chunks: dict[str, list] | None = None
    ) -> None:
        self._present = present
        self.status: dict[str, str] = {}
        # virtual_path → [ChunkRef 유사 객체]. 청크 보관 device 판정용.
        self._chunks = chunks or {}

    def lookup(self, virtual_path: str):
        return object() if virtual_path in self._present else None

    def set_replication_status(self, virtual_path: str, status: str) -> None:
        self.status[virtual_path] = status

    def get_chunks(self, virtual_path: str) -> list:
        return list(self._chunks.get(virtual_path, []))


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
        # 홀더로 실제 push한 횟수(재전송 생략 검증용)
        self.store_calls = 0

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
            cloud.store_calls += 1
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


def _manager(storage_pool, meta, holders, **cloud_kwargs):
    mgr = ReplicationManager(
        _FakeAuth(), "http://server", meta, storage_pool,
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
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/a/file.bin", content)
    meta = _FakeMeta({"/a/file.bin"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])

    result = mgr.replicate("/a/file.bin")
    assert result.status == "replicated"
    assert result.chunk_count > 1  # chunk_size=64 → 다중 청크
    assert all(n == 3 for n in result.replicas_per_chunk)
    assert meta.status["/a/file.bin"] == "replicated"

    # 복구: 원본과 정확히 일치 (Property 3)
    storage_pool.write_file("/a/file.bin", b"corrupted")  # 로컬 훼손 후 복구
    n = mgr.recover("/a/file.bin")
    assert n == len(content)
    assert storage_pool.read_file("/a/file.bin") == content


def test_replicate_insufficient_holders_is_pending(key):
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"data")
    meta = _FakeMeta({"/f"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2"])  # 2 < 3

    result = mgr.replicate("/f")
    assert result.status == "pending"
    assert all(n == 2 for n in result.replicas_per_chunk)
    assert meta.status["/f"] == "pending"


def test_replicate_skips_failed_holder(key):
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"data")
    meta = _FakeMeta({"/f"})
    # 4 홀더 중 1곳 store 실패 → 3곳 확보 → replicated
    mgr, _cloud = _manager(
        storage_pool, meta, ["h1", "h2", "h3", "h4"], store_fail_addresses={"h2"}
    )
    result = mgr.replicate("/f")
    assert result.status == "replicated"
    assert all(n == 3 for n in result.replicas_per_chunk)


def test_recover_missing_when_no_chunks(key):
    storage_pool = _FakeStoragePool(key)
    meta = _FakeMeta(set())
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    with pytest.raises(RecoveryError) as ei:
        mgr.recover("/never-replicated")
    assert ei.value.missing_chunks == []


def test_recover_missing_when_all_holders_offline(key):
    content = ("x" * 200).encode("utf-8")
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", content)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    mgr.replicate("/f")
    # 모든 홀더 오프라인 → 복구 불가
    cloud.offline = {"h1", "h2", "h3"}
    with pytest.raises(RecoveryError) as ei:
        mgr.recover("/f")
    assert len(ei.value.missing_chunks) >= 1


def test_recover_succeeds_with_one_reachable_holder(key):
    """스웜: 홀더 1곳만 도달 가능해도 복구 성공."""
    content = ("y" * 300).encode("utf-8")
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", content)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    mgr.replicate("/f")
    cloud.offline = {"h1", "h2"}  # h3만 온라인
    n = mgr.recover("/f")
    assert n == len(content)
    assert storage_pool.read_file("/f") == content


def test_ensure_replicas_tops_up_degraded_chunk(key):
    """홀더 1곳 오프라인 → 새 홀더로 복제본을 채워 healthy 회복."""
    content = ("z" * 400).encode("utf-8")
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", content)
    meta = _FakeMeta({"/f"})
    # cap=3: replicate는 h1~h3에 배치, h4는 재복제 예비.
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3", "h4"], cap=3)
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
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"data")
    meta = _FakeMeta({"/f"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"], cap=3)
    mgr.replicate("/f")
    report = mgr.ensure_replicas("/f")
    assert report.status == "replicated"
    assert report.repaired == 0


def test_ensure_replicas_unrecoverable_when_no_online_source(key):
    content = ("q" * 200).encode("utf-8")
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", content)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"], cap=3)
    mgr.replicate("/f")
    cloud.offline = {"h1", "h2", "h3"}  # 소스 없음 → 복구 불가
    report = mgr.ensure_replicas("/f")
    assert report.status == "pending"
    assert len(report.unrecoverable) == report.chunk_count
    assert meta.status["/f"] == "pending"


def _bare_manager(key):
    storage_pool = _FakeStoragePool(key)
    return ReplicationManager(
        _FakeAuth(), "http://server", _FakeMeta(set()), storage_pool, min_replicas=2
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


# --- pending 재시도 시 확보된 청크 재전송 생략 ---

def test_replicate_skips_already_secured_chunks(key):
    """같은 내용을 다시 복제하면 이미 목표를 채운 청크는 push하지 않는다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"resume" * 100)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        first = mgr.replicate("/f")
        calls_after_first = cloud.store_calls
        second = mgr.replicate("/f")
    finally:
        mgr.close()

    assert first.status == "replicated"
    assert calls_after_first > 0
    assert second.status == "replicated"
    assert cloud.store_calls == calls_after_first  # 재전송 없음
    assert second.replicas_per_chunk == first.replicas_per_chunk


def test_replicate_resends_when_content_changed(key):
    """파일이 바뀌면(청크 해시 불일치) 낡은 사본을 두지 않고 다시 올린다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"before" * 100)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        calls_after_first = cloud.store_calls
        storage_pool.put("/f", b"after!" * 100)  # 내용 변경 → 해시 변경
        result = mgr.replicate("/f")
    finally:
        mgr.close()

    assert result.status == "replicated"
    assert cloud.store_calls > calls_after_first  # 변경분은 재전송


def test_replicate_resends_when_replicas_lost(key):
    """등록은 남았지만 홀더가 모두 오프라인이면 다시 올린다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"lost" * 100)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        calls_after_first = cloud.store_calls
        cloud.offline = {"h1", "h2", "h3"}  # online 복제 0 → 목표 미달
        cloud.holders = ["h4", "h5", "h6"]  # 새 홀더로 다시 확보
        result = mgr.replicate("/f")
    finally:
        mgr.close()

    assert cloud.store_calls > calls_after_first
    assert result.status == "replicated"


def test_replicate_logs_progress_for_large_file(key, caplog):
    """청크가 많은 파일은 진행 로그를 남긴다(무응답 오인 방지)."""
    import logging

    from stardustlib import replication_manager as rm

    storage_pool = _FakeStoragePool(key)
    # chunk_size=64 → PROGRESS_MIN_CHUNKS(20) 이상이 되도록 충분히 크게
    storage_pool.put("/big", b"p" * (64 * rm.PROGRESS_MIN_CHUNKS * 2))
    meta = _FakeMeta({"/big"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    with caplog.at_level(logging.INFO, logger="stardustlib.replication_manager"):
        try:
            mgr.replicate("/big")
        finally:
            mgr.close()

    progress = [r for r in caplog.records if "복제 진행" in r.getMessage()]
    assert progress, "대용량 파일인데 진행 로그가 없다"
    # 마지막 로그는 전체 청크를 처리한 시점을 알린다
    assert progress[-1].getMessage().split("/")[1].split()[0].isdigit()


def test_replicate_small_file_has_no_progress_noise(key, caplog):
    """청크가 적은 파일은 진행 로그로 로그를 오염시키지 않는다."""
    import logging

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/small", b"s" * 100)  # chunk_size=64 → 2청크
    meta = _FakeMeta({"/small"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    with caplog.at_level(logging.INFO, logger="stardustlib.replication_manager"):
        try:
            mgr.replicate("/small")
        finally:
            mgr.close()

    assert not [r for r in caplog.records if "복제 진행" in r.getMessage()]


# --- 보관 한도 초과(507) 홀더 배제 ---

@pytest.mark.asyncio
async def test_direct_quota_response_blocks_holder(key):
    """직접 TCP 507이면 그 홀더를 배치 후보 배제 목록에 넣는다."""
    mgr = _bare_manager(key)

    class _Resp:
        status_code = 507

    async def post(*a, **k):
        return _Resp()

    mgr._client.post = post
    ok = await mgr._holder_store("devQ", "1.2.3.4:9090", "c1", b"x", "tok")
    assert ok is False
    assert mgr.quota_blocked_devices() == ["devQ"]


@pytest.mark.asyncio
async def test_relay_quota_response_blocks_holder(key):
    """릴레이가 status=507을 전달하면(RelayOpError) 홀더를 배제한다."""
    from stardustlib.relay_client import RelayOpError

    mgr = _bare_manager(key)

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def fake_relay(device_id, op, payload):
        raise RelayOpError("Relay op failed (status=507): quota", status=507)

    mgr._client.post = boom
    mgr._relay_op = fake_relay
    ok = await mgr._holder_store("devQ", "1.2.3.4:9090", "c1", b"x", "tok")
    assert ok is False
    assert mgr.quota_blocked_devices() == ["devQ"]


@pytest.mark.asyncio
async def test_udp_quota_response_blocks_holder(key):
    """UDP(홀펀칭) 경로의 507도 배제 대상이다."""
    mgr = _bare_manager(key)

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def udp(device_id, op, payload):
        return (507, {"error": "quota exceeded"})

    mgr._client.post = boom
    mgr.set_udp_transport(udp)
    ok = await mgr._holder_store("devQ", "1.2.3.4:9090", "c1", b"x", "tok")
    assert ok is False
    assert mgr.quota_blocked_devices() == ["devQ"]


@pytest.mark.asyncio
async def test_relay_reachability_failure_does_not_block_holder(key):
    """도달 불가(비-507)는 일시적이므로 배제하지 않는다."""
    mgr = _bare_manager(key)

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def fake_relay(device_id, op, payload):
        raise OSError("Relay timeout: target device did not respond")

    mgr._client.post = boom
    mgr._relay_op = fake_relay
    ok = await mgr._holder_store("devQ", "1.2.3.4:9090", "c1", b"x", "tok")
    assert ok is False
    assert mgr.quota_blocked_devices() == []


def test_quota_block_expires(key):
    """배제 기간이 지나면 다시 후보가 된다(홀더가 공간을 회수한 경우)."""
    import time as _time

    from stardustlib import replication_manager as rm

    mgr = _bare_manager(key)
    mgr._mark_quota_blocked("devQ")
    assert mgr.quota_blocked_devices() == ["devQ"]
    # 만료 시각을 과거로 돌려 경과를 모형화한다
    mgr._quota_blocked["devQ"] = _time.monotonic() - 1.0
    assert mgr.quota_blocked_devices() == []
    assert rm.QUOTA_BLOCK_SECONDS > 0


def test_replicate_stops_asking_for_quota_blocked_holder(key):
    """첫 청크에서 507이 나면 이후 청크의 placement에서 그 홀더를 제외한다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"q" * 500)  # chunk_size=64 → 다중 청크
    meta = _FakeMeta({"/f"})
    mgr = ReplicationManager(
        _FakeAuth(), "http://server", meta, storage_pool,
        chunk_size=64, min_replicas=1,
    )
    storage_pool.device_id = "self-dev"
    excludes: list[list[str]] = []

    async def register_chunk(token, chunk_id, file_ref, idx, size,
                             chunk_hash=None):
        return None

    async def placement(token, size, exclude):
        excludes.append(list(exclude))
        return [{"device_id": "devQ", "connection_address": "devQ"}]

    async def record_replica(token, chunk_id, device_id):
        return True

    async def relay_quota(device_id, op, payload):
        from stardustlib.relay_client import RelayOpError

        raise RelayOpError("Relay op failed (status=507): quota", status=507)

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    mgr._register_chunk = register_chunk
    mgr._placement = placement
    mgr._record_replica = record_replica
    mgr._relay_op = relay_quota
    mgr._client.post = boom
    try:
        result = mgr.replicate("/f")
    finally:
        mgr.close()

    assert result.status == "pending"
    assert len(excludes) >= 2
    assert "devQ" not in excludes[0]      # 첫 청크는 후보로 요청
    assert "devQ" in excludes[1]          # 507 관측 후 배제
    assert "self-dev" in excludes[1]      # 자기 device 제외는 유지


def test_placement_requests_spare_candidates(key):
    """placement는 목표 복제본 수보다 여유 있게 후보를 요청한다."""
    from stardustlib import replication_manager as rm

    mgr = _bare_manager(key)
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"holders": []}

    async def post(url, json=None, headers=None, **k):
        captured.update(json or {})
        return _Resp()

    mgr._client.post = post
    import asyncio as _asyncio

    _asyncio.run(mgr._placement("tok", 100, ["x"]))
    assert captured["count"] == mgr.min_replicas + rm.PLACEMENT_SPARE
    assert captured["exclude"] == ["x"]


def test_file_ref_does_not_leak_path(key):
    storage_pool = _FakeStoragePool(key)
    meta = _FakeMeta(set())
    mgr, _cloud = _manager(storage_pool, meta, [])
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

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"x" * 200)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
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
    storage_pool = _FakeStoragePool(key)
    original = b"integrity-check" * 20
    storage_pool.put("/f", original)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        # 첫 홀더가 손상된 바이트를 반환하도록 만든다
        cloud.corrupt.add("h1")
        storage_pool.write_file("/f", b"")  # 로컬 원본 훼손
        nbytes = mgr.recover("/f")
    finally:
        mgr.close()

    assert nbytes == len(original)
    assert storage_pool.read_file("/f") == original


def test_recover_fails_when_all_holders_corrupt(key):
    """모든 홀더가 손상이면 누락 chunk_id를 명시한 RecoveryError를 낸다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"y" * 200)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
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
    storage_pool = _FakeStoragePool(key)
    original = b"legacy-chunk" * 10
    storage_pool.put("/f", original)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
        # 서버가 해시를 모르는 상태(구버전/레거시)로 만든다
        for info in cloud.chunk_meta[mgr._file_ref("/f")]:
            info["hash"] = None
        storage_pool.write_file("/f", b"")
        nbytes = mgr.recover("/f")
    finally:
        mgr.close()

    assert nbytes == len(original)
    assert storage_pool.read_file("/f") == original


def test_heal_does_not_copy_corrupt_chunk(key):
    """재복제가 손상된 소스를 배제하고 정상 소스만 새 홀더로 복사한다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"z" * 100)
    meta = _FakeMeta({"/f"})
    # 홀더 3개로 복제 후, 1곳을 손상시키고 새 홀더(h4)를 추가해 보충하게 한다
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
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
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"w" * 100)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
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

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"at-rest-bytes" * 30)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    infos = sorted(cloud.chunk_meta[mgr._file_ref("/f")], key=lambda c: c["idx"])
    rejoined = chunker.join([
        (c["idx"], cloud.holder_store["h1"][c["chunk_id"]]) for c in infos
    ])
    assert rejoined == storage_pool.read_ciphertext("/f")


def test_recover_restores_identical_at_rest_bytes(key):
    """복구는 받은 암호문을 그대로 기록해 at-rest 바이트를 동일하게 되돌린다.

    재암호화하면 등록된 청크 해시와 at-rest가 어긋나므로, 동일성이 중요하다.
    """
    storage_pool = _FakeStoragePool(key)
    original = b"restore-identical" * 25
    storage_pool.put("/f", original)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    before = storage_pool.read_ciphertext("/f")
    try:
        mgr.replicate("/f")
        storage_pool.write_file("/f", b"")  # 로컬 훼손(다른 암호문으로 덮어씀)
        assert storage_pool.read_ciphertext("/f") != before
        mgr.recover("/f")
    finally:
        mgr.close()

    assert storage_pool.read_ciphertext("/f") == before  # 바이트 동일 복원
    assert storage_pool.read_file("/f") == original


def test_replicate_needs_no_encryption_engine(key):
    """복제는 at-rest 암호문만 다루므로 암호화 엔진 없이도 동작한다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"opaque" * 20)
    meta = _FakeMeta({"/f"})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    mgr._engine = None  # 복제 경로는 엔진에 의존하지 않는다
    try:
        result = mgr.replicate("/f")
    finally:
        mgr.close()
    assert result.status == "replicated"


# --- 원본 보관 device는 홀더 후보에서 제외 (Property 2) ---

def _chunk_refs(count: int, device_id: str | None) -> list:
    """ChunkRef 목록을 만든다(보관 device 지정)."""
    from stardustlib.models import ChunkRef

    return [
        ChunkRef(index=i, chunk_ref=f"ref{i}", source_id="s1",
                 device_id=device_id, size=64)
        for i in range(count)
    ]


def test_origin_device_excluded_from_placement(key):
    """청크 원본을 보관한 device는 그 청크의 홀더 후보에서 빠진다."""
    storage_pool = _FakeStoragePool(key)
    content = b"o" * 200
    storage_pool.put("/f", content)
    # 청크 원본이 h1에 있다고 알린다(스필오버 등)
    meta = _FakeMeta({"/f"}, {"/f": _chunk_refs(10, "h1")})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3", "h4"])
    seen_excludes: list[list[str]] = []
    inner = mgr._placement

    async def spy(token, size, exclude):
        seen_excludes.append(list(exclude))
        return await inner(token, size, exclude)

    mgr._placement = spy
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    assert seen_excludes
    assert all("h1" in e for e in seen_excludes)
    # h1에는 사본을 만들지 않는다
    assert "h1" not in cloud.holder_store


def test_local_chunks_exclude_self_device(key):
    """청크 device_id가 NULL(로컬)이면 자기 device를 원본으로 보고 제외한다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"l" * 200)
    storage_pool.device_id = "self-dev"
    meta = _FakeMeta({"/f"}, {"/f": _chunk_refs(10, None)})
    mgr, _cloud = _manager(storage_pool, meta, ["self-dev", "h2", "h3", "h4"])
    seen: list[list[str]] = []
    inner = mgr._placement

    async def spy(token, size, exclude):
        seen.append(list(exclude))
        return await inner(token, size, exclude)

    mgr._placement = spy
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    assert seen and all("self-dev" in e for e in seen)


def test_no_holder_after_exclusion_is_pending_with_reason(key, caplog):
    """제외 후 후보가 없으면 pending이고 사유를 경고로 남긴다(조용한 성공 금지)."""
    import logging

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"n" * 200)
    # 홀더가 h1 하나뿐인데 그 h1이 원본 보관처 → 후보 없음
    meta = _FakeMeta({"/f"}, {"/f": _chunk_refs(10, "h1")})
    mgr, cloud = _manager(storage_pool, meta, ["h1"])
    with caplog.at_level(logging.WARNING,
                         logger="stardustlib.replication_manager"):
        try:
            result = mgr.replicate("/f")
        finally:
            mgr.close()

    assert result.status == "pending"
    assert result.no_holder_chunks == result.chunk_count
    assert not cloud.holder_store  # 어디에도 저장하지 않았다
    assert any("배치 후보가 없는 청크" in r.getMessage()
               for r in caplog.records)


def test_heal_excludes_origin_device(key):
    """재복제에서도 원본 보관 device를 후보에서 뺀다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"h" * 200)
    meta = _FakeMeta({"/f"}, {"/f": _chunk_refs(10, "h1")})
    mgr, cloud = _manager(storage_pool, meta, ["h2", "h3", "h4"])
    try:
        mgr.replicate("/f")
        cloud.holders.append("h1")   # h1이 후보 목록에 들어와도
        cloud.offline.update({"h2"})  # degraded 유발
        seen: list[list[str]] = []
        inner = mgr._placement

        async def spy(token, size, exclude):
            seen.append(list(exclude))
            return await inner(token, size, exclude)

        mgr._placement = spy
        mgr.ensure_replicas("/f")
    finally:
        mgr.close()

    assert seen and all("h1" in e for e in seen)


def test_no_chunk_records_keeps_previous_behaviour(key):
    """청크 레코드가 없으면(원본 위치 미상) 기존 배치 동작 그대로."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"p" * 200)
    meta = _FakeMeta({"/f"})  # get_chunks → 빈 목록
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        result = mgr.replicate("/f")
    finally:
        mgr.close()

    assert result.status == "replicated"
    assert result.no_holder_chunks == 0


# --- 청크 단위 분담: 로컬 청크만 올리고 상태는 레지스트리로 판정 ---

def test_replicate_skipped_when_no_local_chunks(key):
    """청크가 전부 다른 device 보관이면 올리지 않고 skipped로 끝낸다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"r" * 200)
    storage_pool.device_id = "self-dev"
    # 모든 청크를 원격 보관으로 지정
    storage_pool.chunk_devices = {i: "other-dev" for i in range(10)}
    meta = _FakeMeta({"/f"}, {"/f": _chunk_refs(10, "other-dev")})
    mgr, cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        result = mgr.replicate("/f")
    finally:
        mgr.close()

    assert result.status == "skipped"
    assert result.chunk_count == 0
    assert not cloud.holder_store        # 아무것도 전송하지 않았다
    assert "/f" not in meta.status       # 복제 상태를 바꾸지 않는다


def test_replicate_uploads_only_local_chunks(key):
    """청크가 두 device에 나뉘면 자기 몫만 올린다(원격 읽기 없음)."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"s" * 400)
    storage_pool.device_id = "self-dev"
    from stardustlib import chunker
    from stardustlib.models import ChunkRef

    # 짝수 index만 로컬, 홀수는 다른 기기 보관
    all_parts = chunker.split(storage_pool.read_ciphertext("/f"), 64)
    remote = {i: "other-dev" for i, _d in all_parts if i % 2}
    storage_pool.chunk_devices = remote
    refs = [
        ChunkRef(index=i, chunk_ref=f"r{i}", source_id="s1",
                 device_id=remote.get(i), size=64)
        for i, _d in all_parts
    ]
    meta = _FakeMeta({"/f"}, {"/f": refs})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    assert storage_pool.read_indices  # 로컬 청크만 읽었다
    assert all(i % 2 == 0 for i in storage_pool.read_indices)


def test_warns_once_when_self_device_unknown(key, caplog):
    """자기 device_id를 모르면 1회 경고한다(배치에서 자기 기기 제외 불가)."""
    import logging

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"w" * 200)
    # device_id 미설정(단발 세션이 자기 device를 특정하지 못한 상황)
    meta = _FakeMeta({"/f"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    with caplog.at_level(logging.WARNING,
                         logger="stardustlib.replication_manager"):
        try:
            mgr.replicate("/f")
            mgr.replicate("/f")  # 두 번째 호출에서 중복 경고 없음
        finally:
            mgr.close()

    warnings = [
        r for r in caplog.records if "자기 device_id" in r.getMessage()
    ]
    assert len(warnings) == 1


# --- 읽기 단계 진행 로그 / 지연 홀더 로그 ---

def test_read_stage_logs_progress_for_large_file(key, caplog):
    """읽기 구간도 진행 로그를 남긴다(전송 전 공백 방지)."""
    import logging

    from stardustlib import replication_manager as rm

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/big", b"R" * (64 * rm.PROGRESS_MIN_CHUNKS * 2))
    meta = _FakeMeta({"/big"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    with caplog.at_level(logging.INFO, logger="stardustlib.replication_manager"):
        try:
            mgr.replicate("/big")
        finally:
            mgr.close()

    assert [r for r in caplog.records if "복제 읽기" in r.getMessage()]


def test_read_stage_quiet_for_small_file(key, caplog):
    """청크가 적으면 읽기 진행 로그를 남기지 않는다."""
    import logging

    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/small", b"r" * 100)
    meta = _FakeMeta({"/small"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    with caplog.at_level(logging.INFO, logger="stardustlib.replication_manager"):
        try:
            mgr.replicate("/small")
        finally:
            mgr.close()

    assert not [r for r in caplog.records if "복제 읽기" in r.getMessage()]


@pytest.mark.asyncio
async def test_slow_holder_is_logged(key, caplog, monkeypatch):
    """직접+릴레이 타임아웃을 소진한 홀더를 지연으로 남긴다."""
    import logging

    from stardustlib import replication_manager as rm

    mgr = _bare_manager(key)
    monkeypatch.setattr(rm, "SLOW_HOLDER_SECONDS", 0.0)  # 항상 지연으로 판정

    async def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    async def slow_relay(device_id, op, payload):
        raise OSError("Relay timeout: target device did not respond")

    mgr._client.post = boom
    mgr._relay_op = slow_relay
    with caplog.at_level(logging.WARNING,
                         logger="stardustlib.replication_manager"):
        ok = await mgr._holder_store("devS", "1.2.3.4:9090", "c1", b"x", "tok")

    assert ok is False
    assert any("홀더 전송 지연" in r.getMessage() for r in caplog.records)


def test_progress_tracked_and_cleared(key):
    """복제 중 진행이 갱신되고 끝나면 비활성으로 정리된다(Property 4)."""
    from stardustlib.replication_progress import ProgressTracker

    tracker = ProgressTracker()
    seen: list[tuple[str, int, int]] = []
    real_advance = tracker.advance

    def spy(done, secured=None):
        real_advance(done, secured)
        snap = tracker.snapshot()
        if snap:
            seen.append((snap.stage, snap.done, snap.total))

    tracker.advance = spy
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"g" * 300)
    meta = _FakeMeta({"/f"})
    mgr = ReplicationManager(
        _FakeAuth(), "http://server", meta, storage_pool,
        chunk_size=64, min_replicas=3, progress=tracker,
    )
    _Cloud(["h1", "h2", "h3"]).attach(mgr)
    try:
        mgr.replicate("/f")
    finally:
        mgr.close()

    assert seen  # 진행이 갱신됐다
    assert any(stage == "storing" for stage, _d, _t in seen)
    assert tracker.snapshot() is None  # 종료 후 정리


def test_progress_cleared_on_failure(key):
    """복제가 예외로 끝나도 진행 표시가 남지 않는다."""
    from stardustlib.replication_progress import ProgressTracker

    tracker = ProgressTracker()
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"x" * 100)
    meta = _FakeMeta({"/f"})
    mgr = ReplicationManager(
        _FakeAuth(), "http://server", meta, storage_pool,
        chunk_size=64, min_replicas=1, progress=tracker,
    )

    def boom(*_a, **_k):
        raise OSError("read failed")

    storage_pool.read_chunks = boom
    try:
        with pytest.raises(OSError):
            mgr.replicate("/f")
    finally:
        mgr.close()

    assert tracker.snapshot() is None


def test_progress_optional(key):
    """추적기를 주입하지 않아도 복제는 그대로 동작한다."""
    storage_pool = _FakeStoragePool(key)
    storage_pool.put("/f", b"n" * 200)
    meta = _FakeMeta({"/f"})
    mgr, _cloud = _manager(storage_pool, meta, ["h1", "h2", "h3"])
    try:
        assert mgr.replicate("/f").status == "replicated"
    finally:
        mgr.close()
