"""StardustFS 데이터 모델 정의.

설정 스키마(TypedDict)와 런타임 데이터 모델(dataclass)을 정의한다.
"""

from dataclasses import dataclass
from typing import Literal, TypeAlias, TypedDict


# --- 설정 스키마 (Configuration Schema) ---


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
    """StardustFS 전체 설정 (v1)."""

    version: int                    # 설정 파일 버전 (현재 1)
    sources: list[SourceConfig]     # 스토리지 소스 목록
    metadata_db: str                # SQLite DB 파일 경로
    key_file: str | None            # 암호화 키 파일 경로 (선택)


# --- v2 설정 스키마 ---


class ServerConfig(TypedDict):
    """중앙 서버 연결 설정."""

    url: str | None     # https:// URL 또는 None (오프라인 전용)
    device_name: str    # 디바이스 이름 (1-64자)


class SyncConfig(TypedDict):
    """메타데이터 동기화 설정."""

    interval_seconds: int                   # 동기화 간격 (10-3600초)
    conflict_strategy: Literal["copy"]      # 충돌 해결 전략


class P2PConfig(TypedDict):
    """P2P 서버 설정."""

    port: int       # P2P 포트 (1024-65535)
    enabled: bool   # P2P 활성화 여부
    auto_mount_devices: bool  # 내 다른 디바이스를 자동으로 remote 소스로 마운트 (기본 True)


class RemoteSourceConfig(TypedDict):
    """원격 디바이스 소스 설정."""

    type: Literal["remote"]
    id: str             # 소스 고유 ID
    device_id: str      # RFC 4122 UUID (8-4-4-4-12)


SourceConfigV2: TypeAlias = (
    DirectorySourceConfig | LoopbackSourceConfig | RemoteSourceConfig
)


class StardustConfigV2(TypedDict):
    """StardustFS v2 전체 설정."""

    version: Literal[2]
    server: ServerConfig
    sources: list[SourceConfigV2]
    sync: SyncConfig
    p2p: P2PConfig
    metadata_db: str
    key_file: str | None


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
    version: int = 1        # 메타데이터 버전 (동기화용)
    device_id: str | None = None    # 마지막 수정 디바이스 ID
    sync_status: str = "synced"     # synced | pending | conflict
    deleted: bool = False   # tombstone 여부 (삭제 동기화용)
    evicted: bool = False   # 로컬 원본 축출(복제본 전용) 여부 — 읽기는 복제 복구 (로컬 전용)
    replication_status: str = "none"  # none|pending|replicated (소유자가 설정, 동기화 전파)


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
