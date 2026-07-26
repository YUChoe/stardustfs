"""리플리케이션 매니저 (백업/복구 오케스트레이션).

replicate(virtual_path): 파일 평문을 자체 포함 암호문 blob으로 암호화 → 고정 크기
청크로 분할 → 서버에 청크 등록 + placement 요청 → 배치된 홀더에 직접 push →
ack 수집 → 레지스트리 확정 → 모든 청크가 ≥min_replicas 확보 시 replicated, 아니면
pending(경고).

recover(virtual_path): 서버에서 청크 목록 조회 → 청크별 도달 가능한 홀더에서 fetch →
결합 → 복호화 → 로컬 복원. 도달 가능한 홀더가 없는 청크가 있으면 규격 에러(누락
chunk_id 명시).

서버는 청크 내용/키를 저장하지 않고(위치/크기/회계만), 홀더는 복호화 불가능한 암호문만
보관한다(zero-knowledge 유지). file_ref/chunk_id는 가상경로의 SHA-256 해시라 서버에
경로를 노출하지 않는다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from stardustlib import chunker, replication_progress
from stardustlib.auth_client import AuthClient
from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import AuthenticationError
from stardustlib.remote_source import _EventLoopThread, direct_tcp_viable

logger = logging.getLogger(__name__)

DEFAULT_MIN_REPLICAS = 1  # 원본 외 추가 사본 수. 서버 /replication/policy로 재정의됨.
# 홀더 직접 연결 시도 타임아웃(초). 짧게 잡아 도달 불가 시 릴레이로 빨리 fallback.
DIRECT_HOLDER_TIMEOUT = 3.0
# 보관 한도 초과(507)를 응답한 홀더를 배치 후보에서 제외하는 기간(초). 서버 회계가
# 홀더 실제 한도보다 낙관적일 때(신고값 노후화 등) 같은 홀더로 매 청크·매 주기
# 재시도하는 폭주를 막는다. 만료되면 다시 후보가 되어 공간 회수를 반영한다.
QUOTA_BLOCK_SECONDS = 1800.0
# placement에 목표 복제본 수보다 여유 있게 후보를 요청한다. 일부 홀더가 쿼터 초과
# (507)나 도달 불가로 실패해도 같은 청크에서 대체 홀더로 넘어갈 수 있다.
PLACEMENT_SPARE = 2
# 홀더 보관 한도 초과 상태 코드(p2p replica_store).
_QUOTA_STATUS = 507
# 청크가 이 수 이상인 파일은 복제 진행 로그를 남긴다(대용량 파일 무응답 오인 방지).
PROGRESS_MIN_CHUNKS = 20
# 한 청크 전송이 이 시간을 넘기면 지연으로 보고 홀더를 로그에 남긴다.
# 직접 TCP(3s) + 릴레이 long-poll(35s)을 모두 소진한 경우를 잡는 기준.
SLOW_HOLDER_SECONDS = 30.0
# 한 파일 복제 중 남길 진행 로그 횟수(진행률 대략 1/N 단위).
PROGRESS_REPORTS = 10


class ReplicationError(Exception):
    """리플리케이션 처리 실패(설정/암호화 등)."""


class RecoveryError(Exception):
    """복구 실패. 도달 불가로 누락된 청크가 있을 때."""

    def __init__(self, message: str, missing_chunks: list[str]) -> None:
        super().__init__(message)
        self.missing_chunks = missing_chunks


@dataclass
class ReplicationResult:
    """replicate 결과. status는 replicated|pending."""

    status: str
    chunk_count: int
    min_replicas: int
    replicas_per_chunk: list[int] = field(default_factory=list)
    # 제외 규칙 적용 후 배치 후보가 하나도 없던 청크 수(조용한 실패 방지).
    no_holder_chunks: int = 0


@dataclass
class HealthSummary:
    """비파괴적 건강성 점검 결과(재복제 없이 현황만)."""

    degraded: bool       # 청크 중 하나라도 online 복제 수 < min_replicas
    chunk_count: int
    min_online: int      # 청크별 online 복제 수의 최소값


@dataclass
class HealReport:
    """ensure_replicas(재복제) 결과."""

    status: str  # replicated|pending
    chunk_count: int
    min_replicas: int
    repaired: int = 0  # 복제본을 추가한 청크 수
    unrecoverable: list[str] = field(default_factory=list)  # 도달 소스 없는 청크


class ReplicationManager:
    """암호화 청크 복제/복구 오케스트레이터."""

    def __init__(
        self,
        auth_client: AuthClient,
        server_url: str,
        metadata_store: Any,
        storage_pool: Any,
        *,
        chunk_size: int = chunker.DEFAULT_CHUNK_SIZE,
        min_replicas: int = DEFAULT_MIN_REPLICAS,
        timeout: float = 10.0,
        max_concurrent_repair: int = 4,
        io: _EventLoopThread | None = None,
        progress=None,
    ) -> None:
        self._auth = auth_client
        self._server_url = server_url.rstrip("/")
        self._meta = metadata_store
        self._storage_pool = storage_pool
        self._engine = getattr(storage_pool, "encryption_engine", None)
        self._chunk_size = chunk_size
        self._min_replicas = min_replicas
        self._timeout = timeout
        self._max_concurrent_repair = max_concurrent_repair
        self._io = io or _EventLoopThread.get_instance()
        self._client = httpx.AsyncClient(timeout=timeout)
        # 직접 UDP(홀펀칭) 전송 콜백: async (device_id, op, payload) -> (status, result).
        # 데몬이 HolePunchService.send_op를 주입한다. 같은 _io 루프에서 await된다.
        self._udp_send = None
        # 보관 한도 초과(507)를 낸 홀더 → 배제 만료 시각(monotonic 초).
        self._quota_blocked: dict[str, float] = {}
        # 자기 device_id 미확정 경고를 1회만 남기기 위한 플래그.
        self._warned_no_self_device = False
        # 진행 상태 추적기(선택). daemon이 주입하면 제어 채널·GUI에 노출된다.
        self._progress = progress

    # ------------------------------------------------------------------
    # 식별자 (서버에 가상경로 비노출 — SHA-256 해시)
    # ------------------------------------------------------------------

    @property
    def min_replicas(self) -> int:
        """목표 복제본 수."""
        return self._min_replicas

    def set_min_replicas(self, n: int) -> None:
        """목표 복제본 수를 갱신한다(정책 변경 반영)."""
        if n >= 1:
            self._min_replicas = n

    def _mark_quota_blocked(self, device_id: str) -> None:
        """보관 한도를 초과한 홀더를 QUOTA_BLOCK_SECONDS 동안 배치 후보에서 뺀다.

        서버 회계(신고 제공 용량)가 홀더의 실제 한도보다 낙관적일 때 배치가 계속
        같은 홀더를 지목하므로, 클라이언트가 직접 기억해 재시도를 낭비하지 않는다.
        """
        first_time = device_id not in self._quota_blocked
        self._quota_blocked[device_id] = time.monotonic() + QUOTA_BLOCK_SECONDS
        if first_time:
            logger.warning(
                "홀더 보관 한도 초과(507) — %.0f분간 배치 제외: dev=%s. "
                "홀더의 제공 용량 신고가 실제 한도보다 큰 상태일 수 있습니다",
                QUOTA_BLOCK_SECONDS / 60, device_id,
            )

    def _resolve_file_status(
        self, file_ref: str, result: ReplicationResult
    ) -> str:
        """파일 전체의 복제 상태를 서버 레지스트리 기준으로 판정한다.

        각 device는 자기 로컬 청크만 올리므로, 이 device의 전송 결과만으로는 파일이
        완료됐는지 알 수 없다(다른 device가 나머지를 올린다). 레지스트리에 등록된
        모든 청크가 min_replicas 이상이면 replicated다.

        조회 실패 시에는 이 device의 전송 결과를 그대로 쓴다(보수적).
        """
        try:
            summary = self._io.run_coroutine(self._health(file_ref))
        except Exception as e:  # noqa: BLE001 — 판정 실패는 비치명
            logger.debug("복제 상태 조회 실패, 로컬 결과 사용: %s", e)
            return result.status
        if summary.chunk_count == 0:
            return result.status
        return "pending" if summary.degraded else "replicated"

    def _origin_devices(self, virtual_path: str) -> dict[int, str]:
        """청크 index → 원본을 보관한 device_id.

        `file_chunks.device_id`가 NULL이면 이 device 로컬이므로 자기 device_id로
        채운다. 청크 레코드가 없으면 빈 맵이다(원본 위치를 알 수 없어 제외 불가).

        소유는 사용자 단위이므로 "실행 중인 기기"가 아니라 이 원본 위치가 홀더
        배제의 기준이다 — 원본이 있는 기기에 사본을 두면 내구성 이득이 없다.
        """
        get_chunks = getattr(self._meta, "get_chunks", None)
        if not callable(get_chunks):
            return {}
        self_dev = getattr(self._storage_pool, "device_id", None)
        origins: dict[int, str] = {}
        for chunk in get_chunks(virtual_path):
            device_id = getattr(chunk, "device_id", None) or self_dev
            if device_id:
                origins[chunk.index] = device_id
        return origins

    def quota_blocked_devices(self) -> list[str]:
        """현재 보관 한도 초과로 배제 중인 홀더 목록(만료분은 정리한다)."""
        now = time.monotonic()
        for device_id in [
            d for d, until in self._quota_blocked.items() if until <= now
        ]:
            del self._quota_blocked[device_id]
        return list(self._quota_blocked)

    def set_udp_transport(self, fn) -> None:
        """직접 UDP(홀펀칭) 전송 콜백을 설정한다.

        fn은 async (device_id, op, payload) -> (status, result). 홀더 전송 시 직접
        TCP가 도달 불가하면 릴레이 전에 이 경로를 시도한다(직접 우선, 릴레이 최후).
        """
        self._udp_send = fn

    def _file_ref(self, virtual_path: str) -> str:
        uid = self._auth.user_id or ""
        return hashlib.sha256(
            f"{uid}:{virtual_path}".encode("utf-8")
        ).hexdigest()

    def _chunk_id(self, file_ref: str, idx: int) -> str:
        return hashlib.sha256(
            f"{file_ref}:{idx}".encode("utf-8")
        ).hexdigest()

    # ------------------------------------------------------------------
    # 공개 동기 API
    # ------------------------------------------------------------------

    def replicate(self, virtual_path: str) -> ReplicationResult:
        """이 device가 보관한 청크를 ≥min_replicas 홀더에 복제한다.

        로컬 I/O(읽기·암호화·상태 기록)는 호출 스레드에서, 네트워크는 IO 루프에서
        수행한다. 파일이 없으면 FileNotFoundError, 암호화 미설정 시 ReplicationError.

        올릴 로컬 청크가 없으면(청크가 전부 다른 device 보관) status="skipped"로
        끝내고 복제 상태를 바꾸지 않는다 — 그 기기가 자기 몫을 올린다.
        """
        meta = self._meta.lookup(virtual_path)
        if meta is None:
            raise FileNotFoundError(virtual_path)

        try:
            return self._replicate_tracked(virtual_path)
        finally:
            # 성공·실패·예외 어느 경로에서도 진행 표시를 정리한다.
            self._progress_call("finish")

    def _replicate_tracked(self, virtual_path: str) -> ReplicationResult:
        """replicate 본체. 진행 정리는 호출자(replicate)가 책임진다."""
        file_ref = self._file_ref(virtual_path)
        self._progress_call(
            "begin", virtual_path, 0, replication_progress.STAGE_READING
        )
        chunks = self._chunks_to_replicate(virtual_path)
        origins = self._origin_devices(virtual_path)
        if not chunks:
            logger.info(
                "로컬 청크 없음, 백업 건너뜀(보관 기기가 담당): %s", virtual_path
            )
            return ReplicationResult(
                status="skipped", chunk_count=0,
                min_replicas=self._min_replicas,
            )

        self._progress_call(
            "set_stage", replication_progress.STAGE_STORING, len(chunks)
        )
        result = self._io.run_coroutine(
            self._replicate_chunks(file_ref, chunks, origins)
        )
        # 이 device는 자기 청크만 올렸다. 파일 전체 상태는 서버 레지스트리로
        # 판정해야 다른 device가 올린 몫이 반영된다.
        result.status = self._resolve_file_status(file_ref, result)
        self._meta.set_replication_status(virtual_path, result.status)
        if result.status != "replicated":
            blocked = self.quota_blocked_devices()
            reason = (
                f" — 보관 한도 초과로 배제된 홀더: {blocked}" if blocked else ""
            )
            if result.no_holder_chunks:
                reason += (
                    f" — 배치 후보가 없는 청크 {result.no_holder_chunks}개"
                    f"(원본 보관 기기·자기 기기·한도 초과 기기를 제외하면 남는"
                    f" 홀더가 없습니다)"
                )
            # 청크가 많으면 복제수 목록이 장문이 되므로 최소/최대만 요약한다.
            counts = result.replicas_per_chunk
            logger.warning(
                "파일 복제 미완료(pending): %s — 청크 %d개, 복제수 min=%d max=%d "
                "(목표 %d)%s",
                virtual_path, len(counts),
                min(counts) if counts else 0, max(counts) if counts else 0,
                self._min_replicas, reason,
            )
        return result

    def _chunks_to_replicate(self, virtual_path: str) -> list:
        """이 device가 올릴 청크 목록 (idx, 암호문)을 만든다.

        at-rest 청크를 그대로 복제 청크로 쓴다(재분할 없음). 저장·전송·복제가 같은
        경계를 공유하므로 복구 후 바이트가 그대로 일치하고 청크 해시도 재계산 없이
        유효하다.

        원격 device가 보관한 청크는 읽지 않는다. 데이터를 갖지 않은 기기가 원본을
        릴레이로 당겨오는 왕복을 막기 위해서다(그 청크는 보관 기기가 올린다).
        올릴 로컬 청크가 없으면 빈 목록을 돌려주고 호출자가 skipped로 끝낸다.
        """
        read_chunks = getattr(self._storage_pool, "read_chunks", None)
        if not callable(read_chunks):
            return []
        parts = read_chunks(
            virtual_path, local_only=True,
            on_progress=self._make_read_reporter(),
        )
        return sorted(parts, key=lambda p: p[0])

    def _progress_call(self, method: str, *args) -> None:
        """진행 추적기 호출(미주입이면 no-op). 추적 실패가 복제를 막지 않는다."""
        tracker = self._progress
        if tracker is None:
            return
        try:
            getattr(tracker, method)(*args)
        except Exception as e:  # noqa: BLE001 — 추적은 부가 기능
            logger.debug("진행 추적 실패(%s): %s", method, e)

    def _make_read_reporter(self):
        """읽기 단계 진행 로그·추적 콜백. 청크가 적으면 로그는 남기지 않는다."""
        state = {"every": None}

        def report(done: int, total: int) -> None:
            self._progress_call("advance", done)
            if state["every"] is None:
                state["every"] = (
                    max(1, total // PROGRESS_REPORTS)
                    if total >= PROGRESS_MIN_CHUNKS else 0
                )
                if state["every"]:
                    self._progress_call(
                        "set_stage", replication_progress.STAGE_READING, total
                    )
            every = state["every"]
            if every and (done % every == 0 or done == total):
                logger.info("복제 읽기: %d/%d 청크", done, total)

        return report

    def recover(self, virtual_path: str) -> int:
        """복제본에서 파일을 복구해 로컬에 기록한다. 기록 바이트 수를 반환한다.

        받은 청크가 at-rest 청크 표현이면 청크 그대로 되돌려 기록하고, 레거시 통짜
        blob이면 이어붙여 단일 블록으로 기록한다. 어느 경우든 재암호화하지 않으므로
        at-rest 바이트가 복제 시점과 동일하게 유지된다.

        도달 가능한 홀더가 없는 청크가 있으면 RecoveryError(누락 chunk_id 명시).
        """
        if self._engine is None:
            raise ReplicationError("암호화 엔진이 없어 복구할 수 없습니다")
        file_ref = self._file_ref(virtual_path)
        parts = self._io.run_coroutine(self._recover_chunks(file_ref))

        if self._is_chunked_set(parts):
            # 청크별로 복호화해 검증하고 평문 크기를 합산한다(청크는 단독 복호화 가능).
            ciphers = [data for _idx, data in parts]
            plain_size = sum(len(self._engine.decrypt(c)) for c in ciphers)
            self._storage_pool.write_chunks(virtual_path, ciphers, plain_size)
            return plain_size

        # 레거시 통짜 blob: 이어붙인 뒤 한 번 복호화해 검증한다.
        blob = chunker.join(parts)
        plaintext = self._engine.decrypt(blob)
        self._storage_pool.write_ciphertext(virtual_path, blob, len(plaintext))
        return len(plaintext)

    @staticmethod
    def _is_chunked_set(parts: list) -> bool:
        """받은 청크들이 at-rest 청크 표현인지 판정한다.

        청크 표현이면 청크마다 독립 암호화되어 각각 암호문 헤더(매직)로 시작한다.
        레거시 blob을 고정 크기로 나눈 조각은 첫 조각만 매직으로 시작한다.
        조각이 하나뿐이면 두 표현이 동일하므로 단일 blob 경로로 처리한다.
        """
        if len(parts) < 2:
            return False
        return all(
            data[:4] == EncryptionEngine.MAGIC for _idx, data in parts
        )

    def ensure_replicas(self, virtual_path: str) -> HealReport:
        """건강성을 점검해 부족한 청크의 복제본을 채운다(재복제).

        청크는 불변이므로 온라인 홀더에서 받아(재암호화 없이) 새 홀더로 복사한다.
        온라인 홀더가 없는 청크는 unrecoverable로 보고한다. 모든 청크가
        min_replicas를 충족하면 replicated, 아니면 pending으로 표시한다.
        """
        report = self._io.run_coroutine(
            self._ensure_chunks(
                self._file_ref(virtual_path),
                self._origin_devices(virtual_path),
            )
        )
        # 메타데이터가 있는 파일이면 상태를 갱신한다(복구 전용 호출은 없을 수 있음).
        if self._meta.lookup(virtual_path) is not None:
            self._meta.set_replication_status(virtual_path, report.status)
        if report.status != "replicated":
            logger.warning(
                "재복제 후에도 미충족(pending): %s — 복구 불가 청크 %d개",
                virtual_path, len(report.unrecoverable),
            )
        return report

    def replication_health(self, virtual_path: str) -> HealthSummary:
        """재복제 없이 현재 복제 건강성만 점검한다(유예 게이트용)."""
        return self._io.run_coroutine(
            self._health(self._file_ref(virtual_path))
        )

    def close(self) -> None:
        """내부 httpx 클라이언트를 닫는다."""
        try:
            self._io.run_coroutine(self._client.aclose())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 오케스트레이션 (async)
    # ------------------------------------------------------------------

    async def _replicate_chunks(
        self, file_ref: str, chunks: list[tuple[int, bytes]],
        origins: dict[int, str] | None = None,
    ) -> ReplicationResult:
        token = await self._token()
        # 소유자 자신의 device는 홀더에서 제외한다(자기 기기에 백업은 무의미·헤어핀 실패).
        self_dev = getattr(self._storage_pool, "device_id", None)
        base_exclude = [self_dev] if self_dev else []
        if not self_dev and not self._warned_no_self_device:
            self._warned_no_self_device = True
            logger.warning(
                "자기 device_id를 알 수 없어 배치에서 자기 기기를 제외하지 못합니다. "
                "원본 보관 기기 제외 규칙에만 의존합니다(daemon 미등록 상태일 수 있음)"
            )
        origins = origins or {}
        # pending 재시도에서 이미 확보한 청크를 다시 올리지 않기 위한 기존 등록 정보.
        registered = await self._registered_by_idx(token, file_ref)
        replicas_per_chunk: list[int] = []
        skipped = 0
        no_holder = 0
        total = len(chunks)
        # 큰 파일은 한 사이클이 수 분 걸리므로 진행 상황을 주기적으로 남긴다
        # (요청은 받았는데 아무 로그도 없어 멈춘 것처럼 보이는 것을 막는다).
        report_every = max(1, total // PROGRESS_REPORTS) if total >= (
            PROGRESS_MIN_CHUNKS
        ) else 0
        for done, (idx, data) in enumerate(chunks, start=1):
            chunk_id = self._chunk_id(file_ref, idx)
            digest = chunker.chunk_hash(data)
            # 같은 내용으로 이미 목표 복제본을 확보한 청크는 재전송하지 않는다.
            # 해시가 다르면(파일 수정) 등록된 사본은 낡은 것이므로 다시 올린다.
            prior = registered.get(idx)
            if prior is not None and prior.get("hash") == digest:
                online = await self._online_replicas(token, chunk_id)
                if online >= self._min_replicas:
                    replicas_per_chunk.append(online)
                    skipped += 1
                    self._report_store_progress(done, replicas_per_chunk)
                    self._log_progress(
                        done, total, report_every, replicas_per_chunk
                    )
                    continue
            await self._register_chunk(
                token, chunk_id, file_ref, idx, len(data),
                chunk_hash=digest,
            )
            # 한도 초과로 배제 중인 홀더는 매 청크마다 다시 걸러낸다(같은 파일의
            # 첫 청크에서 507이 나면 나머지 청크는 그 홀더를 요청하지 않는다).
            exclude = base_exclude + self.quota_blocked_devices()
            # 이 청크의 원본을 보관한 기기도 제외한다(같은 기기 사본은 무의미).
            origin = origins.get(idx)
            if origin and origin not in exclude:
                exclude.append(origin)
            holders = await self._placement(token, len(data), exclude=exclude)
            if not holders:
                no_holder += 1
            placed = 0
            for holder in holders:
                if placed >= self._min_replicas:
                    break  # 목표 충족 — 여유 후보는 쓰지 않는다
                address = holder.get("connection_address")
                device_id = holder.get("device_id")
                if not device_id:
                    continue
                if await self._holder_store(
                    device_id, address, chunk_id, data, token
                ):
                    if await self._record_replica(token, chunk_id, device_id):
                        placed += 1
            replicas_per_chunk.append(placed)
            self._report_store_progress(done, replicas_per_chunk)
            self._log_progress(done, total, report_every, replicas_per_chunk)

        if skipped:
            logger.info(
                "복제 재개: 이미 확보된 청크 %d/%d개 재전송 생략",
                skipped, total,
            )
        ok = bool(replicas_per_chunk) and all(
            n >= self._min_replicas for n in replicas_per_chunk
        )
        return ReplicationResult(
            status="replicated" if ok else "pending",
            chunk_count=len(chunks),
            min_replicas=self._min_replicas,
            replicas_per_chunk=replicas_per_chunk,
            no_holder_chunks=no_holder,
        )

    def _log_progress(
        self, done: int, total: int, report_every: int,
        replicas_per_chunk: list[int],
    ) -> None:
        """대용량 파일 복제의 진행 상황을 주기적으로 남긴다(report_every=0이면 무음)."""
        if not report_every:
            return
        if done % report_every and done != total:
            return
        ok = sum(1 for n in replicas_per_chunk if n >= self._min_replicas)
        logger.info("복제 진행: %d/%d 청크 (목표 확보 %d)", done, total, ok)

    def _report_store_progress(
        self, done: int, replicas_per_chunk: list[int]
    ) -> None:
        """전송 단계 진행을 추적기에 반영한다(로그와 별개로 매 청크)."""
        secured = sum(1 for n in replicas_per_chunk if n >= self._min_replicas)
        self._progress_call("advance", done, secured)

    async def _registered_by_idx(
        self, token: str, file_ref: str
    ) -> dict[int, dict]:
        """서버에 등록된 이 파일의 청크를 idx로 색인한다(재개 판정용).

        조회 실패(오프라인·구버전 서버)는 비치명이다. 빈 dict를 돌려주면 모든 청크를
        다시 올리는 기존 동작이 된다.
        """
        try:
            infos = await self._list_chunks(token, file_ref)
        except Exception as e:  # noqa: BLE001 — 재개 최적화 실패는 비치명
            logger.debug("기존 청크 조회 실패, 전체 재전송: %s", e)
            return {}
        return {
            info["idx"]: info for info in infos if info.get("idx") is not None
        }

    async def _online_replicas(self, token: str, chunk_id: str) -> int:
        """청크의 온라인 복제본 수. 조회 실패 시 0(안전 측 — 다시 올린다)."""
        try:
            holders = await self._list_replicas(token, chunk_id)
        except Exception as e:  # noqa: BLE001 — 비치명
            logger.debug("복제본 조회 실패(%s): %s", chunk_id[:16], e)
            return 0
        return sum(1 for h in holders if h.get("is_online") is not False)

    async def _recover_chunks(self, file_ref: str) -> list:
        token = await self._token()
        chunk_infos = await self._list_chunks(token, file_ref)
        if not chunk_infos:
            raise RecoveryError(
                f"복구할 청크가 등록돼 있지 않습니다: file_ref={file_ref}", []
            )

        parts: list[tuple[int, bytes]] = []
        missing: list[str] = []
        for info in chunk_infos:
            chunk_id = info["chunk_id"]
            idx = info["idx"]
            data = await self._fetch_from_any_holder(
                token, chunk_id, info.get("hash")
            )
            if data is None:
                missing.append(chunk_id)
            else:
                parts.append((idx, data))

        if missing:
            raise RecoveryError(
                f"도달 가능한 홀더가 없는 청크 {len(missing)}개", missing
            )
        # 이어붙이지 않고 (idx, 암호문) 목록을 그대로 돌려준다. 호출자가 청크 표현
        # 여부를 판정해 청크로 되돌리거나 단일 블록으로 합친다.
        return sorted(parts, key=lambda p: p[0])

    async def _ensure_chunks(
        self, file_ref: str, origins: dict[int, str] | None = None
    ) -> HealReport:
        token = await self._token()
        chunk_infos = await self._list_chunks(token, file_ref)
        if not chunk_infos:
            raise RecoveryError(
                f"재복제할 청크가 없습니다: file_ref={file_ref}", []
            )

        sem = asyncio.Semaphore(self._max_concurrent_repair)
        origins = origins or {}

        async def heal(info: dict) -> dict:
            async with sem:  # 동시 재복제 상한
                return await self._heal_chunk(
                    token, info, origins.get(info.get("idx"))
                )

        outcomes = await asyncio.gather(*[heal(i) for i in chunk_infos])
        repaired = sum(1 for o in outcomes if o["added"] > 0)
        unrecoverable = [o["chunk_id"] for o in outcomes if not o["healthy"]]
        return HealReport(
            status="replicated" if not unrecoverable else "pending",
            chunk_count=len(chunk_infos),
            min_replicas=self._min_replicas,
            repaired=repaired,
            unrecoverable=unrecoverable,
        )

    async def _health(self, file_ref: str) -> HealthSummary:
        token = await self._token()
        chunk_infos = await self._list_chunks(token, file_ref)
        if not chunk_infos:
            return HealthSummary(degraded=False, chunk_count=0, min_online=0)
        min_online: int | None = None
        for info in chunk_infos:
            holders = await self._list_replicas(token, info["chunk_id"])
            online = sum(
                1 for h in holders if h.get("is_online") is not False
            )
            min_online = online if min_online is None else min(min_online, online)
        min_online = min_online or 0
        return HealthSummary(
            degraded=min_online < self._min_replicas,
            chunk_count=len(chunk_infos),
            min_online=min_online,
        )

    async def _heal_chunk(
        self, token: str, info: dict, origin: str | None = None
    ) -> dict:
        """한 청크의 복제본을 min_replicas까지 채운다(불변 청크 복사).

        origin은 이 청크의 원본을 보관한 device_id로, 배치 후보에서 제외한다.
        """
        chunk_id = info["chunk_id"]
        holders = await self._list_replicas(token, chunk_id)
        online = [
            h for h in holders
            if h.get("is_online") is not False and h.get("connection_address")
        ]
        current_devices = [h["device_id"] for h in holders if h.get("device_id")]
        need = self._min_replicas - len(online)
        if need <= 0:
            return {"chunk_id": chunk_id, "added": 0, "healthy": True}

        # 온라인 홀더에서 청크를 받아온다(재암호화 없이 그대로 복사).
        # 손상된 사본을 새 홀더로 퍼뜨리지 않도록 복사 전에 무결성을 검증한다.
        expected_hash = info.get("hash")
        data = None
        for h in online:
            candidate = await self._holder_fetch(
                h.get("device_id"), h.get("connection_address"), chunk_id, token
            )
            if candidate is None:
                continue
            if not self._verify_chunk(
                chunk_id, candidate, expected_hash, h.get("device_id")
            ):
                continue  # 손상된 소스 → 다음 온라인 홀더
            data = candidate
            break
        if data is None:
            # 도달 가능한 소스가 없어 복제 불가 — 데이터 자체는 다른 곳에 있을 수 있음.
            return {"chunk_id": chunk_id, "added": 0, "healthy": False}

        self_dev = getattr(self._storage_pool, "device_id", None)
        exclude = list(current_devices)
        if self_dev and self_dev not in exclude:
            exclude.append(self_dev)
        # 원본을 보관한 기기에는 사본을 두지 않는다.
        if origin and origin not in exclude:
            exclude.append(origin)
        # 한도 초과로 배제 중인 홀더는 재복제 후보에서도 뺀다.
        exclude += [
            d for d in self.quota_blocked_devices() if d not in exclude
        ]
        candidates = await self._placement(token, len(data), exclude=exclude)
        added = 0
        for cand in candidates:
            if added >= need:
                break
            address = cand.get("connection_address")
            device_id = cand.get("device_id")
            if not device_id:
                continue
            if await self._holder_store(
                device_id, address, chunk_id, data, token
            ):
                if await self._record_replica(token, chunk_id, device_id):
                    added += 1
        return {
            "chunk_id": chunk_id,
            "added": added,
            "healthy": (len(online) + added) >= self._min_replicas,
        }

    async def _fetch_from_any_holder(
        self, token: str, chunk_id: str, expected_hash: str | None = None
    ) -> bytes | None:
        """온라인·도달 가능한 홀더에서 청크를 받아 무결성 검증까지 통과한 것을 반환한다.

        expected_hash가 있으면 받은 바이트의 SHA-256과 비교해, 불일치하면 그 홀더의
        응답을 버리고 다음 홀더를 시도한다(손상된 사본 격리). expected_hash가 None인
        레거시 청크는 검증을 생략한다.
        """
        holders = await self._list_replicas(token, chunk_id)
        for holder in holders:
            if holder.get("is_online") is False:
                continue
            device_id = holder.get("device_id")
            address = holder.get("connection_address")
            if not device_id and not address:
                continue
            data = await self._holder_fetch(device_id, address, chunk_id, token)
            if data is None:
                continue
            if not self._verify_chunk(chunk_id, data, expected_hash, device_id):
                continue  # 손상된 사본 → 다음 홀더
            return data
        return None

    @staticmethod
    def _verify_chunk(
        chunk_id: str, data: bytes, expected_hash: str | None,
        device_id: str | None,
    ) -> bool:
        """받은 청크가 등록된 해시와 일치하는지 확인한다(해시 없으면 통과).

        불일치는 경고로 남긴다 — 조용히 성공 처리하지 않고 호출자가 다음 홀더를
        시도하거나 규격 에러로 종결한다.
        """
        if not expected_hash:
            return True  # 레거시 청크(해시 미등록) 또는 구버전 서버
        actual = chunker.chunk_hash(data)
        if actual == expected_hash:
            return True
        logger.warning(
            "청크 무결성 불일치 — 홀더 배제: chunk=%s holder=%s "
            "expected=%s actual=%s",
            chunk_id, device_id, expected_hash[:12], actual[:12],
        )
        return False

    # ------------------------------------------------------------------
    # 서버 제어 평면 호출 (단위 테스트에서 패치 가능)
    # ------------------------------------------------------------------

    async def _token(self) -> str:
        try:
            return await self._auth.get_valid_token()
        except AuthenticationError as e:
            raise ReplicationError(f"인증 실패: {e}") from e

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _register_chunk(
        self, token: str, chunk_id: str, file_ref: str, idx: int, size: int,
        chunk_hash: str | None = None,
    ) -> None:
        """청크를 서버 레지스트리에 등록한다(내용 해시 포함).

        chunk_hash는 복구·재복제에서 받은 바이트를 검증하는 데 쓰인다. 구버전 서버는
        이 필드를 무시한다(선택 필드).
        """
        payload: dict[str, Any] = {
            "chunk_id": chunk_id, "file_ref": file_ref,
            "idx": idx, "size": size,
        }
        if chunk_hash is not None:
            payload["hash"] = chunk_hash
        await self._client.post(
            f"{self._server_url}/replication/chunks",
            json=payload,
            headers=self._auth_headers(token),
        )

    async def _placement(
        self, token: str, size: int, exclude: list[str]
    ) -> list[dict]:
        """배치 후보를 받는다. 목표 복제본 수 + 여유분(PLACEMENT_SPARE)을 요청해
        일부 홀더가 실패해도 같은 청크에서 대체 홀더를 쓸 수 있게 한다."""
        resp = await self._client.post(
            f"{self._server_url}/replication/placement",
            json={
                "size": size,
                "count": self._min_replicas + PLACEMENT_SPARE,
                "exclude": exclude,
            },
            headers=self._auth_headers(token),
        )
        if resp.status_code != 200:
            return []
        return list(resp.json().get("holders", []))

    async def _record_replica(
        self, token: str, chunk_id: str, device_id: str
    ) -> bool:
        resp = await self._client.post(
            f"{self._server_url}/replication/replicas",
            json={"chunk_id": chunk_id, "holder_device_id": device_id},
            headers=self._auth_headers(token),
        )
        return resp.status_code == 200

    async def _list_chunks(self, token: str, file_ref: str) -> list[dict]:
        resp = await self._client.get(
            f"{self._server_url}/replication/chunks/{file_ref}",
            headers=self._auth_headers(token),
        )
        if resp.status_code != 200:
            return []
        return list(resp.json())

    async def _list_replicas(self, token: str, chunk_id: str) -> list[dict]:
        resp = await self._client.get(
            f"{self._server_url}/replication/replicas/{chunk_id}",
            headers=self._auth_headers(token),
        )
        if resp.status_code != 200:
            return []
        return list(resp.json())

    # ------------------------------------------------------------------
    # 홀더 직접 전송 (단위 테스트에서 패치 가능)
    # ------------------------------------------------------------------

    async def _relay_op(self, device_id: str, op: str, payload: dict) -> dict:
        """같은 사용자 device로 릴레이 op를 전달한다(직접 연결 실패 시 fallback).

        RelayClient는 status!=200/도달 불가 시 OSError를 던진다.
        """
        from stardustlib.relay_client import RelayClient

        relay = RelayClient(self._auth, self._server_url, device_id, self._io)
        return await relay.request_async(op, payload)

    async def _holder_store(
        self, device_id: str, address: str, chunk_id: str,
        data: bytes, token: str,
    ) -> bool:
        """홀더에 청크 암호문을 push한다. 직접 연결 실패 시 릴레이로 fallback한다.

        전송 순서: (1) 직접 TCP(광고 주소, 주로 LAN) → (2) 직접 UDP(홀펀칭) →
        (3) 릴레이(정책 허가 시, 최후). 각 단계에서 200=성공(True), 비-200(쿼터 등)은
        재경로 무의미하므로 False, 도달 불가(타임아웃/연결 실패)면 다음 단계로 넘어간다.

        보관 한도 초과(507)는 어느 경로에서 관측되든 그 홀더를 일정 시간 배치
        후보에서 배제해(quota_blocked) 같은 실패를 반복하지 않는다.
        """
        started = time.monotonic()
        try:
            return await self._holder_store_paths(
                device_id, address, chunk_id, data, token
            )
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_HOLDER_SECONDS:
                logger.warning(
                    "홀더 전송 지연 %.0f초(직접+릴레이 타임아웃 소진): dev=%s addr=%s",
                    elapsed, device_id, address,
                )

    async def _holder_store_paths(
        self, device_id: str, address: str, chunk_id: str,
        data: bytes, token: str,
    ) -> bool:
        """_holder_store의 전송 캐스케이드 본체(직접 TCP → UDP → 릴레이)."""
        encoded = base64.b64encode(data).decode("ascii")
        body = {"chunk_id": chunk_id, "data": encoded}
        authed = {**body, "auth_token": token}
        # (1) 직접 TCP — 도달 가능성 없는 주소(다른 네트워크의 사설 IP)는 건너뛴다
        if address and direct_tcp_viable(address):
            try:
                resp = await self._client.post(
                    f"http://{address}/p2p/replica_store",
                    json=authed, timeout=DIRECT_HOLDER_TIMEOUT,
                )
                if resp.status_code == _QUOTA_STATUS and device_id:
                    self._mark_quota_blocked(device_id)
                return resp.status_code == 200
            except (httpx.TimeoutException, httpx.NetworkError):
                pass  # 도달 불가 → 다음 경로
        # (2) 직접 UDP(홀펀칭)
        if self._udp_send is not None and device_id:
            try:
                status, _result = await self._udp_send(
                    device_id, "replica_store", authed
                )
                if status == _QUOTA_STATUS:
                    self._mark_quota_blocked(device_id)
                return status == 200  # 비-200(쿼터 등)은 릴레이해도 동일
            except Exception:  # noqa: BLE001 — 펀치/전송 실패 → 릴레이
                pass
        # (3) 릴레이(정책 허가 시) — 타 사용자 홀더면 소유자 토큰으로 인가
        if not device_id:
            return False
        try:
            await self._relay_op(device_id, "replica_store", authed)
            return True
        except Exception as e:  # noqa: BLE001
            # RelayOpError.status로 홀더 핸들러의 원래 상태를 구분한다.
            if getattr(e, "status", None) == _QUOTA_STATUS:
                self._mark_quota_blocked(device_id)
            else:
                logger.info(
                    "홀더 store 실패(direct+udp+relay) dev=%s addr=%s: %s",
                    device_id, address, e,
                )
            return False

    async def _holder_fetch(
        self, device_id: str, address: str, chunk_id: str, token: str
    ) -> bytes | None:
        """홀더에서 청크 암호문을 받는다.

        전송 순서: 직접 TCP → 직접 UDP(홀펀칭) → 릴레이(정책, 최후). 도달 불가면 다음
        단계로, 비-200은 재경로 무의미하므로 None. 모두 실패 시 None(다음 홀더로 진행).
        """
        body = {"chunk_id": chunk_id}
        authed = {**body, "auth_token": token}

        def _data_from(result: dict) -> bytes | None:
            try:
                return base64.b64decode(result["data"])
            except (KeyError, ValueError, TypeError):
                return None

        # (1) 직접 TCP — 도달 가능성 없는 주소는 건너뛴다
        if address and direct_tcp_viable(address):
            try:
                resp = await self._client.post(
                    f"http://{address}/p2p/replica_fetch",
                    json=authed, timeout=DIRECT_HOLDER_TIMEOUT,
                )
                if resp.status_code == 200:
                    return _data_from(resp.json())
                return None  # 404/403 등은 재경로 무의미
            except (httpx.TimeoutException, httpx.NetworkError):
                pass  # 도달 불가 → 다음 경로
        # (2) 직접 UDP(홀펀칭)
        if self._udp_send is not None and device_id:
            try:
                status, result = await self._udp_send(
                    device_id, "replica_fetch", authed
                )
                if status == 200:
                    return _data_from(result)
                return None
            except Exception:  # noqa: BLE001 — 펀치/전송 실패 → 릴레이
                pass
        # (3) 릴레이(정책 허가 시)
        if not device_id:
            return None
        try:
            result = await self._relay_op(device_id, "replica_fetch", authed)
            return _data_from(result)
        except Exception as e:  # noqa: BLE001
            logger.info(
                "홀더 fetch 실패(direct+udp+relay) dev=%s addr=%s: %s",
                device_id, address, e,
            )
            return None
