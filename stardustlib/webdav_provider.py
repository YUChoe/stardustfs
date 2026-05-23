"""wsgidav 기반 WebDAV 프로바이더.

StardustFS의 JBOD Manager를 wsgidav DAVProvider 인터페이스로 래핑하여
WebDAV 클라이언트에 암호화된 가상 파일시스템을 제공한다.
"""

import logging
import mimetypes
from io import BytesIO
from typing import IO

from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.dav_error import DAVError

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import InsufficientStorageError
from stardustlib.jbod_manager import JBODManager

logger = logging.getLogger(__name__)

# HTTP 상태 코드 상수
HTTP_NOT_FOUND = 404
HTTP_UNAUTHORIZED = 401
HTTP_INSUFFICIENT_STORAGE = 507
HTTP_INTERNAL_ERROR = 500


class _WriteBuffer(BytesIO):
    """쓰기 완료 시 JBODManager에 데이터를 저장하는 BytesIO 래퍼."""

    def __init__(self, jbod_manager: JBODManager, virtual_path: str) -> None:
        super().__init__()
        self._jbod_manager = jbod_manager
        self._virtual_path = virtual_path

    def close(self) -> None:
        """버퍼를 닫으면서 JBODManager에 데이터를 기록한다."""
        if not self.closed:
            data = self.getvalue()
            try:
                self._jbod_manager.write_file(self._virtual_path, data)
            except Exception:
                logger.error(
                    "파일 쓰기 실패: %s", self._virtual_path, exc_info=True
                )
                raise
            finally:
                super().close()



class StardustDAVProvider(DAVProvider):
    """wsgidav DAVProvider를 확장한 커스텀 프로바이더.

    JBODManager를 통해 모든 파일/디렉토리 작업을 수행한다.
    """

    def __init__(
        self,
        jbod_manager: JBODManager,
        encryption_engine: EncryptionEngine,
    ) -> None:
        """StardustDAVProvider 초기화.

        Args:
            jbod_manager: JBOD 스토리지 통합 관리자.
            encryption_engine: 암호화 엔진.
        """
        super().__init__()
        self.jbod_manager = jbod_manager
        self.encryption_engine = encryption_engine

    def get_resource_inst(self, path: str, environ: dict):
        """가상 경로에 해당하는 DAV 리소스 인스턴스를 반환한다.

        Args:
            path: 가상 경로 (예: "/", "/docs/file.txt").
            environ: WSGI 환경 딕셔너리.

        Returns:
            DAVResource 인스턴스 또는 None (리소스 미존재 시).
        """
        # 경로 정규화
        path = path.rstrip("/") or "/"

        # 루트 경로는 항상 디렉토리
        if path == "/":
            return StardustDirectoryResource(
                path, environ, self.jbod_manager
            )

        # 파일 존재 여부 확인
        file_info = self.jbod_manager.get_file_info(path)
        if file_info is not None:
            return StardustFileResource(
                path, environ, self.jbod_manager, file_info
            )

        # 디렉토리 존재 여부 확인 (하위 엔트리가 있으면 디렉토리)
        entries = self.jbod_manager.list_directory(path)
        if entries:
            return StardustDirectoryResource(
                path, environ, self.jbod_manager
            )

        # 메타데이터에 디렉토리로 등록되어 있는지 확인
        dir_path = path.rstrip("/")
        if self.jbod_manager.metadata_store.lookup_directory(dir_path):
            return StardustDirectoryResource(
                path, environ, self.jbod_manager
            )

        return None


