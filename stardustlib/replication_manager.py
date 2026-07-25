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
from dataclasses import dataclass, field
from typing import Any

import httpx

from stardustlib import chunker
from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError
from stardustlib.remote_source import _EventLoopThread, direct_tcp_viable

logger = logging.getLogger(__name__)

DEFAULT_MIN_REPLICAS = 1  # 원본 외 추가 사본 수. 서버 /replication/policy로 재정의됨.
# 홀더 직접 연결 시도 타임아웃(초). 짧게 잡아 도달 불가 시 릴레이로 빨리 fallback.
DIRECT_HOLDER_TIMEOUT = 3.0


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
        jbod_manager: Any,
        *,
        chunk_size: int = chunker.DEFAULT_CHUNK_SIZE,
        min_replicas: int = DEFAULT_MIN_REPLICAS,
        timeout: float = 10.0,
        max_concurrent_repair: int = 4,
        io: _EventLoopThread | None = None,
    ) -> None:
        self._auth = auth_client
        self._server_url = server_url.rstrip("/")
        self._meta = metadata_store
        self._jbod = jbod_manager
        self._engine = getattr(jbod_manager, "encryption_engine", None)
        self._chunk_size = chunk_size
        self._min_replicas = min_replicas
        self._timeout = timeout
        self._max_concurrent_repair = max_concurrent_repair
        self._io = io or _EventLoopThread.get_instance()
        self._client = httpx.AsyncClient(timeout=timeout)
        # 직접 UDP(홀펀칭) 전송 콜백: async (device_id, op, payload) -> (status, result).
        # 데몬이 HolePunchService.send_op를 주입한다. 같은 _io 루프에서 await된다.
        self._udp_send = None

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
        """파일을 암호화·청킹해 ≥min_replicas 홀더에 복제한다.

        로컬 I/O(읽기·암호화·상태 기록)는 호출 스레드에서, 네트워크는 IO 루프에서
        수행한다. 파일이 없으면 FileNotFoundError, 암호화 미설정 시 ReplicationError.
        """
        if self._engine is None:
            raise ReplicationError("암호화 엔진이 없어 복제할 수 없습니다")
        meta = self._meta.lookup(virtual_path)
        if meta is None:
            raise FileNotFoundError(virtual_path)

        plaintext = self._jbod.read_file(virtual_path)
        blob = self._engine.encrypt(plaintext)
        file_ref = self._file_ref(virtual_path)
        chunks = chunker.split(blob, self._chunk_size)

        result = self._io.run_coroutine(
            self._replicate_chunks(file_ref, chunks)
        )
        self._meta.set_replication_status(virtual_path, result.status)
        if result.status != "replicated":
            logger.warning(
                "파일 복제 미완료(pending): %s — 청크별 복제수=%s (목표 %d)",
                virtual_path, result.replicas_per_chunk, self._min_replicas,
            )
        return result

    def recover(self, virtual_path: str) -> int:
        """복제본에서 파일을 복구해 로컬에 기록한다. 기록 바이트 수를 반환한다.

        도달 가능한 홀더가 없는 청크가 있으면 RecoveryError(누락 chunk_id 명시).
        """
        if self._engine is None:
            raise ReplicationError("암호화 엔진이 없어 복구할 수 없습니다")
        file_ref = self._file_ref(virtual_path)
        blob = self._io.run_coroutine(self._recover_chunks(file_ref))
        plaintext = self._engine.decrypt(blob)
        self._jbod.write_file(virtual_path, plaintext)
        return len(plaintext)

    def ensure_replicas(self, virtual_path: str) -> HealReport:
        """건강성을 점검해 부족한 청크의 복제본을 채운다(재복제).

        청크는 불변이므로 온라인 홀더에서 받아(재암호화 없이) 새 홀더로 복사한다.
        온라인 홀더가 없는 청크는 unrecoverable로 보고한다. 모든 청크가
        min_replicas를 충족하면 replicated, 아니면 pending으로 표시한다.
        """
        report = self._io.run_coroutine(
            self._ensure_chunks(self._file_ref(virtual_path))
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
        self, file_ref: str, chunks: list[tuple[int, bytes]]
    ) -> ReplicationResult:
        token = await self._token()
        # 소유자 자신의 device는 홀더에서 제외한다(자기 기기에 백업은 무의미·헤어핀 실패).
        self_dev = getattr(self._jbod, "device_id", None)
        exclude = [self_dev] if self_dev else []
        replicas_per_chunk: list[int] = []
        for idx, data in chunks:
            chunk_id = self._chunk_id(file_ref, idx)
            await self._register_chunk(token, chunk_id, file_ref, idx, len(data))
            holders = await self._placement(token, len(data), exclude=exclude)
            placed = 0
            for holder in holders:
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

        ok = bool(replicas_per_chunk) and all(
            n >= self._min_replicas for n in replicas_per_chunk
        )
        return ReplicationResult(
            status="replicated" if ok else "pending",
            chunk_count=len(chunks),
            min_replicas=self._min_replicas,
            replicas_per_chunk=replicas_per_chunk,
        )

    async def _recover_chunks(self, file_ref: str) -> bytes:
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
            data = await self._fetch_from_any_holder(token, chunk_id)
            if data is None:
                missing.append(chunk_id)
            else:
                parts.append((idx, data))

        if missing:
            raise RecoveryError(
                f"도달 가능한 홀더가 없는 청크 {len(missing)}개", missing
            )
        return chunker.join(parts)

    async def _ensure_chunks(self, file_ref: str) -> HealReport:
        token = await self._token()
        chunk_infos = await self._list_chunks(token, file_ref)
        if not chunk_infos:
            raise RecoveryError(
                f"재복제할 청크가 없습니다: file_ref={file_ref}", []
            )

        sem = asyncio.Semaphore(self._max_concurrent_repair)

        async def heal(info: dict) -> dict:
            async with sem:  # 동시 재복제 상한
                return await self._heal_chunk(token, info)

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

    async def _heal_chunk(self, token: str, info: dict) -> dict:
        """한 청크의 복제본을 min_replicas까지 채운다(불변 청크 복사)."""
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
        data = None
        for h in online:
            data = await self._holder_fetch(
                h.get("device_id"), h.get("connection_address"), chunk_id, token
            )
            if data is not None:
                break
        if data is None:
            # 도달 가능한 소스가 없어 복제 불가 — 데이터 자체는 다른 곳에 있을 수 있음.
            return {"chunk_id": chunk_id, "added": 0, "healthy": False}

        self_dev = getattr(self._jbod, "device_id", None)
        exclude = list(current_devices)
        if self_dev and self_dev not in exclude:
            exclude.append(self_dev)
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
        self, token: str, chunk_id: str
    ) -> bytes | None:
        """청크의 홀더 중 온라인·도달 가능한 곳에서 처음 성공한 데이터를 반환한다."""
        holders = await self._list_replicas(token, chunk_id)
        for holder in holders:
            if holder.get("is_online") is False:
                continue
            device_id = holder.get("device_id")
            address = holder.get("connection_address")
            if not device_id and not address:
                continue
            data = await self._holder_fetch(device_id, address, chunk_id, token)
            if data is not None:
                return data
        return None

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
        self, token: str, chunk_id: str, file_ref: str, idx: int, size: int
    ) -> None:
        await self._client.post(
            f"{self._server_url}/replication/chunks",
            json={"chunk_id": chunk_id, "file_ref": file_ref,
                  "idx": idx, "size": size},
            headers=self._auth_headers(token),
        )

    async def _placement(
        self, token: str, size: int, exclude: list[str]
    ) -> list[dict]:
        resp = await self._client.post(
            f"{self._server_url}/replication/placement",
            json={"size": size, "count": self._min_replicas, "exclude": exclude},
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
        """
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
                return resp.status_code == 200
            except (httpx.TimeoutException, httpx.NetworkError):
                pass  # 도달 불가 → 다음 경로
        # (2) 직접 UDP(홀펀칭)
        if self._udp_send is not None and device_id:
            try:
                status, _result = await self._udp_send(
                    device_id, "replica_store", authed
                )
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
