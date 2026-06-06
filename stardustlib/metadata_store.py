"""SQLCipher 기반 메타데이터 저장소.

pysqlcipher3가 사용 가능하면 암호화된 SQLite를 사용하고,
사용 불가능하면 표준 sqlite3로 폴백한다 (개발/테스트 환경용).

멀티스레드 환경(cheroot)에서 안전하게 동작하도록
스레드 로컬 연결과 WAL 모드를 사용한다.
"""

import logging
import shutil
import threading
import time
from typing import Any

from stardustlib.models import EntryInfo, FileMetadata

logger = logging.getLogger(__name__)

# pysqlcipher3 임포트 시도, 실패 시 표준 sqlite3 폴백
try:
    from pysqlcipher3 import dbapi2 as sqlite3_module

    _ENCRYPTION_AVAILABLE = True
except ImportError:
    import sqlite3 as sqlite3_module  # type: ignore[no-redef]

    _ENCRYPTION_AVAILABLE = False
    logger.warning(
        "pysqlcipher3를 사용할 수 없습니다. "
        "암호화 없이 표준 sqlite3를 사용합니다."
    )

_CONNECTION_TIMEOUT = 10  # 초


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_path TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    physical_path TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    modified_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_virtual_path ON files(virtual_path);
CREATE INDEX IF NOT EXISTS idx_files_source_id ON files(source_id);