class StardustFileResource(DAVNonCollection):
    """개별 파일 리소스.

    JBODManager를 통해 파일 읽기/쓰기/삭제/이동을 수행한다.
    """

    def __init__(
        self,
        path: str,
        environ: dict,
        jbod_manager: JBODManager,
        file_info,
    ) -> None:
        """StardustFileResource 초기화.

        Args:
            path: 가상 파일 경로.
            environ: WSGI 환경 딕셔너리.
            jbod_manager: JBOD 스토리지 관리자.
            file_info: FileInfo 객체.
        """
        super().__init__(path, environ)
        self._jbod_manager = jbod_manager
        self._file_info = file_info

    def get_content_length(self) -> int:
        """파일 크기를 반환한다."""
        return self._file_info.file_size

    def get_content_type(self) -> str:
        """MIME 타입을 반환한다."""
        content_type, _ = mimetypes.guess_type(self.path)
        return content_type or "application/octet-stream"

    def get_creation_date(self) -> float:
        """생성 시간을 반환한다."""
        return self._file_info.created_at

    def get_last_modified(self) -> float:
        """수정 시간을 반환한다."""
        return self._file_info.modified_at

    def support_etag(self) -> bool:
        """ETag 미지원."""
        return False

    def get_etag(self) -> str | None:
        """ETag를 반환한다. 미지원이므로 None."""
        return None

    def support_ranges(self) -> bool:
        """Range 요청 미지원."""
        return False

    def get_content(self) -> IO[bytes]:
        """파일 내용을 읽어 BytesIO 스트림으로 반환한다.

        Raises:
            DAVError: 파일 읽기 실패 시.
        """
        try:
            data = self._jbod_manager.read_file(self.path)
            return BytesIO(data)
        except FileNotFoundError:
            raise DAVError(HTTP_NOT_FOUND)
        except Exception as e:
            logger.error("파일 읽기 실패: %s - %s", self.path, e)
            raise DAVError(HTTP_INTERNAL_ERROR)

    def begin_write(self, *, content_type=None) -> IO[bytes]:
        """쓰기용 스트림을 반환한다.

        close() 호출 시 JBODManager에 데이터가 저장된다.

        Args:
            content_type: 콘텐츠 타입 (미사용).

        Returns:
            쓰기 가능한 BytesIO 버퍼.

        Raises:
            DAVError: 공간 부족 시 507, 기타 에러 시 500.
        """
        return _WriteBuffer(self._jbod_manager, self.path)

    def end_write(self, *, with_errors) -> None:
        """쓰기 완료 알림. _WriteBuffer.close()에서 처리하므로 별도 작업 없음."""
        pass

    def delete(self) -> None:
        """파일을 삭제한다.

        Raises:
            DAVError: 파일 미존재 시 404, 기타 에러 시 500.
        """
        try:
            self._jbod_manager.delete_file(self.path)
        except FileNotFoundError:
            raise DAVError(HTTP_NOT_FOUND)
        except Exception as e:
            logger.error("파일 삭제 실패: %s - %s", self.path, e)
            raise DAVError(HTTP_INTERNAL_ERROR)

    def copy_move_single(self, dest_path: str, *, is_move: bool) -> None:
        """파일을 복사 또는 이동한다.

        Args:
            dest_path: 대상 가상 경로.
            is_move: True이면 이동, False이면 복사.

        Raises:
            DAVError: 원본 미존재 시 404, 공간 부족 시 507, 기타 에러 시 500.
        """
        try:
            if is_move:
                self._jbod_manager.move_file(self.path, dest_path)
            else:
                self._jbod_manager.copy_file(self.path, dest_path)
        except FileNotFoundError:
            raise DAVError(HTTP_NOT_FOUND)
        except InsufficientStorageError:
            raise DAVError(HTTP_INSUFFICIENT_STORAGE)
        except Exception as e:
            logger.error(
                "파일 %s 실패: %s -> %s - %s",
                "이동" if is_move else "복사",
                self.path,
                dest_path,
                e,
            )
            raise DAVError(HTTP_INTERNAL_ERROR)


