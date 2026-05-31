"""JBOD 스토리지 통합 관리자.

복수의 Storage Source를 단일 논리 볼륨으로 통합하고,
파일 배치 전략을 관리한다.
"""

import logging
import time
import uuid

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import InsufficientStorageError
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import EntryInfo, FileInfo
from stardustlib.storage_source import StorageSource

logger = logging.getLogger(__name__)


class JBODManager:
    """JBOD 스토리지 통합 관리자.

    모든 활성 Storage Source의 파일을 단일 네임스페이스로 통합하고,
    파일 배치 전략(Most-Available-Space)을 적용한다.
    """

    def __init__(
        self,
        sources: list[StorageSource],
        metadata_store: MetadataStore,
        encryption_engine: EncryptionEngine | None = None,
        device_id: str | None = None,
    ) -> None:
        """JBODManager 초기화.

        Args:
            sources: Storage Source 목록.
            metadata_store: 메타데이터 저장소.
            encryption_engine: 암호화 엔진 (None이면 암호화 비활성).
            device_id: 이 클라이언트의 디바이스 ID (파일 변경 추적용, 선택).
        """
        self.sources = sources
        self.metadata_store = metadata_store
        self.encryption_engine = encryption_engine
        self.device_id = device_id
        # 개선 1: source_id → StorageSource dict (O(1) 조회)
        self._source_map: dict[str, StorageSource] = {
            s.source_id: s for s in sources
        }

    # --- 소스 관리 ---

    def select_source(self, file_size: int) -> StorageSource:
        """파일 크기 이상의 여유 공간을 가진 소스 중 가용 공간이 가장 많은 소스 선택.

        Args:
            file_size: 저장할 파일 크기 (바이트).

        Returns:
            선택된 StorageSource.

        Raises:
            InsufficientStorageError: 조건을 만족하는 소스가 없을 때.
        """
        best_source: StorageSource | None = None
        best_space: int = -1

        for source in self.sources:
            if not source.is_active:
                continue
            available = source.get_available_space()
            if available >= file_size and available > best_space:
                best_source = source
                best_space = available

        if best_source is None:
            raise InsufficientStorageError(
                f"모든 활성 소스의 여유 공간이 {file_size} 바이트 미만"
            )
        return best_source

    def _get_source_by_id(self, source_id: str) -> StorageSource | None:
        """source_id로 소스를 찾는다. O(1)."""
        return self._source_map.get(source_id)

    def add_source(self, source: StorageSource) -> None:
        """소스를 동적으로 추가한다 (예: 인증 후 RemoteSource 마운트).

        동일 source_id가 이미 있으면 교체한다.
        """
        # 기존 동일 id 소스 제거 후 추가 (중복 방지)
        self.sources = [s for s in self.sources if s.source_id != source.source_id]
        self.sources.append(source)
        self._source_map[source.source_id] = source

    def _generate_physical_path(self, virtual_path: str) -> str:
        """가상 경로에서 물리 경로를 생성한다.

        UUID 기반으로 충돌 없는 물리 경로를 생성한다.
        """
        # 가상 경로의 디렉토리 구조를 유지하면서 파일명에 UUID 추가
        parts = virtual_path.strip("/").split("/")
        if len(parts) > 1:
            dir_part = "/".join(parts[:-1])
            file_part = f"{uuid.uuid4().hex}_{parts[-1]}"
            return f"{dir_part}/{file_part}"
        return f"{uuid.uuid4().hex}_{parts[0]}"

    # --- 파일 작업 ---

    def read_file(self, virtual_path: str) -> bytes:
        """파일을 읽어 복호화된 데이터를 반환한다.

        Args:
            virtual_path: 가상 파일 경로.

        Returns:
            복호화된 파일 데이터.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
            OSError: 소스가 비활성 상태일 때.
        """
        metadata = self.metadata_store.lookup(virtual_path)
        if metadata is None:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {virtual_path}")

        source = self._get_source_by_id(metadata.source_id)
        if source is None or not source.is_active:
            raise OSError(
                f"파일이 위치한 소스가 비활성 상태입니다: {metadata.source_id}"
            )

        encrypted_data = source.read(metadata.physical_path)

        if self.encryption_engine is not None:
            return self.encryption_engine.decrypt(encrypted_data)
        return encrypted_data

    def write_file(self, virtual_path: str, data: bytes) -> None:
        """파일을 암호화하여 저장하고 메타데이터를 기록한다.

        원자적 트랜잭션을 보장한다: 실패 시 부분 파일 삭제 + 메타데이터 롤백.

        Args:
            virtual_path: 가상 파일 경로.
            data: 저장할 원본 데이터.

        Raises:
            InsufficientStorageError: 공간 부족 시.
        """
        if self.encryption_engine is not None:
            encrypted = self.encryption_engine.encrypt(data)
        else:
            encrypted = data

        existing = self.metadata_store.lookup(virtual_path)

        if existing is not None:
            # 기존 파일 덮어쓰기
            source = self._get_source_by_id(existing.source_id)
            if source is None or not source.is_active:
                raise OSError(
                    f"파일이 위치한 소스가 비활성 상태입니다: {existing.source_id}"
                )
            self.metadata_store.begin_transaction()
            try:
                source.write(existing.physical_path, encrypted)
                self.metadata_store.update(
                    virtual_path,
                    file_size=len(data),
                    modified_at=time.time(),
                    device_id=self.device_id,
                )
                self.metadata_store.commit()
            except OSError as e:
                self.metadata_store.rollback()
                if "insufficient space" in str(e).lower():
                    raise InsufficientStorageError(str(e)) from e
                raise
            except Exception:
                self.metadata_store.rollback()
                raise
        else:
            # 새 파일 생성
            source = self.select_source(len(encrypted))
            physical_path = self._generate_physical_path(virtual_path)

            self.metadata_store.begin_transaction()
            try:
                source.write(physical_path, encrypted)
                now = time.time()
                self.metadata_store.insert(
                    virtual_path=virtual_path,
                    source_id=source.source_id,
                    physical_path=physical_path,
                    file_size=len(data),
                    created_at=now,
                    modified_at=now,
                    device_id=self.device_id,
                )
                self.metadata_store.commit()
            except Exception:
                self.metadata_store.rollback()
                # 부분 기록된 파일 정리
                if source.exists(physical_path):
                    source.delete(physical_path)
                raise

    def delete_file(self, virtual_path: str) -> None:
        """파일을 삭제하고 메타데이터를 제거한다.

        Args:
            virtual_path: 삭제할 파일의 가상 경로.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
        """
        metadata = self.metadata_store.lookup(virtual_path)
        if metadata is None:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {virtual_path}")

        source = self._get_source_by_id(metadata.source_id)
        if source is not None and source.is_active:
            try:
                source.delete(metadata.physical_path)
            except FileNotFoundError:
                logger.warning(
                    "물리 파일이 이미 삭제됨: %s", metadata.physical_path
                )

        self.metadata_store.delete(virtual_path)

    def move_file(self, src_path: str, dst_path: str) -> None:
        """파일을 이동(이름 변경)한다.

        동일 소스 내에서 메타데이터의 Virtual_Path만 갱신한다.

        Args:
            src_path: 원본 가상 경로.
            dst_path: 대상 가상 경로.

        Raises:
            FileNotFoundError: 원본 파일이 존재하지 않을 때.
        """
        metadata = self.metadata_store.lookup(src_path)
        if metadata is None:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {src_path}")

        self.metadata_store.rename_path(src_path, dst_path)

    def copy_file(self, src_path: str, dst_path: str) -> None:
        """파일을 복사한다.

        원본 파일을 읽어 새 경로에 쓴다.

        Args:
            src_path: 원본 가상 경로.
            dst_path: 대상 가상 경로.

        Raises:
            FileNotFoundError: 원본 파일이 존재하지 않을 때.
        """
        data = self.read_file(src_path)
        self.write_file(dst_path, data)

    def file_exists(self, virtual_path: str) -> bool:
        """파일 존재 여부를 확인한다."""
        return self.metadata_store.lookup(virtual_path) is not None

    def get_file_info(self, virtual_path: str) -> FileInfo | None:
        """파일 상세 정보를 반환한다."""
        metadata = self.metadata_store.lookup(virtual_path)
        if metadata is None:
            return None
        return FileInfo(
            virtual_path=metadata.virtual_path,
            source_id=metadata.source_id,
            file_size=metadata.file_size,
            created_at=metadata.created_at,
            modified_at=metadata.modified_at,
            is_directory=False,
        )

    # --- 디렉토리 작업 ---

    def list_directory(self, virtual_path: str) -> list[EntryInfo]:
        """메타데이터 기반 디렉토리 목록을 조회한다.

        파일과 디렉토리를 통합하여 중복 제거된 엔트리 목록을 반환한다.

        Args:
            virtual_path: 조회할 디렉토리의 가상 경로.

        Returns:
            해당 디렉토리의 직접 하위 엔트리 목록.
        """
        entries = self.metadata_store.list_entries(virtual_path)
        return entries

    def create_directory(self, virtual_path: str) -> None:
        """모든 활성 소스에 디렉토리를 생성한다.

        일부 소스에서 생성이 실패하면 로그에 기록하고,
        성공한 소스에서는 디렉토리를 유지한다 (Req 2.9).

        Args:
            virtual_path: 생성할 디렉토리의 가상 경로.
        """
        physical_path = virtual_path.lstrip("/")

        for source in self.sources:
            if not source.is_active:
                continue
            try:
                source.mkdir(physical_path)
            except Exception as e:
                logger.error(
                    "Failed to create directory in source %s: %s",
                    source.source_id,
                    e,
                )

        # 메타데이터에 디렉토리 기록
        now = time.time()
        # 경로 정규화: 슬래시로 끝나지 않도록 저장
        dir_path = virtual_path.rstrip("/")
        self.metadata_store.insert_directory(dir_path, now)

    def delete_directory(self, virtual_path: str) -> None:
        """디렉토리 및 하위 파일을 재귀적으로 삭제한다.

        Args:
            virtual_path: 삭제할 디렉토리의 가상 경로.
        """
        # 하위 엔트리 조회 및 재귀 삭제
        entries = self.metadata_store.list_entries(virtual_path)
        for entry in entries:
            child_path = virtual_path.rstrip("/") + "/" + entry.name
            if entry.is_directory:
                self.delete_directory(child_path)
            else:
                try:
                    self.delete_file(child_path)
                except FileNotFoundError:
                    logger.warning(
                        "삭제 대상 파일이 이미 없음: %s", child_path
                    )

        # 모든 활성 소스에서 물리 디렉토리 삭제
        physical_path = virtual_path.lstrip("/")
        for source in self.sources:
            if not source.is_active:
                continue
            try:
                source.rmdir(physical_path)
            except Exception:
                pass

        # 메타데이터에서 디렉토리 제거
        dir_path = virtual_path.rstrip("/")
        self.metadata_store.delete_directory_entry(dir_path)

    def move_directory(self, src_path: str, dst_path: str) -> None:
        """디렉토리를 이동하고 하위 파일 경로를 일괄 갱신한다.

        Args:
            src_path: 원본 디렉토리 가상 경로.
            dst_path: 대상 디렉토리 가상 경로.
        """
        # 접두사 정규화: 슬래시로 끝나도록
        old_prefix = src_path if src_path.endswith("/") else src_path + "/"
        new_prefix = dst_path if dst_path.endswith("/") else dst_path + "/"

        # 메타데이터 일괄 갱신 (files + directories 테이블)
        self.metadata_store.rename_directory(old_prefix, new_prefix)

        # 디렉토리 자체의 메타데이터도 갱신
        old_dir = src_path.rstrip("/")
        new_dir = dst_path.rstrip("/")
        self.metadata_store._conn.execute(
            "UPDATE directories SET virtual_path = ? WHERE virtual_path = ?",
            (new_dir, old_dir),
        )
        self.metadata_store._conn.commit()

        # 물리 디렉토리 이동 (각 활성 소스)
        src_physical = src_path.lstrip("/")
        dst_physical = dst_path.lstrip("/")
        for source in self.sources:
            if not source.is_active:
                continue
            try:
                # 대상 디렉토리 생성 후 원본 삭제 방식
                source.mkdir(dst_physical)
                # 원본 물리 디렉토리는 파일이 이미 이동되었으므로 삭제 시도
                if source.exists(src_physical):
                    source.rmdir(src_physical)
            except Exception as e:
                logger.error(
                    "Failed to move directory in source %s: %s",
                    source.source_id,
                    e,
                )

    # --- 용량 정보 ---

    def get_total_space(self) -> int:
        """모든 활성 소스의 전체 공간 합계를 반환한다."""
        total = 0
        for source in self.sources:
            if source.is_active:
                total += source.get_total_space()
        return total

    def get_available_space(self) -> int:
        """모든 활성 소스의 가용 공간 합계를 반환한다."""
        available = 0
        for source in self.sources:
            if source.is_active:
                available += source.get_available_space()
        return available

    def deactivate_source(self, source_id: str) -> None:
        """소스를 비활성 상태로 전환한다.

        Raises:
            ValueError: 해당 source_id의 소스가 존재하지 않을 때.
        """
        source = self._get_source_by_id(source_id)
        if source is None:
            raise ValueError(f"소스를 찾을 수 없습니다: {source_id}")
        source._deactivate(f"Manually deactivated: {source_id}")
