"""메타데이터/키 동기화 클라이언트.

중앙 서버와 metadata_db 및 key_file을 동기화한다.
오프라인 우선(offline-first) 설계로, 서버 연결 불가 시에도
로컬 기능은 정상 동작한다.
"""

from __future__ import annotations

import asyncio
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
    ) -> None:
        self._auth_client = auth_client
        self._server_url = server_url.rstrip("/")
        self._metadata_store = metadata_store
        self._conflict_resolver = conflict_resolver
        self._interval_seconds = interval_seconds
        self._encryption_key = encryption_key
        self._sync_task: asyncio.Task[None] | None = None
        self._running = False
        self._consecutive_failures = 0
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)

    async def initial_sync(self) -> None:
        """시작 시 서버에서 metadata_db 다운로드 및 병합."""
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
            # 서버에 metadata가 아직 없음 — 첫 동기화
            logger.info("No server metadata found. Skipping merge.")
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

    async def start_periodic_sync(self) -> None:
        """주기적 동기화 루프 시작."""
        if self._running:
            return
        self._running = True
        self._sync_task = asyncio.create_task(self._periodic_loop())
        logger.info(
            "Periodic sync started (interval=%ds).", self._interval_seconds
        )

    async def upload_metadata(self) -> None:
        """로컬 metadata_db 스냅샷을 서버에 업로드."""
        db_path = self._metadata_store._db_path
        if not os.path.exists(db_path):
            logger.warning("Metadata DB not found: %s", db_path)
            return

        try:
            token = await self._auth_client.get_valid_token()
            with open(db_path, "rb") as f:
                db_blob = f.read()

            # 서버 전송 전 AES-256-GCM 암호화
            encrypted_blob = self._encrypt_blob(db_blob)

            response = await self._client.put(
                f"{self._server_url}/sync/metadata",
                headers={"Authorization": f"Bearer {token}"},
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
        # pending → synced 갱신
        pending_files = self._metadata_store.get_pending_files()
        for fm in pending_files:
            self._metadata_store.set_sync_status(fm.virtual_path, "synced")
        logger.info("Metadata uploaded successfully.")

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
        await self._client.aclose()
        logger.info("Sync client stopped.")

    # --- 내부 메서드 ---

    async def _periodic_loop(self) -> None:
        """interval_seconds마다 upload_metadata()를 호출하는 루프."""
        while self._running:
            try:
                await asyncio.sleep(self._interval_seconds)
                if not self._running:
                    break
                await self.upload_metadata()
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
            server_store.initialize()

            # 서버 DB의 모든 파일 레코드 조회
            server_conn = server_store._get_conn()
            cursor = server_conn.execute(
                "SELECT virtual_path, source_id, physical_path, file_size, "
                "created_at, modified_at, version, device_id, sync_status "
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
                ))

            # 각 서버 레코드에 대해 로컬과 비교 병합
            for server_rec in server_records:
                self._merge_record(server_rec)

            server_store.close()
        finally:
            # 임시 파일 정리
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _merge_record(self, server_rec: FileMetadata) -> None:
        """단일 레코드의 version 비교 기반 병합 로직.

        병합 규칙:
        1. server_version > local_base_version AND local_version > local_base_version
           → ConflictResolver.resolve_conflict()
        2. server_version > local_version (충돌 아님)
           → 서버 메타데이터로 로컬 갱신
        3. local_version > server_version (충돌 아님)
           → 다음 업로드 시 반영 (아무 작업 안 함)
        4. version 동일 → 변경 없음
        """
        local_rec = self._metadata_store.lookup(server_rec.virtual_path)

        if local_rec is None:
            # 로컬에 없는 파일 — 서버에서 새로 추가된 파일
            self._insert_from_server(server_rec)
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
            self._handle_conflict(server_rec, local_rec)
        elif server_version > local_version:
            # 서버가 더 최신 → 서버 메타데이터로 갱신
            self._update_from_server(server_rec)
        elif local_version > server_version:
            # 로컬이 더 최신 → 다음 업로드 시 반영 (아무 작업 안 함)
            pass
        else:
            # version 동일 → 변경 없음
            pass

    def _handle_conflict(
        self, server_rec: FileMetadata, local_rec: FileMetadata
    ) -> None:
        """충돌 처리: conflict copy 생성 후 서버 메타데이터를 원본에 적용."""
        try:
            conflict_path = self._conflict_resolver.resolve_conflict(
                server_rec.virtual_path, server_rec.version
            )
            # 서버 메타데이터를 원본 경로에 삽입
            self._insert_from_server(server_rec)
            logger.info(
                "Conflict resolved: %s → conflict copy: %s",
                server_rec.virtual_path, conflict_path,
            )
        except Exception as e:
            logger.error(
                "Conflict resolution failed for %s: %s",
                server_rec.virtual_path, e,
            )

    def _insert_from_server(self, server_rec: FileMetadata) -> None:
        """서버 레코드를 로컬 DB에 삽입 (이미 존재하면 갱신)."""
        existing = self._metadata_store.lookup(server_rec.virtual_path)
        if existing is not None:
            # 기존 레코드 갱신
            conn = self._metadata_store._get_conn()
            conn.execute(
                "UPDATE files SET source_id = ?, physical_path = ?, "
                "file_size = ?, created_at = ?, modified_at = ?, "
                "version = ?, device_id = ?, sync_status = 'synced' "
                "WHERE virtual_path = ?",
                (
                    server_rec.source_id,
                    server_rec.physical_path,
                    server_rec.file_size,
                    server_rec.created_at,
                    server_rec.modified_at,
                    server_rec.version,
                    server_rec.device_id,
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
                "created_at, modified_at, version, device_id, sync_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'synced')",
                (
                    server_rec.virtual_path,
                    server_rec.source_id,
                    server_rec.physical_path,
                    server_rec.file_size,
                    server_rec.created_at,
                    server_rec.modified_at,
                    server_rec.version,
                    server_rec.device_id,
                ),
            )
            conn.commit()

    def _update_from_server(self, server_rec: FileMetadata) -> None:
        """서버 메타데이터로 로컬 레코드를 갱신."""
        conn = self._metadata_store._get_conn()
        conn.execute(
            "UPDATE files SET source_id = ?, physical_path = ?, "
            "file_size = ?, modified_at = ?, "
            "version = ?, device_id = ?, sync_status = 'synced' "
            "WHERE virtual_path = ?",
            (
                server_rec.source_id,
                server_rec.physical_path,
                server_rec.file_size,
                server_rec.modified_at,
                server_rec.version,
                server_rec.device_id,
                server_rec.virtual_path,
            ),
        )
        conn.commit()

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
        """
        if self._encryption_key is None:
            return encrypted

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(encrypted) < 28:  # 12 + 16 minimum
            logger.warning("Encrypted blob too short, treating as plaintext")
            return encrypted

        iv = encrypted[:12]
        tag = encrypted[12:28]
        ciphertext = encrypted[28:]

        aesgcm = AESGCM(self._encryption_key)
        ct_with_tag = ciphertext + tag

        try:
            return aesgcm.decrypt(iv, ct_with_tag, None)
        except Exception as e:
            logger.warning(
                "Metadata blob decryption failed: %s. "
                "Treating as plaintext (legacy unencrypted data).",
                e,
            )
            return encrypted