class StardustDirectoryResource(DAVCollection):
    """디렉토리 리소스.

    JBODManager를 통해 디렉토리 목록 조회, 생성, 삭제를 수행한다.
    """

    def __init__(
        self,
        path: str,
        environ: dict,
        jbod_manager: JBODManager,
    ) -> None:
        """StardustDirectoryResource 초기화.

        Args:
            path: 가상 디렉토리 경로.
            environ: WSGI 환경 딕셔너리.
            jbod_manager: JBOD 스토리지 관리자.
        """
        super().__init__(path, environ)
        self._jbod_manager = jbod_manager

    def get_member_names(self) -> list[str]:
        """디렉토리 내 엔트리 이름 목록을 반환한다.

        Returns:
            하위 파일/디렉토리 이름 목록.
        """
        try:
            entries = self._jbod_manager.list_directory(self.path)
            return [entry.name for entry in entries]
        except Exception as e:
            logger.error("디렉토리 목록 조회 실패: %s - %s", self.path, e)
            raise DAVError(HTTP_INTERNAL_ERROR)

    def get_member(self, name: str):
        """이름으로 하위 리소스를 반환한다.

        Args:
            name: 하위 엔트리 이름.

        Returns:
            DAVResource 인스턴스 또는 None.
        """
        from wsgidav.util import join_uri

        child_path = join_uri(self.path, name)

        # 파일인지 확인
        file_info = self._jbod_manager.get_file_info(child_path)
        if file_info is not None:
            return StardustFileResource(
                child_path, self.environ, self._jbod_manager, file_info
            )

        # 디렉토리인지 확인
        entries = self._jbod_manager.list_directory(child_path)
        if entries:
            return StardustDirectoryResource(
                child_path, self.environ, self._jbod_manager
            )

        # 메타데이터에 디렉토리로 등록되어 있는지 확인
        dir_path = child_path.rstrip("/")
        if self._jbod_manager.metadata_store.lookup_directory(dir_path):
            return StardustDirectoryResource(
                child_path, self.environ, self._jbod_manager
            )

        return None

    def create_empty_resource(self, name: str):
        """빈 파일 리소스를 생성한다.

        Args:
            name: 생성할 파일 이름.

        Returns:
            생성된 StardustFileResource.

        Raises:
            DAVError: 공간 부족 시 507, 기타 에러 시 500.
        """
        from wsgidav.util import join_uri

        child_path = join_uri(self.path, name)
        try:
            self._jbod_manager.write_file(child_path, b"")
        except InsufficientStorageError:
            raise DAVError(HTTP_INSUFFICIENT_STORAGE)
        except Exception as e:
            logger.error("빈 파일 생성 실패: %s - %s", child_path, e)
            raise DAVError(HTTP_INTERNAL_ERROR)

        file_info = self._jbod_manager.get_file_info(child_path)
        return StardustFileResource(
            child_path, self.environ, self._jbod_manager, file_info
        )

    def create_collection(self, name: str):
        """하위 디렉토리를 생성한다.

        Args:
            name: 생성할 디렉토리 이름.

        Returns:
            생성된 StardustDirectoryResource.

        Raises:
            DAVError: 기타 에러 시 500.
        """
        from wsgidav.util import join_uri

        child_path = join_uri(self.path, name)
        try:
            self._jbod_manager.create_directory(child_path)
        except Exception as e:
            logger.error("디렉토리 생성 실패: %s - %s", child_path, e)
            raise DAVError(HTTP_INTERNAL_ERROR)

        return StardustDirectoryResource(
            child_path, self.environ, self._jbod_manager
        )

    def delete(self) -> None:
        """디렉토리를 재귀적으로 삭제한다.

        Raises:
            DAVError: 기타 에러 시 500.
        """
        try:
            self._jbod_manager.delete_directory(self.path)
        except FileNotFoundError:
            raise DAVError(HTTP_NOT_FOUND)
        except Exception as e:
            logger.error("디렉토리 삭제 실패: %s - %s", self.path, e)
            raise DAVError(HTTP_INTERNAL_ERROR)


def create_webdav_app(
    config: "StardustConfig",
    jbod_manager: JBODManager,
    encryption_engine: EncryptionEngine,
):
    """wsgidav WSGI 앱을 생성하고 인증을 설정한다.

    Args:
        config: StardustFS 설정 (webdav.username/password 포함).
        jbod_manager: JBOD 스토리지 관리자.
        encryption_engine: 암호화 엔진.

    Returns:
        WSGI 앱 인스턴스.
    """
    from wsgidav.wsgidav_app import WsgiDAVApp

    provider = StardustDAVProvider(jbod_manager, encryption_engine)

    webdav_cfg = config["webdav"]
    username = webdav_cfg["username"]
    password = webdav_cfg["password"]

    wsgidav_config = {
        "provider_mapping": {"/": provider},
        "verbose": 0,
        "logging": {
            "enable": False,
        },
        "simple_dc": {
            "user_mapping": {
                "*": {
                    username: {
                        "password": password,
                    },
                },
            },
        },
        "http_authenticator": {
            "domain_controller": None,  # simple_dc 사용
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
    }

    app = WsgiDAVApp(wsgidav_config)
    return app
