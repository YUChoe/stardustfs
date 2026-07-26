"""메타데이터/키 동기화 클라이언트.

중앙 서버와 metadata_db 및 key_file을 동기화한다.
오프라인 우선(offline-first) 설계로, 서버 연결 불가 시에도
로컬 기능은 정상 동작한다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.exceptions import KeyNotFoundError, SyncError
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import FileMetadata

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10.0
_MAX_KEY_RETRIES = 3
_MAX_UPLOAD_FAILURES = 3
_MAX_CAS_RETRIES = 5  # 낙관적 잠금(CAS) 충돌 시 재병합·재시도 횟수
_DEFAULT_RETENTION_DAYS = 30  # status에서 받지 못한 경우의 tombstone 보관기간 기본값
# 버전 롱폴링: 서버 대기 한계(25s)보다 길게 잡아 서버가 먼저 응답하도록 한다
_WAIT_HTTP_TIMEOUT = 35.0
_WAIT_RETRY_DELAY = 5.0  # 롱폴 네트워크 오류 시 재시도 간격
_WAIT_IDLE_DELAY = 1.0  # 롱폴이 즉시 changed=false 반환 시 busy loop 방지용 최소 간격


class _WaitUnsupported(Exception):
    """서버가 버전 롱폴링 엔드포인트를 지원하지 않음(404)."""


class SyncClient:
    """메타데이터/키 동기화 클라이언트."""

    def __init__(
        self,
        auth_client: AuthClient,
        server_url: str,
        metadata_store: MetadataStore,
        conflict_resolver: ConflictResolver,
        interval_seconds: int = 30,
        encryption_key: bytes | None = None,
        storage_pool=None,
    ) -> None:
        self._auth_client = auth_client
        self._server_url = server_url.rstrip("/")
        self._metadata_store = metadata_store
        self._conflict_resolver = conflict_resolver
        self._interval_seconds = interval_seconds
        self._encryption_key = encryption_key
        self._storage_pool = storage_pool  # tombstone 전파 시 물리 파일 삭제용 (선택)
        self._sync_task: asyncio.Task[None] | None = None
        self._running = False
        # 버전 롱폴링(즉시 동기화) 태스크. 서버 미지원(404) 시 비활성화.
        self._wait_task: asyncio.Task[None] | None = None
        self._wait_enabled = True
        self._consecutive_failures = 0
        self._last_synced_version = 0
        # 파셜(레코드) 동기화 모드. 서버가 레코드 엔드포인트를 지원하지 않으면(404)
        # False로 낮추고 기존 전체 blob 경로를 사용한다(하위 호환).
        self._record_mode = True
        # record_id 파생용 subkey(지연 파생)
        self._record_subkey: bytes | None = None
        # tombstone 보관기간(초). status 응답에서 갱신됨. 기본 30일.
        self._retention_seconds: float = _DEFAULT_RETENTION_DAYS * 86400
        # last_sync_at 보존 파일 (메타데이터 DB 외부)
        self._syncstate_path = f"{metadata_store._db_path}.syncstate.json"
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)

    async def initial_sync(self) -> None:
        """시작 시 서버에서 metadata_db 다운로드 및 병합."""
        # 레코드 모드: 증분(since=0) 초기 다운로드 우선. 404면 전체 blob 경로로 폴백.
        if self._record_mode:
            try:
                if await self._download_records():
                    logger.info("Initial sync completed (records).")
                    self._run_orphan_gc(startup=True)
                    return
                self._record_mode = False
                logger.info("레코드 미지원(404) — 전체 blob 초기 동기화로 폴백")
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(
                    "Initial sync failed (network): %s. Using local DB.", e
                )
                return
            except Exception as e:
                logger.warning("Initial sync (records) failed: %s.", e)
                return
        try:
            token = await self._auth_client.get_valid_token()
            response = await self._client.get(
                f"{self._server_url}/sync/metadata",
                headers={"Authorization": f"Bearer {token}"},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(
                "Initial sync failed (network): %s. Using local DB.", e
            )
            return
        except Exception as e:
            logger.warning(
                "Initial sync failed: %s. Using local DB.", e
            )
            return

        if response.status_code == 404:
            # 서버에 metadata가 아직 없음 — 로컬 데이터를 업로드
            logger.info("No server metadata found. Uploading local metadata.")
            await self._force_upload()
            return

        if response.status_code >= 400:
            logger.warning(
                "Initial sync failed (HTTP %d). Using local DB.",
                response.status_code,
            )
            return

        server_db_blob = response.content
        await self._merge_server_metadata(server_db_blob)
        logger.info("Initial sync completed.")

        # 시작 시 1회 orphan GC: 이전 세션에서 소유권을 잃은 물리 파일을 정리한다.
        self._run_orphan_gc(startup=True)

    async def start_periodic_sync(self) -> None:
        """주기적 동기화 루프 시작."""
        if self._running:
            return
        self._running = True
        self._sync_task = asyncio.create_task(self._periodic_loop())
        # 버전 롱폴링 루프(즉시 동기화) 시작 — 주기 폴링은 안전망으로 유지
        self._wait_task = asyncio.create_task(self._version_wait_loop())
        logger.info(
            "Periodic sync started (interval=%ds).", self._interval_seconds
        )

    async def upload_metadata(self) -> None:
        """로컬 metadata_db 스냅샷을 서버에 업로드 (낙관적 잠금/CAS).

        pending 변경사항(생성/수정/삭제 tombstone)이 없으면 업로드를 건너뛴다.

        서버에 X-Base-Version 헤더로 마지막으로 동기화한 서버 version을 보낸다.
        서버 version이 그 사이 다른 디바이스에 의해 올라갔으면 409가 반환되며,
        이 경우 서버 변경을 다운로드·재병합한 뒤 재시도한다(최대 _MAX_CAS_RETRIES회).
        이로써 동시 업로드 시 한쪽 변경이 유실되는 레이스컨디션을 방지한다.
        """
        # 레코드 모드: 증분 업로드 우선. 404면 전체 blob 경로로 폴백.
        if self._record_mode:
            try:
                if await self._upload_records():
                    return
                self._record_mode = False
                logger.info("레코드 미지원(404) — 전체 blob 업로드로 폴백")
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_UPLOAD_FAILURES:
                    logger.error("레코드 업로드 실패 %d회: %s",
                                 self._consecutive_failures, e)
                return
            except Exception as e:
                logger.warning("레코드 업로드 오류: %s", e)
                return

        db_path = self._metadata_store._db_path

        # pending 변경사항이 없으면 업로드 불필요 (삭제는 tombstone으로 pending에 포함됨)
        if not self._metadata_store.get_pending_files():
            return

        if not os.path.exists(db_path):
            logger.warning("Metadata DB not found: %s", db_path)
            return

        # 업로드 전 만료된 tombstone 정리 (서버에 정리된 상태가 반영되도록)
        self._gc_tombstones()

        for attempt in range(1, _MAX_CAS_RETRIES + 1):
            pending_files = self._metadata_store.get_pending_files()
            if not pending_files:
                return

            logger.info(
                "Sync: pending %d개 파일 업로드 시작 (base_version=%d, 시도 %d)",
                len(pending_files), self._last_synced_version, attempt,
            )

            try:
                token = await self._auth_client.get_valid_token()

                # WAL 모드에서 최신 데이터를 포함하려면 checkpoint 수행
                conn = self._metadata_store._get_conn()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                with open(db_path, "rb") as f:
                    db_blob = f.read()

                # 서버 전송 전 AES-256-GCM 암호화
                encrypted_blob = self._encrypt_blob(db_blob)

                response = await self._client.put(
                    f"{self._server_url}/sync/metadata",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Base-Version": str(self._last_synced_version),
                    },
                    content=encrypted_blob,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_UPLOAD_FAILURES:
                    logger.error(
                        "Metadata upload failed %d consecutive times: %s",
                        self._consecutive_failures, e,
                    )
                return
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_UPLOAD_FAILURES:
                    logger.error(
                        "Metadata upload failed %d consecutive times: %s",
                        self._consecutive_failures, e,
                    )
                return

            if response.status_code == 409:
                # CAS 충돌: 다른 디바이스가 먼저 업로드함 → 다운로드·재병합 후 재시도
                logger.info(
                    "CAS 충돌 (base_version=%d) — 서버 변경 병합 후 재시도",
                    self._last_synced_version,
                )
                await self._download_and_merge()
                continue

            if response.status_code >= 400:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_UPLOAD_FAILURES:
                    logger.error(
                        "Metadata upload failed %d consecutive times (HTTP %d).",
                        self._consecutive_failures, response.status_code,
                    )
                return

            # 업로드 성공
            self._consecutive_failures = 0
            try:
                resp_data = response.json()
                if "version" in resp_data:
                    self._last_synced_version = resp_data["version"]
            except Exception:
                pass
            # pending → synced 갱신
            for fm in pending_files:
                self._metadata_store.set_sync_status(fm.virtual_path, "synced")
            self._record_sync_success()
            logger.info("Metadata uploaded successfully.")
            return

        logger.warning(
            "Metadata upload: CAS 재시도 %d회 초과, 다음 주기에 재시도",
            _MAX_CAS_RETRIES,
        )

    async def upload_key(self, encrypted_blob: bytes) -> None:
        """암호화된 key blob을 서버에 업로드 (10초 타임아웃, 3회 재시도)."""
        last_error: Exception | None = None
        for attempt in range(1, _MAX_KEY_RETRIES + 1):
            try:
                token = await self._auth_client.get_valid_token()
                response = await self._client.put(
                    f"{self._server_url}/sync/key",
                    headers={"Authorization": f"Bearer {token}"},
                    content=encrypted_blob,
                )
                if response.status_code < 400:
                    logger.info("Key uploaded successfully.")
                    return
                last_error = SyncError(
                    f"Key upload failed (HTTP {response.status_code})"
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                logger.warning(
                    "Key upload attempt %d/%d failed: %s",
                    attempt, _MAX_KEY_RETRIES, e,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "Key upload attempt %d/%d failed: %s",
                    attempt, _MAX_KEY_RETRIES, e,
                )

        raise SyncError(
            f"Key upload failed after {_MAX_KEY_RETRIES} retries: {last_error}"
        )

    async def download_key(self) -> bytes:
        """서버에서 암호화된 key blob 다운로드 (10초 타임아웃, 3회 재시도)."""
        last_error: Exception | None = None
        for attempt in range(1, _MAX_KEY_RETRIES + 1):
            try:
                token = await self._auth_client.get_valid_token()
                response = await self._client.get(
                    f"{self._server_url}/sync/key",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 404:
                    raise KeyNotFoundError(
                        "No key backup exists on server."
                    )
                if response.status_code < 400:
                    logger.info("Key downloaded successfully.")
                    return response.content
                last_error = SyncError(
                    f"Key download failed (HTTP {response.status_code})"
                )
            except KeyNotFoundError:
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                logger.warning(
                    "Key download attempt %d/%d failed: %s",
                    attempt, _MAX_KEY_RETRIES, e,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "Key download attempt %d/%d failed: %s",
                    attempt, _MAX_KEY_RETRIES, e,
                )

        raise SyncError(
            f"Key download failed after {_MAX_KEY_RETRIES} retries: "
            f"{last_error}"
        )

    def mark_dirty(self) -> None:
        """deprecated: tombstone 도입으로 삭제도 pending으로 추적되어 더 이상 불필요.

        하위 호환을 위해 no-op으로 유지한다.
        """
        return

    # --- tombstone GC / stale 재조정 ---

    def _gc_tombstones(self) -> None:
        """보관기간이 지난 tombstone을 로컬 메타데이터에서 제거한다.

        서버는 암호화 blob만 보관하므로 GC는 클라이언트에서만 수행된다.
        활성 레코드는 영향받지 않는다.
        """
        try:
            removed = self._metadata_store.purge_expired_tombstones(
                self._retention_seconds
            )
            if removed > 0:
                logger.info("Tombstone GC: %d개 만료 삭제 레코드 정리", removed)
        except Exception as e:
            logger.warning("Tombstone GC 실패: %s", e)

    def _read_last_sync_at(self) -> float | None:
        """syncstate 파일에서 마지막 성공 동기화 시각을 읽는다. 없으면 None."""
        try:
            with open(self._syncstate_path, encoding="utf-8") as f:
                data = json.load(f)
            value = data.get("last_sync_at")
            return float(value) if value is not None else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _record_sync_success(self) -> None:
        """동기화 성공 시각을 syncstate 파일에 기록한다."""
        try:
            tmp = f"{self._syncstate_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_sync_at": time.time()}, f)
            os.replace(tmp, self._syncstate_path)
        except OSError as e:
            logger.warning("last_sync_at 기록 실패: %s", e)

    def _is_stale(self) -> bool:
        """마지막 동기화 후 보관기간을 초과했으면 stale(장기 오프라인)로 판정한다.

        syncstate 파일이 없으면(최초 구동/새 디바이스) stale이 아니다.
        """
        last_sync_at = self._read_last_sync_at()
        if last_sync_at is None:
            return False
        return (time.time() - last_sync_at) > self._retention_seconds

    async def _fetch_retention_days(self) -> None:
        """서버 status에서 tombstone_retention_days를 받아 보관기간을 갱신한다."""
        try:
            token = await self._auth_client.get_valid_token()
            resp = await self._client.get(
                f"{self._server_url}/sync/metadata/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                days = resp.json().get("tombstone_retention_days")
                if isinstance(days, int) and days > 0:
                    self._retention_seconds = days * 86400
        except Exception as e:
            logger.debug("retention_days 조회 실패, 기본값 유지: %s", e)

    async def reconcile_if_stale(self) -> bool:
        """장기 오프라인 디바이스면 서버 정본으로 재조정(re-baseline)한다.

        오프라인 중에는 파일 변경이 불가능하므로, 정상적으로는 pending이 없어
        서버 정본을 전면 채택(initial_sync)하면 된다. 단, 종료 직전 사이클의
        동기화 실패로 pending이 남은 경우에만 해당 레코드를 conflict copy로
        격리 보존한 뒤 재수신한다.

        Returns:
            재조정을 수행했으면 True, stale이 아니면 False.
        """
        await self._fetch_retention_days()
        if not self._is_stale():
            return False

        logger.warning(
            "장기 오프라인 디바이스 감지(stale) — 서버 정본으로 재조정 수행"
        )

        # 종료 직전 동기화 실패로 남은 pending 변경을 격리 보존
        pending = self._metadata_store.get_pending_files()
        isolated = 0
        for fm in pending:
            if fm.deleted:
                continue  # 삭제 tombstone은 격리 대상 아님
            try:
                conflict_path = self._conflict_resolver.generate_conflict_name(
                    fm.virtual_path
                )
                self._metadata_store.rename_path(fm.virtual_path, conflict_path)
                self._metadata_store.set_sync_status(conflict_path, "pending")
                isolated += 1
                logger.info(
                    "stale 재조정: 미동기 변경 격리 %s → %s",
                    fm.virtual_path, conflict_path,
                )
            except Exception as e:
                logger.warning("미동기 변경 격리 실패 %s: %s", fm.virtual_path, e)

        # 서버 정본 전면 채택. 격리한 레코드는 pending으로 남아 다음 업로드에 포함된다.
        self._last_synced_version = 0
        await self.initial_sync()
        if isolated:
            await self.upload_metadata()
        logger.info("stale 재조정 완료 (격리 %d건)", isolated)
        return True

    async def stop(self) -> None:
        """동기화 루프 중지."""
        self._running = False
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
        if self._wait_task is not None:
            self._wait_task.cancel()
            try:
                await self._wait_task
            except asyncio.CancelledError:
                pass
            self._wait_task = None
        await self._client.aclose()
        logger.info("Sync client stopped.")

    # --- 내부 메서드 ---

    async def _version_wait_loop(self) -> None:
        """서버 version 변경을 롱폴링으로 대기해 즉시 동기화한다.

        주기 폴링(_periodic_loop)과 병행하며, 변경 통지를 받으면 폴링 주기를
        기다리지 않고 즉시 다운로드·병합한다. 서버가 wait 엔드포인트를 지원하지
        않으면(404) 비활성화하고 주기 폴링만 사용한다(하위 호환).
        """
        # 롱폴은 서버 대기(25s)를 견뎌야 하므로 별도의 긴 타임아웃 클라이언트 사용
        async with httpx.AsyncClient(timeout=_WAIT_HTTP_TIMEOUT) as client:
            while self._running and self._wait_enabled:
                try:
                    changed = await self._wait_for_version(client)
                except _WaitUnsupported:
                    logger.info(
                        "서버가 버전 롱폴링 미지원(404) — 주기 폴링만 사용"
                    )
                    self._wait_enabled = False
                    return
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    logger.debug("버전 롱폴 재시도: %s", e)
                    await asyncio.sleep(_WAIT_RETRY_DELAY)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("버전 롱폴 오류: %s", e)
                    await asyncio.sleep(_WAIT_RETRY_DELAY)
                    continue

                if changed:
                    logger.info("버전 변경 통지 수신 — 즉시 동기화")
                    await self._download_and_merge()
                    self._run_orphan_gc()
                else:
                    # 변경 없음(정상 롱폴 타임아웃). 서버가 롱폴을 즉시 반환하는
                    # 경우(프록시/구현 차이)에도 busy loop가 되지 않도록 최소 간격
                    # 대기 후 재시도한다.
                    await asyncio.sleep(_WAIT_IDLE_DELAY)

    async def _wait_for_version(self, client: httpx.AsyncClient) -> bool:
        """GET /sync/metadata/wait로 version 변경을 대기한다.

        Returns:
            서버 version이 known_version보다 커졌으면 True(변경), 타임아웃이면 False.

        Raises:
            _WaitUnsupported: 서버가 404(미지원)를 반환한 경우.
            httpx.TimeoutException/ConnectError: 네트워크 오류.
        """
        token = await self._auth_client.get_valid_token()
        response = await client.get(
            f"{self._server_url}/sync/metadata/wait",
            params={"known_version": self._last_synced_version},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 404:
            raise _WaitUnsupported()
        if response.status_code >= 400:
            logger.warning(
                "버전 롱폴 실패: HTTP %d", response.status_code
            )
            return False
        data = response.json()
        return bool(data.get("changed", False))

    def _run_orphan_gc(self, startup: bool = False) -> None:
        """orphan GC를 안전하게 실행한다 (동기화 자체를 막지 않음).

        startup=True이면 플래그와 무관하게 1회 전체 스캔(이전 세션 정리),
        그 외에는 이번 사이클에 소유권 이전/감지가 있었을 때만 1회 스캔한다.
        """
        storage_pool = self._storage_pool
        if storage_pool is None:
            return
        try:
            if startup:
                if hasattr(storage_pool, "gc_orphan_files"):
                    storage_pool.gc_orphan_files()
            else:
                if hasattr(storage_pool, "gc_orphan_files_if_needed"):
                    storage_pool.gc_orphan_files_if_needed()
        except Exception as e:
            logger.warning("orphan GC 실패(무시하고 계속): %s", e)

    async def _periodic_loop(self) -> None:
        """interval_seconds마다 upload_metadata()를 호출하는 루프."""
        logger.info("_periodic_loop started, interval=%ds", self._interval_seconds)
        # 최초 기동 시 즉시 동기화
        try:
            logger.info("Periodic sync cycle: initial sync on start")
            await self._download_and_merge()
            await self.upload_metadata()
            self._run_orphan_gc()
        except Exception as e:
            logger.error("Periodic sync initial error: %s", e)

        while self._running:
            try:
                await asyncio.sleep(self._interval_seconds)
                if not self._running:
                    break
                logger.info("Periodic sync cycle: starting download+upload")
                # 양방향 동기화: 다운로드(병합) → 업로드
                await self._download_and_merge()
                await self.upload_metadata()
                # 사이클 종료 후 필요 시에만 orphan GC (사이클당 1회)
                self._run_orphan_gc()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Periodic sync error: %s", e)

    async def _merge_server_metadata(self, server_db_blob: bytes) -> None:
        """서버에서 받은 암호화된 DB blob을 복호화 후 임시 파일로 저장하고 레코드 단위 병합."""
        # 서버에서 받은 blob 복호화
        decrypted_blob = self._decrypt_blob(server_db_blob)

        # 임시 파일에 서버 DB 저장
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        try:
            os.write(tmp_fd, decrypted_blob)
            os.close(tmp_fd)

            # 서버 DB를 MetadataStore로 열기 (암호화 키 동일)
            server_store = MetadataStore(
                tmp_path, self._metadata_store._encryption_key
            )
            try:
                server_store.initialize()
            except Exception as e:
                server_store.close()
                from stardustlib.exceptions import KeyMismatchError
                raise KeyMismatchError(
                    f"서버 metadata 복호화 실패: {e}. "
                    f"key_file이 서버에 업로드한 PC와 동일한지 확인하세요. "
                    f"새 디바이스라면 먼저 key 복원(GET /sync/key)을 수행하세요."
                ) from e

            # 서버 DB의 모든 파일 레코드 조회 (tombstone 포함)
            server_conn = server_store._get_conn()
            cursor = server_conn.execute(
                "SELECT virtual_path, source_id, physical_path, file_size, "
                "created_at, modified_at, version, device_id, sync_status, deleted, "
                "COALESCE(replication_status, 'none') AS replication_status "
                "FROM files"
            )
            server_records: list[FileMetadata] = []
            for row in cursor.fetchall():
                server_records.append(FileMetadata(
                    virtual_path=row["virtual_path"],
                    source_id=row["source_id"],
                    physical_path=row["physical_path"],
                    file_size=row["file_size"],
                    created_at=row["created_at"],
                    modified_at=row["modified_at"],
                    version=row["version"],
                    device_id=row["device_id"],
                    sync_status=row["sync_status"],
                    deleted=bool(row["deleted"]),
                    replication_status=row["replication_status"],
                ))

            # 청크 매니페스트도 함께 가져온다. 서버 blob은 SQLite 파일 전체이므로
            # file_chunks 행이 그 안에 들어 있다. 이걸 읽지 않으면 수신 측에 파일
            # 레코드만 생기고 매니페스트가 없어, 읽기가 레거시 단일 blob 경로로 빠져
            # 첫 청크만 복호화해 조용히 잘린 내용을 돌려준다(에러도 나지 않는다).
            server_chunks = self._read_server_chunks(server_conn)

            # 각 서버 레코드에 대해 로컬과 비교 병합
            logger.info(
                "Merge: 서버 DB에서 %d개 레코드 조회됨 (청크 매니페스트 %s)",
                len(server_records),
                "포함" if server_chunks is not None else "없음(구버전)",
            )
            for server_rec in server_records:
                chunks = (
                    server_chunks.get(server_rec.virtual_path, [])
                    if server_chunks is not None else None
                )
                self._merge_record(server_rec, chunks)

            server_store.close()
        finally:
            # 임시 파일 정리
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _read_server_chunks(server_conn) -> dict | None:
        """서버 DB(전체 blob)에서 가상 경로별 청크 매니페스트를 읽는다.

        Returns:
            {virtual_path: [ChunkRef...]}. 서버 DB에 file_chunks 테이블이 없으면
            (청크 네이티브 이전 클라이언트가 올린 blob) None — 이 경우 로컬 매니페스트를
            건드리지 않는다. 테이블은 있고 그 파일에 행이 없으면 빈 목록이 되어 로컬
            매니페스트를 비운다(서버 표현이 레거시 blob이라는 뜻).
        """
        from stardustlib.models import ChunkRef

        exists = server_conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='file_chunks'"
        ).fetchone()
        if exists is None:
            return None

        result: dict = {}
        rows = server_conn.execute(
            "SELECT virtual_path, chunk_index, chunk_ref, source_id, "
            "device_id, size, hash FROM file_chunks "
            "ORDER BY virtual_path, chunk_index"
        ).fetchall()
        for row in rows:
            result.setdefault(row["virtual_path"], []).append(
                ChunkRef(
                    index=row["chunk_index"],
                    chunk_ref=row["chunk_ref"],
                    source_id=row["source_id"],
                    device_id=row["device_id"],
                    size=row["size"],
                    hash=row["hash"],
                )
            )
        return result

    def _merge_record(
        self, server_rec: FileMetadata, chunks: list | None = None
    ) -> None:
        """단일 레코드의 version 비교 기반 병합 로직 (tombstone 포함).

        서버 레코드를 채택할 때는 청크 매니페스트도 통째로 채택한다. chunks가 None
        이면(전체 blob 폴백 경로) 로컬 매니페스트를 그대로 둔다.

        병합 규칙:
        1. server_version > local_base_version AND local_version > local_base_version
           → ConflictResolver.resolve_conflict()
        2. server_version > local_version (충돌 아님)
           → 서버 메타데이터로 로컬 갱신 (서버가 tombstone이면 로컬도 삭제 전파)
        3. local_version > server_version (충돌 아님)
           → 다음 업로드 시 반영 (아무 작업 안 함)
        4. version 동일 → 변경 없음
        """
        # tombstone도 비교 대상에 포함하기 위해 lookup_any 사용
        local_rec = self._metadata_store.lookup_any(server_rec.virtual_path)

        if local_rec is None:
            if server_rec.deleted:
                # 로컬에 없는 삭제 레코드 — 전파할 대상 없음
                return
            # 로컬에 없는 파일 — 서버에서 새로 추가된 파일
            self._insert_from_server(server_rec, chunks)
            logger.info(
                "Merge: inserted from server: %s (version=%d)",
                server_rec.virtual_path, server_rec.version,
            )
            return

        server_version = server_rec.version
        local_version = local_rec.version

        # base_version: 마지막 동기화 시점의 version
        # 단순화: sync_status가 "synced"이면 local_version이 base_version
        # sync_status가 "pending"이면 base_version = local_version - 1
        # (로컬에서 수정이 있었으므로)
        if local_rec.sync_status == "pending":
            local_base_version = local_version - 1
        else:
            local_base_version = local_version

        # 충돌 판정
        if (server_version > local_base_version
                and local_version > local_base_version):
            # 양쪽 모두 수정됨 → 충돌
            logger.info(
                "Merge: CONFLICT %s (server_v=%d, local_v=%d, base_v=%d)",
                server_rec.virtual_path, server_version, local_version, local_base_version,
            )
            self._handle_conflict(server_rec, local_rec, chunks)
        elif server_version > local_version:
            # 서버가 더 최신 → 서버 메타데이터로 갱신 (tombstone 전파 포함)
            if server_rec.deleted:
                logger.info(
                    "Merge: deleted from server: %s (server_v=%d > local_v=%d)",
                    server_rec.virtual_path, server_version, local_version,
                )
            else:
                logger.info(
                    "Merge: updated from server: %s (server_v=%d > local_v=%d)",
                    server_rec.virtual_path, server_version, local_version,
                )
            # 소유권 이전 감지: 내가 소유하던 레코드가 다른 디바이스 소유로 바뀌면
            # 내 로컬 물리 파일이 orphan이 된다 → 다음 사이클에 GC 필요.
            self._detect_ownership_loss(local_rec, server_rec)
            self._update_from_server(server_rec, chunks)
        elif local_version > server_version:
            # 로컬이 더 최신 → 다음 업로드 시 반영 (아무 작업 안 함)
            pass
        else:
            # version 동일 → 변경 없음
            pass

    def _detect_ownership_loss(
        self, local_rec: FileMetadata, server_rec: FileMetadata
    ) -> None:
        """소유권 이전으로 로컬 물리 파일이 orphan이 되는지 감지한다.

        로컬 레코드가 이 디바이스 소유였는데(local device_id == 내 device_id),
        서버 레코드에서 다른 디바이스 소유로 바뀌었다면, 내 로컬 물리 파일은 더
        이상 metadata가 가리키지 않는 orphan이 된다. StoragePool에 GC 필요
        플래그를 세운다(다음 사이클 1회 스캔).
        """
        storage_pool = self._storage_pool
        if storage_pool is None:
            return
        my_device = getattr(storage_pool, "device_id", None)
        if my_device is None:
            return
        if server_rec.deleted:
            return  # 삭제는 기존 tombstone 물리 삭제 경로가 처리
        if local_rec.device_id == my_device and server_rec.device_id != my_device:
            storage_pool.mark_gc_needed()

    def _handle_conflict(
        self, server_rec: FileMetadata, local_rec: FileMetadata,
        chunks: list | None = None,
    ) -> None:
        """충돌 처리: conflict copy 생성 후 서버 메타데이터를 원본에 적용."""
        try:
            conflict_path = self._conflict_resolver.resolve_conflict(
                server_rec.virtual_path, server_rec.version
            )
            # 서버 메타데이터를 원본 경로에 삽입(청크 매니페스트도 함께 채택)
            self._insert_from_server(server_rec, chunks)
            logger.info(
                "Conflict resolved: %s → conflict copy: %s",
                server_rec.virtual_path, conflict_path,
            )
        except Exception as e:
            logger.error(
                "Conflict resolution failed for %s: %s",
                server_rec.virtual_path, e,
            )

    def _adopt_chunks(
        self, virtual_path: str, chunks: list | None, deleted: bool = False
    ) -> None:
        """서버 레코드의 청크 매니페스트를 통째로 채택한다.

        chunks가 None이면(전체 blob 폴백 경로) 로컬 매니페스트를 건드리지 않는다.
        빈 목록이면 서버 표현이 레거시 통짜 blob이라는 뜻이므로 로컬 매니페스트를
        비운다(표현 불일치 방지).

        tombstone(deleted) 레코드는 매니페스트를 복원하지 않고 비운다. 물리 블록이
        이미 삭제됐으므로 지워진 청크를 가리키는 매니페스트가 남으면 안 된다.
        """
        if chunks is None:
            return
        if deleted:
            self._metadata_store.delete_chunks(virtual_path)
            self._metadata_store.commit()
            return
        if chunks:
            self._metadata_store.put_chunks(virtual_path, chunks)
        else:
            self._metadata_store.delete_chunks(virtual_path)
        self._metadata_store.commit()

    def _insert_from_server(
        self, server_rec: FileMetadata, chunks: list | None = None
    ) -> None:
        """서버 레코드를 로컬 DB에 삽입 (이미 존재하면 갱신). tombstone 포함."""
        existing = self._metadata_store.lookup_any(server_rec.virtual_path)
        deleted_val = 1 if server_rec.deleted else 0
        if existing is not None:
            # 기존 레코드 갱신
            conn = self._metadata_store._get_conn()
            conn.execute(
                "UPDATE files SET source_id = ?, physical_path = ?, "
                "file_size = ?, created_at = ?, modified_at = ?, "
                "version = ?, device_id = ?, sync_status = 'synced', deleted = ?, "
                "replication_status = ? "
                "WHERE virtual_path = ?",
                (
                    server_rec.source_id,
                    server_rec.physical_path,
                    server_rec.file_size,
                    server_rec.created_at,
                    server_rec.modified_at,
                    server_rec.version,
                    server_rec.device_id,
                    deleted_val,
                    server_rec.replication_status,
                    server_rec.virtual_path,
                ),
            )
            conn.commit()
        else:
            # 새 레코드 삽입
            conn = self._metadata_store._get_conn()
            conn.execute(
                "INSERT INTO files "
                "(virtual_path, source_id, physical_path, file_size, "
                "created_at, modified_at, version, device_id, sync_status, deleted, "
                "replication_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'synced', ?, ?)",
                (
                    server_rec.virtual_path,
                    server_rec.source_id,
                    server_rec.physical_path,
                    server_rec.file_size,
                    server_rec.created_at,
                    server_rec.modified_at,
                    server_rec.version,
                    server_rec.device_id,
                    deleted_val,
                    server_rec.replication_status,
                ),
            )
            conn.commit()
        self._adopt_chunks(
            server_rec.virtual_path, chunks, server_rec.deleted
        )

    def _update_from_server(
        self, server_rec: FileMetadata, chunks: list | None = None
    ) -> None:
        """서버 메타데이터로 로컬 레코드를 갱신. tombstone(deleted) 전파 포함.

        서버 레코드가 tombstone이면 로컬 물리 파일도 삭제한다.
        """
        # tombstone 전파 시 물리 파일 삭제 (StoragePool 참조가 있을 때만)
        if server_rec.deleted and self._storage_pool is not None:
            local_rec = self._metadata_store.lookup(server_rec.virtual_path)
            if local_rec is not None:
                try:
                    # 청크 표현이면 청크 전부를, 레거시면 단일 블록을 지운다.
                    self._storage_pool._delete_local_blocks(local_rec)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    logger.warning(
                        "tombstone 물리 파일 삭제 실패 %s: %s",
                        server_rec.virtual_path, e,
                    )

        conn = self._metadata_store._get_conn()
        conn.execute(
            "UPDATE files SET source_id = ?, physical_path = ?, "
            "file_size = ?, modified_at = ?, "
            "version = ?, device_id = ?, sync_status = 'synced', deleted = ?, "
            "replication_status = ? "
            "WHERE virtual_path = ?",
            (
                server_rec.source_id,
                server_rec.physical_path,
                server_rec.file_size,
                server_rec.modified_at,
                server_rec.version,
                server_rec.device_id,
                1 if server_rec.deleted else 0,
                server_rec.replication_status,
                server_rec.virtual_path,
            ),
        )
        conn.commit()
        self._adopt_chunks(
            server_rec.virtual_path, chunks, server_rec.deleted
        )

    async def _force_upload(self) -> None:
        """pending 여부와 관계없이 로컬 metadata_db를 서버에 강제 업로드한다."""
        db_path = self._metadata_store._db_path
        if not os.path.exists(db_path):
            return

        try:
            token = await self._auth_client.get_valid_token()

            # WAL 모드에서 최신 데이터를 포함하려면 checkpoint 수행
            conn = self._metadata_store._get_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            with open(db_path, "rb") as f:
                db_blob = f.read()

            encrypted_blob = self._encrypt_blob(db_blob)

            response = await self._client.put(
                f"{self._server_url}/sync/metadata",
                headers={"Authorization": f"Bearer {token}"},
                content=encrypted_blob,
            )
            if response.status_code < 400:
                try:
                    resp_data = response.json()
                    if "version" in resp_data:
                        self._last_synced_version = resp_data["version"]
                except Exception:
                    pass
                # pending → synced 갱신 (중복 업로드로 인한 version 무한 증가 방지)
                for fm in self._metadata_store.get_pending_files():
                    self._metadata_store.set_sync_status(fm.virtual_path, "synced")
                self._record_sync_success()
                logger.info("Force upload completed.")
            else:
                logger.warning("Force upload failed: HTTP %d", response.status_code)
        except Exception as e:
            logger.warning("Force upload error: %s", e)

    # --- 파셜(레코드) 동기화 ---

    def _get_record_subkey(self) -> bytes:
        """record_id 파생용 subkey를 지연 파생하여 반환한다."""
        if self._record_subkey is None:
            from stardustlib.metadata_records import derive_record_subkey
            self._record_subkey = derive_record_subkey(self._encryption_key)
        return self._record_subkey

    async def _download_records(self) -> bool:
        """레코드 증분 다운로드 후 병합한다.

        Returns:
            레코드 미지원(404)이면 False(전체 blob 폴백 신호), 그 외 True.

        네트워크 예외는 상위 호출부에서 처리하도록 전파한다.
        """
        token = await self._auth_client.get_valid_token()
        resp = await self._client.get(
            f"{self._server_url}/sync/metadata/records",
            params={"since": self._last_synced_version},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            logger.warning("레코드 다운로드 실패: HTTP %d", resp.status_code)
            return True

        from stardustlib.metadata_records import (
            deserialize_chunks,
            deserialize_metadata,
            unpad_plaintext,
        )

        data = resp.json()
        records = data.get("records", [])
        for item in records:
            encrypted = base64.b64decode(item["encrypted_record"])
            padded = self._decrypt_blob(encrypted)
            plaintext = unpad_plaintext(padded)
            server_rec = deserialize_metadata(plaintext)
            # 청크 매니페스트는 파일 레코드에 함께 실려 온다(없으면 레거시 blob).
            self._merge_record(
                server_rec, chunks=deserialize_chunks(plaintext)
            )

        server_version = int(
            data.get("current_version", self._last_synced_version)
        )
        if server_version > self._last_synced_version:
            self._last_synced_version = server_version
        if records:
            self._record_sync_success()
            logger.info(
                "레코드 증분 병합: %d건, version=%d",
                len(records), server_version,
            )
        return True

    async def _upload_records(self) -> bool:
        """pending 레코드 증분 업로드 + 만료 tombstone purge (CAS).

        base_version(last_synced_version)으로 낙관적 잠금을 적용하고, 409 충돌 시
        증분 재다운로드·재병합 후 재시도한다.

        Returns:
            레코드 미지원(404)이면 False(전체 blob 폴백 신호), 그 외 True.
        """
        from stardustlib.metadata_records import (
            pad_plaintext,
            record_id_for,
            serialize_metadata,
        )

        subkey = self._get_record_subkey()

        for _ in range(_MAX_CAS_RETRIES):
            pending = self._metadata_store.get_pending_files()
            expired = self._metadata_store.list_expired_tombstones(
                self._retention_seconds
            )
            purge_ids = [record_id_for(subkey, p) for p in expired]
            if not pending and not purge_ids:
                return True

            records_payload = []
            for fm in pending:
                # 청크 표현이면 매니페스트를 레코드 페이로드에 함께 싣는다.
                chunks = self._metadata_store.get_chunks(fm.virtual_path)
                padded = pad_plaintext(serialize_metadata(fm, chunks))
                encrypted = self._encrypt_blob(padded)
                records_payload.append({
                    "record_id": record_id_for(subkey, fm.virtual_path),
                    "encrypted_record": base64.b64encode(
                        encrypted
                    ).decode("ascii"),
                })

            token = await self._auth_client.get_valid_token()
            resp = await self._client.put(
                f"{self._server_url}/sync/metadata/records",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "base_version": self._last_synced_version,
                    "records": records_payload,
                    "purge_ids": purge_ids,
                },
            )
            if resp.status_code == 404:
                return False
            if resp.status_code == 409:
                # CAS 충돌: 서버 변경 재병합 후 재시도
                logger.info(
                    "레코드 CAS 충돌 (base_version=%d) — 재병합 후 재시도",
                    self._last_synced_version,
                )
                handled = await self._download_records()
                if not handled:
                    return False
                continue
            if resp.status_code >= 400:
                self._consecutive_failures += 1
                logger.warning("레코드 업로드 실패: HTTP %d", resp.status_code)
                return True

            self._consecutive_failures = 0
            new_version = int(resp.json().get("version", self._last_synced_version))
            self._last_synced_version = new_version
            for fm in pending:
                self._metadata_store.set_sync_status(fm.virtual_path, "synced")
            if expired:
                self._metadata_store.purge_expired_tombstones(
                    self._retention_seconds
                )
            self._record_sync_success()
            logger.info(
                "레코드 업로드 완료: %d건 업서트, %d건 purge, version=%d",
                len(pending), len(purge_ids), new_version,
            )
            return True

        logger.warning(
            "레코드 업로드: CAS 재시도 %d회 초과, 다음 주기에 재시도",
            _MAX_CAS_RETRIES,
        )
        return True

    async def _download_and_merge(self) -> None:
        """서버 metadata version을 확인하고, 로컬보다 높으면 다운로드하여 병합한다."""
        logger.debug("_download_and_merge called (last_synced_version=%d)", self._last_synced_version)
        # 레코드 모드: 증분 다운로드 우선. 404면 전체 blob 경로로 폴백.
        if self._record_mode:
            try:
                if await self._download_records():
                    return
                self._record_mode = False
                logger.info("레코드 미지원(404) — 전체 blob 동기화로 폴백")
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.debug("레코드 다운로드 실패(network): %s", e)
                return
            except Exception as e:
                logger.debug("레코드 다운로드 실패: %s", e)
                return
        try:
            token = await self._auth_client.get_valid_token()

            # 서버 version 조회
            status_resp = await self._client.get(
                f"{self._server_url}/sync/metadata/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status_resp.status_code != 200:
                logger.warning(
                    "Sync status check failed: HTTP %d", status_resp.status_code
                )
                return

            server_status = status_resp.json()
            server_version = server_status.get("version")
            # tombstone 보관기간 정책 갱신 (서버가 알려준 값)
            days = server_status.get("tombstone_retention_days")
            if isinstance(days, int) and days > 0:
                self._retention_seconds = days * 86400
            if server_version is None:
                # 서버에 metadata 없음 — 로컬 데이터 강제 업로드
                await self._force_upload()
                return

            # 로컬 version과 비교
            if server_version <= self._last_synced_version:
                return  # 변경 없음

            # 서버가 더 높으면 다운로드
            response = await self._client.get(
                f"{self._server_url}/sync/metadata",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 200:
                return

            await self._merge_server_metadata(response.content)
            self._last_synced_version = server_version
            self._gc_tombstones()
            self._record_sync_success()
            logger.info(
                "Periodic sync: merged server version %d", server_version
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.debug("Periodic download failed (network): %s", e)
        except Exception as e:
            logger.debug("Periodic merge failed: %s", e)

    # --- 암호화/복호화 헬퍼 ---

    def _encrypt_blob(self, plaintext: bytes) -> bytes:
        """AES-256-GCM으로 blob을 암호화한다.

        encryption_key가 None이면 평문 그대로 반환 (개발/테스트용).
        반환 형식: iv(12B) + tag(16B) + ciphertext
        """
        if self._encryption_key is None:
            return plaintext

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = os.urandom(12)
        aesgcm = AESGCM(self._encryption_key)
        ct_with_tag = aesgcm.encrypt(iv, plaintext, None)
        # AESGCM.encrypt() returns ciphertext + tag(16B)
        ciphertext = ct_with_tag[:-16]
        tag = ct_with_tag[-16:]
        return iv + tag + ciphertext

    def _decrypt_blob(self, encrypted: bytes) -> bytes:
        """AES-256-GCM으로 blob을 복호화한다.

        encryption_key가 None이면 평문으로 간주하여 그대로 반환.
        입력 형식: iv(12B) + tag(16B) + ciphertext

        Raises:
            SyncError: 복호화 실패 시 (키 불일치 또는 데이터 손상).
        """
        if self._encryption_key is None:
            return encrypted

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(encrypted) < 28:  # 12 + 16 minimum
            from stardustlib.exceptions import KeyMismatchError
            raise KeyMismatchError(
                "서버 metadata blob 크기가 최소 크기(28바이트) 미만입니다. "
                "서버에 저장된 데이터가 손상되었거나 암호화되지 않은 "
                "레거시 데이터입니다."
            )

        iv = encrypted[:12]
        tag = encrypted[12:28]
        ciphertext = encrypted[28:]

        aesgcm = AESGCM(self._encryption_key)
        ct_with_tag = ciphertext + tag

        try:
            return aesgcm.decrypt(iv, ct_with_tag, None)
        except Exception as e:
            from stardustlib.exceptions import KeyMismatchError
            raise KeyMismatchError(
                f"서버 metadata 복호화 실패: {e}. "
                f"key_file이 서버에 업로드한 PC와 동일한지 확인하세요. "
                f"새 디바이스라면 먼저 key 복원(GET /sync/key)을 수행하세요."
            ) from e
