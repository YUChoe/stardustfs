"""SQLCipher 기반 메타데이터 저장소.

pysqlcipher3가 사용 가능하면 암호화된 SQLite를 사용하고,
사용 불가능하면 표준 sqlite3로 폴백한다 (개발/테스트 환경용).
"""

import logging
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

    파일 위치 및 속성 정보를 SQLite에 저장하여
    O(log n) 조회 성능을 제공한다.
    """

    def __init__(self, db_path: str, encryption_key: bytes) -> None:
        """MetadataStore 초기화.

        Args:
            db_path: SQLite 데이터베이스 파일 경로.
            encryption_key: SQLCipher 암호화 키 (32바이트).
        """
        self._db_path = db_path
        self._encryption_key = encryption_key
        self._conn: Any = None

    def initialize(self) -> None:
        """데이터베이스 연결 및 스키마 생성.

        연결 타임아웃은 10초로 설정된다.
        pysqlcipher3 사용 가능 시 PRAGMA key로 암호화 키를 설정한다.
        """
        self._conn = sqlite3_module.connect(
            self._db_path, timeout=_CONNECTION_TIMEOUT
        )
        self._conn.row_factory = sqlite3_module.Row

        if _ENCRYPTION_AVAILABLE:
            key_hex = self._encryption_key.hex()
            self._conn.execute(f"PRAGMA key = \"x'{key_hex}'\"")

        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info("Metadata Store 초기화 완료: %s", self._db_path)

    # --- 파일 메타데이터 CRUD ---

    def insert(
        self,
        virtual_path: str,
        source_id: str,
        physical_path: str,
        file_size: int,
        created_at: float,
        modified_at: float,
    ) -> None:
        """파일 메타데이터를 삽입한다.

        Args:
            virtual_path: 가상 경로 (유일성 제약).
            source_id: Storage Source ID.
            physical_path: 물리적 파일 경로.
            file_size: 파일 크기 (바이트).
            created_at: 생성 시간 (UTC 타임스탬프).
            modified_at: 수정 시간 (UTC 타임스탬프).
        """
        self._conn.execute(
            "INSERT INTO files "
            "(virtual_path, source_id, physical_path, file_size, created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (virtual_path, source_id, physical_path, file_size, created_at, modified_at),
        )
        self._conn.commit()

    def update(self, virtual_path: str, file_size: int, modified_at: float) -> None:
        """파일 메타데이터를 갱신한다.

        Args:
            virtual_path: 갱신할 파일의 가상 경로.
            file_size: 새 파일 크기.
            modified_at: 새 수정 시간.
        """
        self._conn.execute(
            "UPDATE files SET file_size = ?, modified_at = ? WHERE virtual_path = ?",
            (file_size, modified_at, virtual_path),
        )
        self._conn.commit()

    def delete(self, virtual_path: str) -> None:
        """파일 메타데이터를 삭제한다.

        Args:
            virtual_path: 삭제할 파일의 가상 경로.
        """
        self._conn.execute(
            "DELETE FROM files WHERE virtual_path = ?",
            (virtual_path,),
        )
        self._conn.commit()

    def lookup(self, virtual_path: str) -> FileMetadata | None:
        """가상 경로로 파일 메타데이터를 조회한다.

        인덱스 기반 O(log n) 조회를 보장한다.

        Args:
            virtual_path: 조회할 가상 경로.

        Returns:
            FileMetadata 또는 None (미존재 시).
        """
        cursor = self._conn.execute(
            "SELECT virtual_path, source_id, physical_path, file_size, "
            "created_at, modified_at FROM files WHERE virtual_path = ?",
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
        )


    # --- 디렉토리 메타데이터 ---

    def insert_directory(self, virtual_path: str, created_at: float) -> None:
        """디렉토리 메타데이터를 삽입한다.

        Args:
            virtual_path: 디렉토리 가상 경로 (유일성 제약).
            created_at: 생성 시간 (UTC 타임스탬프).
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO directories (virtual_path, created_at) "
            "VALUES (?, ?)",
            (virtual_path, created_at),
        )
        self._conn.commit()

    def lookup_directory(self, virtual_path: str) -> bool:
        """디렉토리가 메타데이터에 등록되어 있는지 확인한다.

        Args:
            virtual_path: 확인할 디렉토리 가상 경로.

        Returns:
            디렉토리가 존재하면 True, 아니면 False.
        """
        cursor = self._conn.execute(
            "SELECT 1 FROM directories WHERE virtual_path = ?",
            (virtual_path,),
        )
        return cursor.fetchone() is not None

    def delete_directory_entry(self, virtual_path: str) -> None:
        """디렉토리 메타데이터를 삭제한다.

        Args:
            virtual_path: 삭제할 디렉토리의 가상 경로.
        """
        self._conn.execute(
            "DELETE FROM directories WHERE virtual_path = ?",
            (virtual_path,),
        )
        self._conn.commit()

    # --- 디렉토리 작업 ---

    def list_entries(self, directory_path: str) -> list[EntryInfo]:
        """디렉토리 하위의 직접 엔트리 목록을 반환한다.

        파일 테이블과 디렉토리 테이블 모두에서 조회하여
        통합된 목록을 반환한다.

        Args:
            directory_path: 조회할 디렉토리 경로 (예: "/docs/").
                           슬래시로 끝나야 한다.

        Returns:
            해당 디렉토리의 직접 하위 엔트리 목록.
        """
        # 경로 정규화: 슬래시로 끝나도록
        if not directory_path.endswith("/"):
            directory_path += "/"

        results: dict[str, EntryInfo] = {}

        # 파일 엔트리 조회: directory_path로 시작하되 하위 디렉토리의 파일은 제외
        cursor = self._conn.execute(
            "SELECT virtual_path, file_size, created_at, modified_at "
            "FROM files WHERE virtual_path LIKE ? || '%'",
            (directory_path,),
        )
        for row in cursor.fetchall():
            vpath: str = row["virtual_path"]
            # directory_path 이후의 상대 경로 추출
            relative = vpath[len(directory_path):]
            if not relative:
                continue
            # 직접 하위 파일만 (슬래시가 없는 경우)
            if "/" not in relative:
                results[relative] = EntryInfo(
                    name=relative,
                    is_directory=False,
                    file_size=row["file_size"],
                    created_at=row["created_at"],
                    modified_at=row["modified_at"],
                )
            else:
                # 하위 디렉토리 이름 추출
                dir_name = relative.split("/")[0]
                if dir_name not in results:
                    results[dir_name] = EntryInfo(
                        name=dir_name,
                        is_directory=True,
                        file_size=0,
                        created_at=row["created_at"],
                        modified_at=row["modified_at"],
                    )

        # 디렉토리 테이블에서 직접 하위 디렉토리 조회
        cursor = self._conn.execute(
            "SELECT virtual_path, created_at "
            "FROM directories WHERE virtual_path LIKE ? || '%'",
            (directory_path,),
        )
        for row in cursor.fetchall():
            vpath = row["virtual_path"]
            relative = vpath[len(directory_path):]
            if not relative:
                continue
            # 슬래시 제거 후 직접 하위만
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
        """파일의 가상 경로를 변경한다.

        Args:
            old_path: 기존 가상 경로.
            new_path: 새 가상 경로.
        """
        self._conn.execute(
            "UPDATE files SET virtual_path = ? WHERE virtual_path = ?",
            (new_path, old_path),
        )
        self._conn.commit()

    def rename_directory(self, old_prefix: str, new_prefix: str) -> None:
        """디렉토리 이동 시 하위 모든 파일의 가상 경로 접두사를 갱신한다.

        Args:
            old_prefix: 기존 디렉토리 경로 접두사.
            new_prefix: 새 디렉토리 경로 접두사.
        """
        # files 테이블: old_prefix로 시작하는 모든 경로 갱신
        self._conn.execute(
            "UPDATE files SET virtual_path = ? || SUBSTR(virtual_path, ?) "
            "WHERE virtual_path LIKE ? || '%'",
            (new_prefix, len(old_prefix) + 1, old_prefix),
        )
        # directories 테이블: old_prefix로 시작하는 모든 경로 갱신
        self._conn.execute(
            "UPDATE directories SET virtual_path = ? || SUBSTR(virtual_path, ?) "
            "WHERE virtual_path LIKE ? || '%'",
            (new_prefix, len(old_prefix) + 1, old_prefix),
        )
        self._conn.commit()

    # --- 트랜잭션 관리 ---

    def begin_transaction(self) -> None:
        """명시적 트랜잭션을 시작한다."""
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        """현재 트랜잭션을 커밋한다."""
        self._conn.commit()

    def rollback(self) -> None:
        """현재 트랜잭션을 롤백한다."""
        self._conn.rollback()

    def close(self) -> None:
        """데이터베이스 연결을 닫는다."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
