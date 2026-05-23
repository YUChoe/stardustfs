"""StardustFS 데이터 모델 정의.

설정 스키마(TypedDict)와 런타임 데이터 모델(dataclass)을 정의한다.
"""

from dataclasses import dataclass
from typing import Literal, TypedDict


# --- 설정 스키마 (Configuration Schema) ---


class WebDAVConfig(TypedDict):
    """WebDAV 서비스 설정."""

    host: str       # 바인드 호스트 (고정: "127.0.0.1")
    port: int       # 바인드 포트 (기본: 8080)
    username: str   # Basic Auth 사용자명
    password: str   # Basic Auth 비밀번호


class DirectorySourceConfig(TypedDict):
    """디렉토리 소스 설정."""

    type: Literal["directory"]
    id: str         # 소스 고유 ID
    path: str       # 절대 경로


class LoopbackSourceConfig(TypedDict):
    """루프백 소스 설정."""

    type: Literal["loopback"]
    id: str         # 소스 고유 ID
    path: str       # 루프백 파일 절대 경로
    size: int       # 파일 크기 (바이트, 10MB ~ 2TB)


SourceConfig = DirectorySourceConfig | LoopbackSourceConfig


class StardustConfig(TypedDict):
    """StardustFS 전체 설정."""

    version: int                    # 설정 파일 버전 (현재 1)
    webdav: WebDAVConfig            # WebDAV 서비스 설정
    sources: list[SourceConfig]     # 스토리지 소스 목록
    metadata_db: str                # SQLite DB 파일 경로
    key_file: str | None            # 암호화 키 파일 경로 (선택)


# --- 런타임 데이터 모델 ---


@dataclass
class FileMetadata:
    """파일 메타데이터. Metadata Store에서 조회된 파일 정보."""

    virtual_path: str       # 최대 4096자
    source_id: str          # Storage Source ID
    physical_path: str      # 최대 4096자
    file_size: int          # 0 이상, 바이트 단위
    created_at: float       # UTC 타임스탬프
    modified_at: float      # UTC 타임스탬프


@dataclass
class EntryInfo:
    """디렉토리 엔트리 정보. 디렉토리 목록 조회 결과."""

    name: str               # 파일/디렉토리 이름
    is_directory: bool      # 디렉토리 여부
    file_size: int          # 파일 크기 (디렉토리는 0)
    created_at: float       # 생성 시간
    modified_at: float      # 수정 시간


@dataclass
class FileInfo:
    """파일 상세 정보. JBOD Manager에서 반환하는 파일 정보."""

    virtual_path: str
    source_id: str
    file_size: int
    created_at: float
    modified_at: float
    is_directory: bool


@dataclass
class EncryptedFileHeader:
    """암호화된 파일 헤더. 파일 앞부분에 저장되는 메타 정보."""

    magic: bytes            # b'SDFS' (4바이트)
    version: int            # 1 (1바이트)
    mode_id: int            # 1=GCM (1바이트)
    iv: bytes               # 128비트 초기화 벡터 (16바이트)
    tag: bytes              # GCM 인증 태그 (16바이트)
