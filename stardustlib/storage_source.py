"""스토리지 소스 추상 클래스 및 구현체 (Directory, Loopback).

StorageSource ABC를 정의하고, DirectorySource와 LoopbackSource 구현체를 제공한다.
"""

import logging
import os
import shutil
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Loopback 파일 크기 제한
MIN_LOOPBACK_SIZE = 10 * 1024 * 1024  # 10MB
MAX_LOOPBACK_SIZE = 2 * 1024 * 1024 * 1024 * 1024  # 2TB


class StorageSource(ABC):
    """스토리지 소스 추상 기본 클래스.

    개별 스토리지 단위를 추상화하여 Directory와 Loopback 두 유형을
    통일된 인터페이스로 제공한다.
    """

    def __init__(self, source_id: str, path: str) -> None:
        self._source_id = source_id
        self._path = path
        self._active = False

    @property
    def source_id(self) -> str:
        """소스 고유 ID를 반환한다."""
        return self._source_id

    @property
    def path(self) -> str:
        """소스 루트 경로를 반환한다."""
        return self._path

    @property
    def is_active(self) -> bool:
        """소스가 활성 상태인지 반환한다."""
        return self._active

    def _deactivate(self, reason: str) -> None:
        """소스를 비활성 상태로 전환하고 로그를 기록한다."""
        self._active = False
        logger.error(
            "Storage source '%s' deactivated: %s", self._source_id, reason
        )

    @abstractmethod
    def initialize(self) -> None:
        """소스를 초기화하고 사용 가능 상태로 만든다."""
        ...

    @abstractmethod
    def read(self, physical_path: str) -> bytes:
        """physical_path(소스 루트 상대 경로)의 파일을 읽어 반환한다."""
        ...

    @abstractmethod
    def write(self, physical_path: str, data: bytes) -> None:
        """physical_path에 data를 기록한다."""
        ...

    @abstractmethod
    def delete(self, physical_path: str) -> None:
        """physical_path의 파일을 삭제한다."""
        ...

    @abstractmethod
    def exists(self, physical_path: str) -> bool:
        """physical_path가 존재하는지 확인한다."""
        ...

    @abstractmethod
    def mkdir(self, physical_path: str) -> None:
        """physical_path에 디렉토리를 생성한다."""
        ...

    @abstractmethod
    def rmdir(self, physical_path: str) -> None:
        """physical_path의 디렉토리를 삭제한다."""
        ...

    @abstractmethod
    def list_dir(self, physical_path: str) -> list[str]:
        """physical_path 디렉토리의 엔트리 목록을 반환한다."""
        ...

    @abstractmethod
    def get_available_space(self) -> int:
        """사용 가능한 공간(바이트)을 반환한다."""
        ...

    @abstractmethod
    def get_total_space(self) -> int:
        """전체 공간(바이트)을 반환한다."""
        ...