CREATE TABLE IF NOT EXISTS directories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_path TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_directories_virtual_path ON directories(virtual_path);
"""


class MetadataStore:
    """SQLCipher 기반 암호화 메타데이터 저장소.

    멀티스레드 환경에서 안전하게 동작하도록 스레드 로컬 연결을 사용한다.
    WAL 모드로 읽기/쓰기 동시성을 향상시킨다.
    """

    def __init__(self, db_path: str, encryption_key: bytes) -> None:
        self._db_path = db_path
        self._encryption_key = encryption_key
        self._local = threading.local()
        self._lock = threading.Lock()
        self._initialized = False

    def _get_conn(self) -> Any:
        """현재 스레드의 DB 연결을 반환한다. 없으면 새로 생성."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3_module.connect(
                self._db_path, timeout=_CONNECTION_TIMEOUT,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3_module.Row
            if _ENCRYPTION_AVAILABLE:
                key_hex = self._encryption_key.hex()
                conn.execute(f"PRAGMA key = \"x'{key_hex}'\"")
            # WAL 모드: 읽기/쓰기 동시성 향상
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    @property
    def _conn(self) -> Any:
        """하위 호환성을 위한 프로퍼티."""
        return self._get_conn()

    def initialize(self) -> None:
        """데이터베이스 스키마를 생성한다."""
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        self._migrate_to_v2()
        self._migrate_to_v3()
        self._migrate_to_v4()
        self._migrate_to_v5()
        self._initialized = True
        logger.info("Metadata Store 초기화 완료: %s", self._db_path)

    def _needs_migration(self) -> bool:
        """v2 마이그레이션이 필요한지 판단한다.

        schema_version 테이블이 존재하고 version >= 2이면 불필요.
        files 테이블에 version 컬럼이 없으면 필요.
        """
        conn = self._get_conn()

        # schema_version 테이블 존재 여부 확인
        cursor = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        )
        if cursor.fetchone() is not None:
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            if row is not None and row["version"] >= 2:
                return False

        # files 테이블에 version 컬럼 존재 여부 확인
        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "version" in columns:
            return False

        return True

    def _migrate_to_v2(self) -> None:
        """v1 → v2 스키마 마이그레이션을 수행한다.

        - DB 파일 백업 ("{원본}.v1.bak")
        - files 테이블에 version, device_id, sync_status 컬럼 추가
        - schema_version 테이블 생성 및 버전 기록
        - 기존 레코드 초기값 설정
        - 실패 시 트랜잭션 롤백
        """
        if not self._needs_migration():
            return

        # DB 파일 백업
        backup_path = f"{self._db_path}.v1.bak"
        try:
            shutil.copy2(self._db_path, backup_path)
            logger.info("DB 백업 생성: %s", backup_path)
        except OSError as e:
            logger.error("DB 백업 실패, 마이그레이션 중단: %s", e)
            return

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # files 테이블에 컬럼 추가
            conn.execute(
                "ALTER TABLE files ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            )
            conn.execute(
                "ALTER TABLE files ADD COLUMN device_id TEXT"
            )
            conn.execute(
                "ALTER TABLE files ADD COLUMN sync_status TEXT DEFAULT 'synced'"
            )

            # 기존 레코드 초기값 설정
            conn.execute(
                "UPDATE files SET version = 1, sync_status = 'synced' "
                "WHERE version = 1"
            )

            # schema_version 테이블 생성 및 버전 기록
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "    id INTEGER PRIMARY KEY CHECK (id = 1),"
                "    version INTEGER NOT NULL,"
                "    migrated_at REAL NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (id, version, migrated_at) "
                "VALUES (1, 2, ?)",
                (time.time(),),
            )

            conn.commit()
            logger.info("MetadataStore v2 스키마 마이그레이션 완료")
        except Exception as e:
            conn.rollback()
            logger.error("스키마 마이그레이션 실패, 롤백 수행: %s", e)
            raise

    def _migrate_to_v3(self) -> None:
        """v2 → v3 스키마 마이그레이션을 수행한다.

        - files 테이블에 deleted 컬럼 추가 (tombstone, 삭제 동기화용)
        - schema_version을 3으로 갱신
        이미 deleted 컬럼이 존재하면 아무 작업도 하지 않는다.
        """
        conn = self._get_conn()

        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "deleted" in columns:
            return

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "ALTER TABLE files ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (id, version, migrated_at) "
                "VALUES (1, 3, ?)",
                (time.time(),),
            )
            conn.commit()
            logger.info("MetadataStore v3 스키마 마이그레이션 완료 (tombstone)")
        except Exception as e:
            conn.rollback()
            logger.error("v3 스키마 마이그레이션 실패, 롤백 수행: %s", e)
            raise

    def _migrate_to_v4(self) -> None:
        """v3 → v4 스키마 마이그레이션을 수행한다.

        - files 테이블에 replication_status 컬럼 추가 (none|pending|replicated)
        - schema_version을 4로 갱신
        이미 컬럼이 존재하면 아무 작업도 하지 않는다. 기존 레코드는 DEFAULT 'none'.
        """
        conn = self._get_conn()

        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "replication_status" in columns:
            return

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "ALTER TABLE files ADD COLUMN replication_status "
                "TEXT NOT NULL DEFAULT 'none'"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (id, version, migrated_at) "
                "VALUES (1, 4, ?)",
                (time.time(),),
            )
            conn.commit()
            logger.info(
                "MetadataStore v4 스키마 마이그레이션 완료 (replication_status)"
            )
        except Exception as e:
            conn.rollback()
            logger.error("v4 스키마 마이그레이션 실패, 롤백 수행: %s", e)
            raise

    def _migrate_to_v5(self) -> None:
        """v4 → v5: files에 evicted 컬럼 추가 (로컬 원본 축출=복제본 전용 표시).

        이미 컬럼이 있으면 아무 작업도 하지 않는다. 기존 레코드는 DEFAULT 0.
        """
        conn = self._get_conn()
        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "evicted" in columns:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "ALTER TABLE files ADD COLUMN evicted INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (id, version, migrated_at) "
                "VALUES (1, 5, ?)",
                (time.time(),),
            )
            conn.commit()
            logger.info("MetadataStore v5 스키마 마이그레이션 완료 (evicted)")
        except Exception as e:
            conn.rollback()
            logger.error("v5 스키마 마이그레이션 실패, 롤백 수행: %s", e)
            raise

    # --- 파일 메타데이터 CRUD ---

    _VALID_SYNC_STATUSES = ("synced", "pending", "conflict")
    _VALID_REPLICATION_STATUSES = ("none", "pending", "replicated")
    def insert(
        self,
        virtual_path: str,
        source_id: str,
        physical_path: str,
        file_size: int,
        created_at: float,
        modified_at: float,
        device_id: str | None = None,
    ) -> None:
        """파일 메타데이터를 삽입한다.

        version=1, sync_status="pending"으로 초기 삽입한다.
        device_id는 이 변경을 수행한 디바이스 ID (없으면 NULL).
        동일 경로에 tombstone(삭제 마커)이 남아 있으면 재활성화한다.
        """
        conn = self._get_conn()
        # 동일 경로에 tombstone이 있으면 재활성화 (version 증가로 삭제보다 우선)
        existing = conn.execute(
            "SELECT version FROM files WHERE virtual_path = ?",
            (virtual_path,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE files SET source_id = ?, physical_path = ?, "
                "file_size = ?, created_at = ?, modified_at = ?, "
                "version = version + 1, sync_status = 'pending', deleted = 0, "
                "device_id = ? "
                "WHERE virtual_path = ?",
                (source_id, physical_path, file_size, created_at, modified_at,
                 device_id, virtual_path),
            )
            conn.commit()
            return
        conn.execute(
            "INSERT INTO files "
            "(virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at, version, sync_status, deleted, device_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', 0, ?)",
            (virtual_path, source_id, physical_path, file_size, created_at,
             modified_at, device_id),
        )
        conn.commit()

    def update(
        self,
        virtual_path: str,
        file_size: int,
        modified_at: float,
        device_id: str | None = None,
        source_id: str | None = None,
        physical_path: str | None = None,
    ) -> None:
        """파일 메타데이터를 갱신한다.

        version을 1 증가시키고 sync_status를 "pending"으로 설정한다.
        device_id는 이 변경을 수행한 디바이스 ID (없으면 기존 값 유지).
        source_id/physical_path는 소유권 이전(takeover) 시 물리 위치를 함께
        갱신하기 위해 사용한다 (없으면 기존 값 유지).
        """
        conn = self._get_conn()
        set_clauses = [
            "file_size = ?",
            "modified_at = ?",
            "version = version + 1",
            "sync_status = 'pending'",
            "evicted = 0",  # 내용/위치 갱신 = 재구체화 → 축출 플래그 해제
        ]
        params: list = [file_size, modified_at]
        if device_id is not None:
            set_clauses.append("device_id = ?")
            params.append(device_id)
        if source_id is not None:
            set_clauses.append("source_id = ?")
            params.append(source_id)
        if physical_path is not None:
            set_clauses.append("physical_path = ?")
            params.append(physical_path)
        params.append(virtual_path)
        conn.execute(
            f"UPDATE files SET {', '.join(set_clauses)} WHERE virtual_path = ?",
            tuple(params),
        )
        conn.commit()

    def increment_version(self, virtual_path: str, device_id: str) -> None:
        """파일의 version을 1 증가시키고 device_id를 설정한다."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET version = version + 1, device_id = ? "
            "WHERE virtual_path = ?",
            (device_id, virtual_path),
        )
        conn.commit()

    def set_sync_status(self, virtual_path: str, status: str) -> None:
        """sync_status를 변경한다.

        Args:
            virtual_path: 대상 파일의 가상 경로.
            status: 유효값 "synced", "pending", "conflict".

        Raises:
            ValueError: 유효하지 않은 status 값.
        """
        if status not in self._VALID_SYNC_STATUSES:
            raise ValueError(
                f"유효하지 않은 sync_status: {status!r}. "
                f"허용값: {self._VALID_SYNC_STATUSES}"
            )
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET sync_status = ? WHERE virtual_path = ?",
            (status, virtual_path),
        )
        conn.commit()

    def set_replication_status(self, virtual_path: str, status: str) -> None:
        """replication_status를 변경한다 (none|pending|replicated).

        Raises:
            ValueError: 유효하지 않은 status 값.
        """
        if status not in self._VALID_REPLICATION_STATUSES:
            raise ValueError(
                f"유효하지 않은 replication_status: {status!r}. "
                f"허용값: {self._VALID_REPLICATION_STATUSES}"
            )
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET replication_status = ? WHERE virtual_path = ?",
            (status, virtual_path),
        )
        conn.commit()

    def get_replication_status(self, virtual_path: str) -> str | None:
        """파일의 replication_status를 반환한다(레코드 없으면 None)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT replication_status FROM files WHERE virtual_path = ?",
            (virtual_path,),
        ).fetchone()
        return row["replication_status"] if row is not None else None

    def mark_evicted(self, virtual_path: str) -> None:
        """로컬 원본 축출(복제본 전용)로 표시한다. evicted는 디바이스-로컬 상태로
        version/sync_status를 바꾸지 않는다(동기화로 전파되지 않음)."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET evicted = 1 WHERE virtual_path = ?",
            (virtual_path,),
        )
        conn.commit()

    def list_eviction_candidates(self) -> list[FileMetadata]:
        """축출 후보(replicated·미축출·활성)를 오래된 순(modified_at ASC)으로 반환한다.

        로컬 소유 여부는 호출자(jbod)가 소스로 필터한다.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at, version, device_id, sync_status, deleted, "
            "evicted FROM files WHERE deleted = 0 AND evicted = 0 "
            "AND replication_status = 'replicated' ORDER BY modified_at ASC",
        )
        return [self._row_to_metadata(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_metadata(row) -> FileMetadata:
        """files 행을 FileMetadata로 변환한다(evicted 포함, 키 없으면 False)."""
        keys = row.keys()
        return FileMetadata(
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
            evicted=bool(row["evicted"]) if "evicted" in keys else False,
        )

    def list_files_in_source(self, source_id: str) -> list[FileMetadata]:
        """해당 소스에 저장된 활성(deleted=0) 파일 목록을 반환한다(evacuate 대상)."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at, version, device_id, sync_status, deleted "
            "FROM files WHERE source_id = ? AND deleted = 0",
            (source_id,),
        )
        return [
            FileMetadata(
                virtual_path=row["virtual_path"], source_id=row["source_id"],
                physical_path=row["physical_path"], file_size=row["file_size"],
                created_at=row["created_at"], modified_at=row["modified_at"],
                version=row["version"], device_id=row["device_id"],
                sync_status=row["sync_status"], deleted=bool(row["deleted"]),
            )
            for row in cursor.fetchall()
        ]

    def list_virtual_paths_for_replication(
        self, statuses: tuple[str, ...], owner_device_id: str | None = None
    ) -> list[str]:
        """리플리케이션 대상 가상 경로 목록을 반환한다(자동 백업/heal용).

        deleted=0 이고 replication_status가 statuses 중 하나인 파일. owner_device_id가
        주어지면 그 device 소유(또는 레거시 NULL=로컬)만 포함한다 — 다른 device가
        소유한 원격 파일은 그 device가 백업하므로 제외한다.
        """
        if not statuses:
            return []
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in statuses)
        sql = (
            "SELECT virtual_path FROM files "
            "WHERE deleted = 0 AND COALESCE(replication_status, 'none') "
            f"IN ({placeholders})"
        )
        params: list = list(statuses)
        if owner_device_id is not None:
            sql += " AND (device_id = ? OR device_id IS NULL)"
            params.append(owner_device_id)
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [row["virtual_path"] for row in rows]

    def count_replication_by_sources(
        self, source_ids: list[str]
    ) -> dict[str, int]:
        """주어진 소스(이 디바이스의 로컬 소스)에 저장된 활성 파일의 백업 상태 집계.

        반환 {none, pending, replicated}. 다른 디바이스가 소유(원격 소스)한 파일은
        제외되므로, 각 디바이스는 자신이 책임지는(로컬 저장) 파일의 백업 상태만 센다.
        source_ids가 비면 모두 0.
        """
        counts = {"none": 0, "pending": 0, "replicated": 0}
        if not source_ids:
            return counts
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            "SELECT COALESCE(replication_status, 'none') AS st, COUNT(*) AS n "
            f"FROM files WHERE deleted = 0 AND source_id IN ({placeholders}) "
            "GROUP BY st",
            tuple(source_ids),
        ).fetchall()
        for row in rows:
            if row["st"] in counts:
                counts[row["st"]] = row["n"]
        return counts

    def get_pending_files(self) -> list[FileMetadata]:
        """sync_status가 "pending"인 모든 파일 목록을 반환한다.

        tombstone(deleted=1)도 pending이면 포함한다 (삭제 동기화 업로드 대상).
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at, version, device_id, sync_status, deleted "
            "FROM files WHERE sync_status = 'pending'"
        )
        results: list[FileMetadata] = []
        for row in cursor.fetchall():
            results.append(FileMetadata(
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
            ))
        return results

    def delete(self, virtual_path: str) -> None:
        """파일을 tombstone으로 표시한다 (soft delete).

        실제 행을 제거하지 않고 deleted=1, version+1, sync_status='pending'으로
        설정하여 삭제 사실이 다른 디바이스로 동기화되도록 한다.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET deleted = 1, version = version + 1, "
            "sync_status = 'pending', modified_at = ? "
            "WHERE virtual_path = ?",
            (time.time(), virtual_path),
        )
        conn.commit()

    def purge_expired_tombstones(self, retention_seconds: float) -> int:
        """보관기간이 지난 tombstone을 물리적으로 제거한다 (GC).

        deleted=1이고 modified_at이 (현재시각 - retention_seconds)보다 오래된
        레코드만 삭제한다. 활성(deleted=0) 레코드는 절대 삭제하지 않는다.

        Args:
            retention_seconds: tombstone 보관기간(초).

        Returns:
            삭제된 tombstone 레코드 수.
        """
        cutoff = time.time() - retention_seconds
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM files WHERE deleted = 1 AND modified_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount if cursor.rowcount is not None else 0

    def lookup(self, virtual_path: str) -> FileMetadata | None:
        """가상 경로로 파일 메타데이터를 조회한다.

        tombstone(deleted=1)은 존재하지 않는 것으로 간주하여 None을 반환한다.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at, version, device_id, sync_status, deleted, "
            "evicted FROM files WHERE virtual_path = ? AND deleted = 0",
            (virtual_path,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def lookup_any(self, virtual_path: str) -> FileMetadata | None:
        """tombstone을 포함하여 가상 경로로 레코드를 조회한다 (동기화 병합용)."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at, version, device_id, sync_status, deleted "
            "FROM files WHERE virtual_path = ?",
            (virtual_path,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return FileMetadata(
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
        )

    def live_physical_paths_for_device(
        self, device_id: str
    ) -> set[tuple[str, str]]:
        """orphan GC용 보존 집합을 반환한다.

        삭제되지 않은(deleted=0) 레코드 중, 현재 디바이스 소유(device_id 일치)
        이거나 소유자 미지정(device_id IS NULL, 레거시)인 것의
        (source_id, physical_path) 집합을 반환한다.

        이 집합에 포함된 물리 파일은 활성 metadata가 참조하므로 GC 대상에서
        제외(보존)해야 한다.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT source_id, physical_path FROM files "
            "WHERE deleted = 0 AND (device_id = ? OR device_id IS NULL)",
            (device_id,),
        )
        return {
            (row["source_id"], row["physical_path"])
            for row in cursor.fetchall()
        }

    # --- 디렉토리 메타데이터 ---

    def insert_directory(self, virtual_path: str, created_at: float) -> None:
        """디렉토리 메타데이터를 삽입한다."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO directories (virtual_path, created_at) "
            "VALUES (?, ?)",
            (virtual_path, created_at),
        )
        conn.commit()

    def lookup_directory(self, virtual_path: str) -> bool:
        """디렉토리가 메타데이터에 등록되어 있는지 확인한다."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT 1 FROM directories WHERE virtual_path = ?",
            (virtual_path,),
        )
        return cursor.fetchone() is not None

    def delete_directory_entry(self, virtual_path: str) -> None:
        """디렉토리 메타데이터를 삭제한다."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM directories WHERE virtual_path = ?",
            (virtual_path,),
        )
        conn.commit()

    # --- 디렉토리 작업 ---

    def list_entries(self, directory_path: str) -> list[EntryInfo]:
        """디렉토리 하위의 직접 엔트리 목록을 반환한다."""
        conn = self._get_conn()

        if not directory_path.endswith("/"):
            directory_path += "/"

        results: dict[str, EntryInfo] = {}

        cursor = conn.execute(
            "SELECT virtual_path, file_size, created_at, modified_at "
            "FROM files WHERE virtual_path LIKE ? || '%' AND deleted = 0",
            (directory_path,),
        )
        for row in cursor.fetchall():
            vpath: str = row["virtual_path"]
            relative = vpath[len(directory_path):]
            if not relative:
                continue
            if "/" not in relative:
                results[relative] = EntryInfo(
                    name=relative,
                    is_directory=False,
                    file_size=row["file_size"],
                    created_at=row["created_at"],
                    modified_at=row["modified_at"],
                )
            else:
                dir_name = relative.split("/")[0]
                if dir_name not in results:
                    results[dir_name] = EntryInfo(
                        name=dir_name,
                        is_directory=True,
                        file_size=0,
                        created_at=row["created_at"],
                        modified_at=row["modified_at"],
                    )

        cursor = conn.execute(
            "SELECT virtual_path, created_at "
            "FROM directories WHERE virtual_path LIKE ? || '%'",
            (directory_path,),
        )
        for row in cursor.fetchall():
            vpath = row["virtual_path"]
            relative = vpath[len(directory_path):]
            if not relative:
                continue
            relative = relative.rstrip("/")
            if "/" not in relative and relative not in results:
                results[relative] = EntryInfo(
                    name=relative,
                    is_directory=True,
                    file_size=0,
                    created_at=row["created_at"],
                    modified_at=row["created_at"],
                )

        return list(results.values())

    def rename_path(self, old_path: str, new_path: str) -> None:
        """파일의 가상 경로를 변경한다."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET virtual_path = ? WHERE virtual_path = ?",
            (new_path, old_path),
        )
        conn.commit()

    def rename_directory(self, old_prefix: str, new_prefix: str) -> None:
        """디렉토리 이동 시 하위 모든 파일의 가상 경로 접두사를 갱신한다."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE files SET virtual_path = ? || SUBSTR(virtual_path, ?) "
            "WHERE virtual_path LIKE ? || '%'",
            (new_prefix, len(old_prefix) + 1, old_prefix),
        )
        conn.execute(
            "UPDATE directories SET virtual_path = ? || SUBSTR(virtual_path, ?) "
            "WHERE virtual_path LIKE ? || '%'",
            (new_prefix, len(old_prefix) + 1, old_prefix),
        )
        conn.commit()

    # --- 트랜잭션 관리 ---

    def begin_transaction(self) -> None:
        """명시적 트랜잭션을 시작한다."""
        self._get_conn().execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        """현재 트랜잭션을 커밋한다."""
        self._get_conn().commit()

    def rollback(self) -> None:
        """현재 트랜잭션을 롤백한다."""
        self._get_conn().rollback()

    def close(self) -> None:
        """현재 스레드의 데이터베이스 연결을 닫는다."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
