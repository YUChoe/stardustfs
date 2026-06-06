"""리플리케이션 백그라운드 스케줄러 (운영 활성화).

daemon이 주기적으로 (1) 미복제 로컬 파일을 자동 백업하고 (2) 복제본이 부족한 파일을
재복제(heal)한다. ReplicationManager의 동기 API는 전용 IO 루프로 자가 브리지되므로
asyncio.to_thread로 호출해 daemon 이벤트 루프를 막지 않는다. 한 파일의 실패는 그
파일에 국한되고 루프는 계속된다(실패 격리).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# 한 주기에 상한(max)만큼 처리했으면 백로그가 남은 것으로 보고 짧게 쉬고 계속 비운다.
_BACKLOG_DRAIN_DELAY = 2.0
# 복제 미완료(pending)가 남으면 전체 주기(backup_interval)를 기다리지 않고 짧게 재시도한다.
# 지속 실패(예: 도달 불가·전송 한도) 시 backup_interval까지 지수 백오프로 늘려 폭주를 막는다.
_RETRY_MIN_DELAY = 15.0


class ReplicationScheduler:
    """백업/heal 백그라운드 루프."""

    def __init__(
        self,
        manager,
        metadata_store,
        owner_device_id: str | None,
        *,
        backup_interval: float = 300.0,
        heal_interval: float = 3600.0,
        heal_grace_seconds: float = 86400.0,
        max_files_per_cycle: int = 20,
        backup_concurrency: int = 4,
        policy_fetcher=None,
        on_policy=None,
        policy_interval: float = 3600.0,
    ) -> None:
        self._manager = manager
        self._metadata = metadata_store
        self._owner_device_id = owner_device_id
        self._backup_interval = backup_interval
        self._heal_interval = heal_interval
        self._heal_grace = heal_grace_seconds
        self._max = max_files_per_cycle
        self._backup_concurrency = max(1, backup_concurrency)
        # 정책 주기 갱신: policy_fetcher()(async)→dict|None, on_policy(dict) 적용 콜백.
        self._policy_fetcher = policy_fetcher
        self._on_policy = on_policy
        self._policy_interval = policy_interval
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # vpath → 처음 degraded로 관측된 시각(monotonic). 유예 경과 시 재복제.
        self._degraded_since: dict[str, float] = {}
        # 로컬에 물리 파일이 없어 백업 불가한 vpath(다른 device 소유의 NULL 레코드 등).
        # 매 주기 재시도/경고 스팸을 막기 위해 캐시해 건너뛴다.
        self._skip_backup: set[str] = set()
        # 직전 주기에서 복제 미완료(pending)로 남은 파일 수. 짧은 재시도 판단에 쓴다.
        self._last_pending: int = 0
        # 현재 재시도 백오프(초). pending이 남는 동안 _RETRY_MIN_DELAY부터 2배씩 증가.
        self._retry_delay: float | None = None

    async def start(self) -> None:
        """백그라운드 루프(백업 + heal)를 기동한다."""
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._backup_loop()),
            asyncio.create_task(self._heal_loop()),
        ]
        if self._policy_fetcher is not None:
            self._tasks.append(asyncio.create_task(self._policy_loop()))
        logger.info(
            "리플리케이션 스케줄러 시작 (backup=%.0fs, heal=%.0fs, max=%d/주기)",
            self._backup_interval, self._heal_interval, self._max,
        )

    async def _policy_loop(self) -> None:
        """주기적으로 서버 정책을 내려받아 적용한다(시작 시 즉시 1회)."""
        while not self._stop.is_set():
            try:
                policy = await self._policy_fetcher()
                if policy and self._on_policy is not None:
                    self._on_policy(policy)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 루프 유지
                logger.warning("정책 갱신 실패: %s", e)
            await self._sleep(self._policy_interval)

    async def stop(self) -> None:
        """루프를 중지하고 매니저를 정리한다."""
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        try:
            self._manager.close()
        except Exception as e:  # noqa: BLE001 — 종료 경로
            logger.debug("매니저 close 중 예외: %s", e)

    async def _sleep(self, seconds: float) -> None:
        """정지 신호가 오면 즉시 깨어나는 대기."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _backup_loop(self) -> None:
        while not self._stop.is_set():
            processed = 0
            try:
                processed = await self.run_backup_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 루프 유지
                logger.error("백업 주기 오류: %s", e, exc_info=True)
            delay = self._next_delay(processed)
            await self._sleep(delay)

    def _next_delay(self, processed: int) -> float:
        """다음 백업 주기까지 대기 시간을 정한다.

        - 상한만큼 처리(백로그) → 짧게 쉬고 계속 비운다.
        - 복제 미완료(pending)가 남음 → 전체 주기를 기다리지 않고 짧은 백오프로 재시도
          (_RETRY_MIN_DELAY부터 2배씩, backup_interval 상한). 지속 실패 시 폭주 방지.
        - 모두 완료 → 백오프 초기화 후 정상 주기 간격.
        """
        if processed >= self._max:
            return _BACKLOG_DRAIN_DELAY
        if self._last_pending > 0:
            nxt = (
                self._retry_delay * 2 if self._retry_delay
                else _RETRY_MIN_DELAY
            )
            self._retry_delay = min(nxt, self._backup_interval)
            return self._retry_delay
        self._retry_delay = None
        return self._backup_interval

    async def run_backup_cycle(self) -> int:
        """미복제/미완료(none|pending) 로컬 파일 ≤max개를 복제한다.

        pending(목표 미달)도 매 주기 재시도해 홀더가 확보되면 곧 replicated가 된다
        (heal의 24h 유예와 달리 즉시 재시도). 처리한 파일 수를 반환한다.
        """
        paths = self._metadata.list_virtual_paths_for_replication(
            ("none", "pending"), self._owner_device_id
        )
        targets = [
            vp for vp in paths[: self._max] if vp not in self._skip_backup
        ]
        if not targets:
            return 0

        # 제한된 동시성으로 병렬 백업한다(한 파일이 직접 연결 타임아웃을 기다리는
        # 동안 다른 파일이 진행). MetadataStore는 스레드별 연결 + WAL이라 안전하다.
        sem = asyncio.Semaphore(self._backup_concurrency)
        done = [0]
        pending = [0]  # 복제 미완료(replicated 아님 또는 오류) → 짧은 재시도 대상

        async def _one(vpath: str) -> None:
            if self._stop.is_set():
                return
            async with sem:
                try:
                    result = await asyncio.to_thread(
                        self._manager.replicate, vpath
                    )
                    done[0] += 1
                    if result.status != "replicated":
                        pending[0] += 1  # 목표 미달 → 곧 재시도
                    logger.info("자동 백업: %s → %s", vpath, result.status)
                except FileNotFoundError:
                    # 이 device에 물리 파일이 없음(다른 device 소유 NULL 레코드 등).
                    # 그 device가 백업하므로 조용히 건너뛰고 캐시한다.
                    self._skip_backup.add(vpath)
                    logger.debug("로컬에 없어 백업 건너뜀: %s", vpath)
                except Exception as e:  # noqa: BLE001 — 파일 단위 실패 격리
                    pending[0] += 1  # 일시 오류 → 짧은 주기로 재시도
                    logger.warning("자동 백업 실패(건너뜀): %s: %s", vpath, e)

        await asyncio.gather(*[_one(vp) for vp in targets])
        self._last_pending = pending[0]
        return done[0]

    async def _heal_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self._heal_interval)
            if self._stop.is_set():
                break
            try:
                await self.run_heal_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 루프 유지
                logger.error("재복제 주기 오류: %s", e, exc_info=True)

    async def run_heal_cycle(self) -> int:
        """복제됨/pending 파일의 건강성을 점검해 유예 경과분만 보충한다.

        degraded(청크 online < min_replicas)가 heal_grace_seconds 이상 지속된 파일만
        ensure_replicas로 재복제한다(일시적 오프라인의 churn 방지). 건강해진 파일은
        관측 기록을 지운다. 실제 재복제한 파일 수를 반환한다.
        """
        # replicated(건강했다가 줄어든) 파일만 유예 후 보충. pending은 backup 루프가
        # 즉시 재시도하므로 제외한다.
        paths = self._metadata.list_virtual_paths_for_replication(
            ("replicated",), self._owner_device_id
        )
        now = asyncio.get_running_loop().time()
        repaired = 0
        for vpath in paths[: self._max]:
            if self._stop.is_set():
                break
            try:
                summary = await asyncio.to_thread(
                    self._manager.replication_health, vpath
                )
            except Exception as e:  # noqa: BLE001 — 실패 격리
                logger.warning("건강성 점검 실패(건너뜀): %s: %s", vpath, e)
                continue

            if not summary.degraded:
                self._degraded_since.pop(vpath, None)
                continue

            first = self._degraded_since.setdefault(vpath, now)
            if (now - first) < self._heal_grace:
                logger.info(
                    "degraded 관측(유예 대기): %s (online=%d)",
                    vpath, summary.min_online,
                )
                continue
            try:
                report = await asyncio.to_thread(
                    self._manager.ensure_replicas, vpath
                )
                repaired += 1
                self._degraded_since.pop(vpath, None)
                logger.info("자동 재복제: %s → %s", vpath, report.status)
            except Exception as e:  # noqa: BLE001 — 실패 격리
                logger.warning("자동 재복제 실패(건너뜀): %s: %s", vpath, e)
        return repaired