class DirectorySource(StorageSource):
    """로컬 디렉토리를 스토리지 소스로 사용하는 구현체.

    physical_path는 소스 루트(self._path) 기준 상대 경로로 해석된다.
    접근 불가 시 비활성 상태로 전환한다 (Req 3.6).
    """

    def __init__(self, source_id: str, path: str) -> None:
        super().__init__(source_id, path)

    def initialize(self) -> None:
        """디렉토리 존재 및 읽기/쓰기 권한을 검증하고 활성화한다.

        경로가 존재하지 않거나 접근 불가능하면 비활성 상태로 전환한다.
        """
        if not os.path.isdir(self._path):
            self._deactivate(f"Path does not exist: {self._path}")
            return
        if not os.access(self._path, os.R_OK | os.W_OK):
            self._deactivate(
                f"Insufficient permissions (read/write): {self._path}"
            )
            return
        self._active = True
        logger.info(
            "DirectorySource '%s' initialised at %s",
            self._source_id,
            self._path,
        )

    def _resolve(self, physical_path: str) -> str:
        """상대 physical_path를 절대 경로로 변환한다."""
        return os.path.join(self._path, physical_path)

    def _check_active(self) -> None:
        """소스가 활성 상태인지 확인하고, 아니면 예외를 발생시킨다."""
        if not self._active:
            raise OSError(
                f"Storage source '{self._source_id}' is not active"
            )

    def read(self, physical_path: str) -> bytes:
        """파일을 읽어 바이트열로 반환한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            with open(full_path, "rb") as f:
                return f.read()
        except PermissionError as e:
            self._deactivate(f"Read permission denied: {physical_path}: {e}")
            raise
        except FileNotFoundError:
            raise
        except OSError as e:
            self._deactivate(f"Read failed for {physical_path}: {e}")
            raise

    def write(self, physical_path: str, data: bytes) -> None:
        """파일에 데이터를 기록한다. 상위 디렉토리가 없으면 생성한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            parent = os.path.dirname(full_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(data)
        except PermissionError as e:
            self._deactivate(
                f"Write permission denied: {physical_path}: {e}"
            )
            raise
        except FileNotFoundError:
            raise
        except OSError as e:
            self._deactivate(f"Write failed for {physical_path}: {e}")
            raise

    def delete(self, physical_path: str) -> None:
        """파일을 삭제한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            os.remove(full_path)
        except PermissionError as e:
            self._deactivate(
                f"Delete permission denied: {physical_path}: {e}"
            )
            raise
        except FileNotFoundError:
            raise
        except OSError as e:
            self._deactivate(f"Delete failed for {physical_path}: {e}")
            raise

    def exists(self, physical_path: str) -> bool:
        """경로가 존재하는지 확인한다."""
        self._check_active()
        return os.path.exists(self._resolve(physical_path))

    def mkdir(self, physical_path: str) -> None:
        """디렉토리를 생성한다 (중간 경로 포함)."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            os.makedirs(full_path, exist_ok=True)
        except PermissionError as e:
            self._deactivate(f"mkdir permission denied: {physical_path}: {e}")
            raise
        except OSError as e:
            self._deactivate(f"mkdir failed for {physical_path}: {e}")
            raise

    def rmdir(self, physical_path: str) -> None:
        """디렉토리를 재귀적으로 삭제한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            shutil.rmtree(full_path)
        except PermissionError as e:
            self._deactivate(f"rmdir permission denied: {physical_path}: {e}")
            raise
        except OSError as e:
            self._deactivate(f"rmdir failed for {physical_path}: {e}")
            raise

    def list_dir(self, physical_path: str) -> list[str]:
        """디렉토리 내 엔트리 이름 목록을 반환한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            return os.listdir(full_path)
        except PermissionError as e:
            self._deactivate(
                f"list_dir permission denied: {physical_path}: {e}"
            )
            raise
        except OSError as e:
            self._deactivate(f"list_dir failed for {physical_path}: {e}")
            raise

    def get_available_space(self) -> int:
        """사용 가능한 디스크 공간(바이트)을 반환한다."""
        self._check_active()
        try:
            usage = shutil.disk_usage(self._path)
            return usage.free
        except OSError as e:
            self._deactivate(f"Failed to get disk usage: {e}")
            raise

    def get_total_space(self) -> int:
        """전체 디스크 공간(바이트)을 반환한다."""
        self._check_active()
        try:
            usage = shutil.disk_usage(self._path)
            return usage.total
        except OSError as e:
            self._deactivate(f"Failed to get disk usage: {e}")
            raise


class LoopbackSource(StorageSource):
    """고정 크기 파일을 논리적 스토리지로 사용.

    루프백 파일(예: vault.img)과 동반 디렉토리(vault.img.d/)를 사용한다.
    루프백 파일은 용량 예약 마커 역할을 하며, 실제 데이터는 동반 디렉토리에 저장된다.
    사용 공간은 동반 디렉토리의 실제 사용량으로 추적하며,
    size_bytes를 초과하는 쓰기를 거부한다.
    """

    def __init__(self, source_id: str, path: str, size_bytes: int) -> None:
        super().__init__(source_id, path)
        if size_bytes < MIN_LOOPBACK_SIZE or size_bytes > MAX_LOOPBACK_SIZE:
            raise ValueError(
                f"Loopback size must be between {MIN_LOOPBACK_SIZE} and "
                f"{MAX_LOOPBACK_SIZE}: got {size_bytes}"
            )
        self._size_bytes = size_bytes
        self._companion_dir = path + ".d"
        self._used_bytes = 0  # 증분 추적용

    @property
    def companion_dir(self) -> str:
        """동반 디렉토리 경로."""
        return self._companion_dir

    def initialize(self) -> None:
        """루프백 소스 초기화.

        - 기존 파일이 있으면 덮어쓰지 않고 활성화 (Req 3.8)
        - 없으면 새로 생성 후 초기화 (Req 3.3)
        - 실패 시 부분 파일 삭제 및 비활성 처리 (Req 3.7)
        """
        if os.path.isfile(self._path):
            self._mount()
            return

        try:
            self._create_loopback_file()
            self._mount()
        except Exception as e:
            logger.error(
                "LoopbackSource '%s' initialisation failed: %s",
                self._source_id,
                e,
            )
            self._cleanup_partial()
            self._active = False

    def _create_loopback_file(self) -> None:
        """루프백 파일과 동반 디렉토리를 생성한다."""
        parent_dir = os.path.dirname(self._path)
        if parent_dir and not os.path.isdir(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        # sparse 파일 생성
        with open(self._path, "wb") as f:
            f.seek(self._size_bytes - 1)
            f.write(b"\x00")

        os.makedirs(self._companion_dir, exist_ok=True)
        logger.info(
            "LoopbackSource '%s' file created: %s (%d bytes)",
            self._source_id,
            self._path,
            self._size_bytes,
        )

    def _mount(self) -> None:
        """논리적 마운트: 파일 존재 확인 후 활성화."""
        if not os.path.isfile(self._path):
            self._deactivate(f"Mount failed, file missing: {self._path}")
            return

        if not os.path.isdir(self._companion_dir):
            os.makedirs(self._companion_dir, exist_ok=True)

        # 초기 사용량 계산 (마운트 시 1회만)
        self._used_bytes = self._scan_used_space()
        self._active = True
        logger.info(
            "LoopbackSource '%s' mounted: %s (used: %d bytes)",
            self._source_id, self._path, self._used_bytes,
        )

    def _unmount(self) -> None:
        """논리적 언마운트: 비활성 처리."""
        self._active = False
        logger.info(
            "LoopbackSource '%s' unmounted: %s", self._source_id, self._path
        )

    def _cleanup_partial(self) -> None:
        """초기화 실패 시 부분 생성된 파일/디렉토리 삭제."""
        if os.path.isfile(self._path):
            try:
                os.remove(self._path)
            except OSError as e:
                logger.warning("Failed to remove partial file %s: %s",
                               self._path, e)
        if os.path.isdir(self._companion_dir):
            try:
                shutil.rmtree(self._companion_dir)
            except OSError as e:
                logger.warning("Failed to remove companion dir %s: %s",
                               self._companion_dir, e)

    def _resolve(self, physical_path: str) -> str:
        """물리 경로를 동반 디렉토리 기준으로 해석."""
        return os.path.join(self._companion_dir, physical_path)

    def _scan_used_space(self) -> int:
        """동반 디렉토리의 실제 사용 공간을 전체 스캔으로 계산. 초기화 시 1회 사용."""
        total_size = 0
        for dirpath, _dirnames, filenames in os.walk(self._companion_dir):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                try:
                    total_size += os.path.getsize(filepath)
                except OSError:
                    pass
        return total_size

    def _check_active(self) -> None:
        """소스가 활성 상태인지 확인한다."""
        if not self._active:
            raise OSError(
                f"Storage source '{self._source_id}' is not active"
            )

    def read(self, physical_path: str) -> bytes:
        """파일을 읽어 반환한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        with open(full_path, "rb") as f:
            return f.read()

    def write(self, physical_path: str, data: bytes) -> None:
        """파일에 데이터를 기록한다. 공간 초과 시 OSError를 발생시킨다."""
        self._check_active()
        full_path = self._resolve(physical_path)

        existing_size = 0
        if os.path.isfile(full_path):
            existing_size = os.path.getsize(full_path)

        needed = len(data) - existing_size
        if needed > 0 and needed > (self._size_bytes - self._used_bytes):
            raise OSError(
                f"LoopbackSource '{self._source_id}' insufficient space: "
                f"need {needed}, available {self._size_bytes - self._used_bytes}"
            )

        parent = os.path.dirname(full_path)
        os.makedirs(parent, exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(data)

        # 증분 추적: 기존 크기를 빼고 새 크기를 더함
        self._used_bytes += len(data) - existing_size

    def delete(self, physical_path: str) -> None:
        """파일을 삭제한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        if os.path.isfile(full_path):
            file_size = os.path.getsize(full_path)
            os.remove(full_path)
            self._used_bytes = max(0, self._used_bytes - file_size)

    def exists(self, physical_path: str) -> bool:
        """경로가 존재하는지 확인한다."""
        self._check_active()
        return os.path.exists(self._resolve(physical_path))

    def mkdir(self, physical_path: str) -> None:
        """디렉토리를 생성한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        os.makedirs(full_path, exist_ok=True)

    def rmdir(self, physical_path: str) -> None:
        """디렉토리를 재귀적으로 삭제한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)

    def list_dir(self, physical_path: str) -> list[str]:
        """디렉토리 내 엔트리 이름 목록을 반환한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        if not os.path.isdir(full_path):
            return []
        return os.listdir(full_path)

    def get_available_space(self) -> int:
        """가용 공간 = 예약 크기 - 추적된 사용량. O(1)."""
        self._check_active()
        return max(0, self._size_bytes - self._used_bytes)

    def get_total_space(self) -> int:
        """전체 공간 = 예약된 크기."""
        self._check_active()
        return self._size_bytes
