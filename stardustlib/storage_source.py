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

    @property
    def is_remote(self) -> bool:
        """원격(크로스 디바이스 프록시) 소스인지 반환한다.

        로컬 쓰기 가능 소스는 False. 원격 소스는 읽기 전용 라우팅 대상이며
        로컬 용량 집계/쓰기 대상 선택에서 제외된다.
        """
        return False

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

    def list_physical_files(self) -> list[str]:
        """소스 데이터 루트 직속의 물리 파일명 목록을 반환한다 (orphan GC용).

        물리 파일은 데이터 루트에 평평한 '<uuid>_<name>' 형식으로 저장된다.
        하위 디렉토리는 제외한다. 기본 구현은 빈 목록(원격 소스 등 미지원).
        """
        return []

    def close(self) -> None:
        """소스 핸들을 정리한다(기본 no-op). 이미지 기반 소스가 오버라이드한다."""
        return None

    def write_chunk(
        self, physical_path: str, data: bytes, offset: int, total_size: int
    ) -> None:
        """대용량 전송용 청크 기록(홀더 측).

        offset=0이면 total_size로 용량을 검사하고 파일을 새로 만들어(truncate)
        0에 기록한다. offset>0이면 같은 파일의 offset 위치에 이어 기록한다.
        로컬 소스(Loopback/Directory)가 seek 기반으로 오버라이드한다. 기본은 미구현
        (RemoteSource는 홀더 대상이 아님).
        """
        raise NotImplementedError

    def read_chunk(self, physical_path: str, offset: int, length: int) -> bytes:
        """대용량 전송용 청크 읽기(홀더 측). offset부터 length 바이트 반환."""
        raise NotImplementedError


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

    def write_chunk(
        self, physical_path: str, data: bytes, offset: int, total_size: int
    ) -> None:
        """청크 기록. offset=0은 새 파일 생성, 이후는 seek 기록(용량은 디스크 free)."""
        self._check_active()
        full_path = self._resolve(physical_path)
        try:
            if offset == 0:
                parent = os.path.dirname(full_path)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                with open(full_path, "wb") as f:
                    f.write(data)
            else:
                with open(full_path, "r+b") as f:
                    f.seek(offset)
                    f.write(data)
        except OSError as e:
            self._deactivate(f"write_chunk failed for {physical_path}: {e}")
            raise

    def read_chunk(self, physical_path: str, offset: int, length: int) -> bytes:
        """offset부터 length 바이트를 읽어 반환한다."""
        self._check_active()
        full_path = self._resolve(physical_path)
        with open(full_path, "rb") as f:
            f.seek(offset)
            return f.read(length)

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

    def list_physical_files(self) -> list[str]:
        """소스 루트의 물리 파일 상대 경로 목록을 반환한다 (orphan GC용).

        청크는 샤드 서브디렉토리(`<hh>/<chunk_ref>`)에 저장되므로 루트 직속뿐 아니라
        하위 디렉토리까지 재귀로 훑는다. 반환 경로는 소스 루트 기준 상대 POSIX 경로다.
        """
        self._check_active()
        names: list[str] = []
        try:
            for root, _dirs, files in os.walk(self._path):
                rel_dir = os.path.relpath(root, self._path)
                for name in files:
                    if rel_dir == ".":
                        names.append(name)
                    else:
                        names.append(
                            f"{rel_dir.replace(os.sep, '/')}/{name}"
                        )
        except OSError as e:
            logger.warning(
                "list_physical_files 실패 (%s): %s", self._source_id, e
            )
            return []
        return names

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
    """고정 크기 FAT 이미지를 논리적 스토리지로 사용한다(파일 내 파일시스템).

    `<path>`는 pyfatfs로 포맷된 FAT 이미지이며, 모든 파일은 이미지 내부에 저장된다
    (별도 동반 디렉토리 없음). size_bytes로 용량이 실제 한정된다(`mount -o loop` 유사).
    쓰기는 데몬 단독(작업 큐 직렬화), 조회 세션은 read_only로 연다. 기존 비-FAT
    `.img`/`.img.d`는 마이그레이션 없이 새 FAT로 재포맷한다(데이터 폐기 — 사용자가
    배포 전 기존 소스를 비운다고 가정).

    용량은 내부 파일 크기 합으로 근사 추적하고, 실제 한정은 쓰기 시 FAT 공간 부족
    예외(PyFATException)를 OSError(insufficient space)로 변환해 집행한다.
    """

    _FAT32_THRESHOLD = 2 * 1024 * 1024 * 1024  # >2GiB면 FAT32

    def __init__(
        self, source_id: str, path: str, size_bytes: int,
        *, read_only: bool = False,
    ) -> None:
        super().__init__(source_id, path)
        if size_bytes < MIN_LOOPBACK_SIZE or size_bytes > MAX_LOOPBACK_SIZE:
            raise ValueError(
                f"Loopback size must be between {MIN_LOOPBACK_SIZE} and "
                f"{MAX_LOOPBACK_SIZE}: got {size_bytes}"
            )
        self._size_bytes = size_bytes
        self._read_only = read_only
        self._fs = None          # PyFatFS 핸들(마운트 후)
        self._used_bytes = 0     # 내부 파일 크기 합(근사)
        # p2p_server 경로 검증이 호스트 루트 containment 검사를 건너뛰도록 표시
        # (FAT는 이미지 내부로 경로를 샌드박스한다).
        self._image_backed = True

    @property
    def read_only(self) -> bool:
        return self._read_only

    # ------------------------------------------------------------------
    # 초기화 / 마운트 / 포맷
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """FAT 이미지를 보장하고 마운트한다.

        - 유효한 FAT 이미지면 마운트, 아니면(신규/비-FAT) 사전할당+mkfs 후 마운트.
        - read_only인데 아직 FAT 이미지가 없으면(데몬 미생성) 비활성으로 둔다.
        """
        try:
            from pyfatfs.PyFatFS import PyFatFS  # noqa: F401
        except Exception as e:  # noqa: BLE001
            self._deactivate(f"pyfatfs 미설치로 루프백 사용 불가: {e}")
            return
        try:
            if not self._is_fat_image():
                if self._read_only:
                    self._deactivate(
                        f"FAT image not ready (read-only): {self._path}"
                    )
                    return
                self._format()
            self._mount()
        except Exception as e:  # noqa: BLE001
            logger.error(
                "LoopbackSource '%s' initialisation failed: %s",
                self._source_id, e,
            )
            self._active = False

    def _is_fat_image(self) -> bool:
        """`<path>`가 열리는 FAT 이미지인지 확인한다(read_only 프로브)."""
        if not os.path.isfile(self._path):
            return False
        from pyfatfs.PyFatFS import PyFatFS

        try:
            probe = PyFatFS(self._path, read_only=True)
            probe.close()
            return True
        except Exception:  # noqa: BLE001 — 비-FAT/손상 → 재포맷 대상
            return False

    def _fat_type(self):
        from pyfatfs.PyFat import PyFat

        if self._size_bytes > self._FAT32_THRESHOLD:
            return PyFat.FAT_TYPE_FAT32
        return PyFat.FAT_TYPE_FAT16

    def _format(self) -> None:
        """이미지 파일을 size_bytes로 사전할당하고 FAT로 포맷한다."""
        from pyfatfs.PyFat import PyFat

        parent = os.path.dirname(self._path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # mkfs는 기존 파일을 rb+로 열므로 크기만큼 미리 만들어 둔다.
        with open(self._path, "wb") as f:
            f.truncate(self._size_bytes)
        pf = PyFat()
        try:
            pf.mkfs(self._path, fat_type=self._fat_type(), size=self._size_bytes)
        finally:
            pf.close()
        logger.info(
            "LoopbackSource '%s' FAT image formatted: %s (%d bytes)",
            self._source_id, self._path, self._size_bytes,
        )

    def _mount(self) -> None:
        from pyfatfs.PyFatFS import PyFatFS

        self._fs = PyFatFS(self._path, read_only=self._read_only)
        self._used_bytes = self._scan_used_space()
        self._active = True
        logger.info(
            "LoopbackSource '%s' mounted (FAT%s, used %d bytes): %s",
            self._source_id, "" if self._read_only else " rw",
            self._used_bytes, self._path,
        )

    def close(self) -> None:
        """FAT 이미지 핸들을 닫는다(언마운트)."""
        if self._fs is not None:
            try:
                self._fs.close()
            except Exception:  # noqa: BLE001
                pass
            self._fs = None
        self._active = False

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _check_active(self) -> None:
        if not self._active or self._fs is None:
            raise OSError(
                f"Storage source '{self._source_id}' is not active"
            )

    @staticmethod
    def _ipath(physical_path: str) -> str:
        """소스 루트 상대 경로를 이미지 내부 절대(POSIX) 경로로 변환한다."""
        return "/" + physical_path.replace("\\", "/").lstrip("/")

    def _ensure_parent(self, ipath: str) -> None:
        parent = ipath.rsplit("/", 1)[0]
        if parent and parent != "":
            try:
                self._fs.makedirs(parent, recreate=True)
            except Exception:  # noqa: BLE001 — 이미 존재 등
                pass

    def _size_of(self, ipath: str) -> int:
        try:
            return self._fs.getsize(ipath)
        except Exception:  # noqa: BLE001
            return 0

    def _scan_used_space(self) -> int:
        total = 0
        try:
            for p in self._fs.walk.files():
                total += self._size_of(p)
        except Exception:  # noqa: BLE001
            pass
        return total

    def _insufficient(self, need: int) -> OSError:
        avail = self.get_available_space()
        return OSError(
            f"LoopbackSource '{self._source_id}' insufficient space: "
            f"need {need}, available {avail}"
        )

    def _safe_remove(self, ipath: str) -> None:
        try:
            self._fs.remove(ipath)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # StorageSource 인터페이스
    # ------------------------------------------------------------------

    def read(self, physical_path: str) -> bytes:
        self._check_active()
        from fs.errors import ResourceNotFound

        ipath = self._ipath(physical_path)
        try:
            with self._fs.open(ipath, "rb") as f:
                return f.read()
        except ResourceNotFound:
            raise FileNotFoundError(physical_path) from None

    def write(self, physical_path: str, data: bytes) -> None:
        self._check_active()
        from pyfatfs._exceptions import PyFATException

        ipath = self._ipath(physical_path)
        existing = self._size_of(ipath) if self._fs.exists(ipath) else 0
        need = len(data) - existing
        if need > 0 and need > self.get_available_space():
            raise self._insufficient(need)
        self._ensure_parent(ipath)
        try:
            with self._fs.open(ipath, "wb") as f:
                f.write(data)
        except PyFATException as e:
            self._safe_remove(ipath)
            raise self._insufficient(len(data)) from e
        self._used_bytes += self._size_of(ipath) - existing

    def write_chunk(
        self, physical_path: str, data: bytes, offset: int, total_size: int
    ) -> None:
        """청크 기록. offset=0은 total_size 용량 검사 후 새로 생성, 이후는 seek 기록."""
        self._check_active()
        from pyfatfs._exceptions import PyFATException

        ipath = self._ipath(physical_path)
        if offset == 0:
            existing = self._size_of(ipath) if self._fs.exists(ipath) else 0
            need = total_size - existing
            if need > 0 and need > self.get_available_space():
                raise self._insufficient(need)
            self._ensure_parent(ipath)
            try:
                with self._fs.open(ipath, "wb") as f:
                    f.write(data)
            except PyFATException as e:
                self._safe_remove(ipath)
                raise self._insufficient(total_size) from e
            self._used_bytes += self._size_of(ipath) - existing
        else:
            before = self._size_of(ipath)
            try:
                with self._fs.open(ipath, "r+b") as f:
                    f.seek(offset)
                    f.write(data)
            except PyFATException as e:
                raise self._insufficient(offset + len(data)) from e
            self._used_bytes += self._size_of(ipath) - before

    def read_chunk(self, physical_path: str, offset: int, length: int) -> bytes:
        self._check_active()
        from fs.errors import ResourceNotFound

        ipath = self._ipath(physical_path)
        try:
            with self._fs.open(ipath, "rb") as f:
                f.seek(offset)
                return f.read(length)
        except ResourceNotFound:
            raise FileNotFoundError(physical_path) from None

    def delete(self, physical_path: str) -> None:
        self._check_active()
        from fs.errors import ResourceNotFound

        ipath = self._ipath(physical_path)
        try:
            sz = self._size_of(ipath)
            self._fs.remove(ipath)
            self._used_bytes = max(0, self._used_bytes - sz)
        except ResourceNotFound:
            pass

    def exists(self, physical_path: str) -> bool:
        self._check_active()
        return self._fs.exists(self._ipath(physical_path))

    def mkdir(self, physical_path: str) -> None:
        self._check_active()
        self._fs.makedirs(self._ipath(physical_path), recreate=True)

    def rmdir(self, physical_path: str) -> None:
        self._check_active()
        from fs.errors import ResourceNotFound

        ipath = self._ipath(physical_path)
        try:
            self._fs.removetree(ipath)
        except ResourceNotFound:
            pass

    def list_dir(self, physical_path: str) -> list[str]:
        self._check_active()
        from fs.errors import DirectoryExpected, ResourceNotFound

        ipath = self._ipath(physical_path)
        try:
            return list(self._fs.listdir(ipath))
        except (ResourceNotFound, DirectoryExpected):
            return []

    def list_physical_files(self) -> list[str]:
        """이미지 내 물리 파일 상대 경로 목록을 반환한다 (orphan GC용).

        청크는 샤드 서브디렉토리(`<hh>/<chunk_ref>`)에 저장되므로 이미지 루트 직속뿐
        아니라 하위 디렉토리까지 재귀로 훑는다(`walk.files()`). 반환 경로는 이미지
        루트 기준 상대 경로다(선행 슬래시 없음).
        """
        self._check_active()
        try:
            return [p.lstrip("/") for p in self._fs.walk.files()]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "list_physical_files 실패 (%s): %s", self._source_id, e
            )
            return []

    def get_available_space(self) -> int:
        """가용 공간 ≈ size_bytes - 내부 파일 크기 합. 실제 한정은 쓰기 시 집행."""
        self._check_active()
        return max(0, self._size_bytes - self._used_bytes)

    def get_total_space(self) -> int:
        """전체 공간 = 이미지 크기."""
        self._check_active()
        return self._size_bytes
