"""시스템 초기화 로직.

설정 로드 → 스토리지 검증 → DB 초기화 → 키 검증 → WebDAV 시작 순서로
시스템을 초기화하고, 모든 검증 실패 원인을 수집하여 일괄 로그 기록 후
0이 아닌 종료 코드로 중단한다.
"""

import json
import logging
import os
import sys

from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from stardustlib.config_loader import ConfigLoader
from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import InvalidKeyError, KeyNotFoundError
from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import SourceConfig, StardustConfig
from stardustlib.storage_source import DirectorySource, LoopbackSource, StorageSource

logger = logging.getLogger(__name__)


def _derive_db_key(master_key: bytes) -> bytes:
    """HKDF로 마스터 키에서 DB 전용 키를 파생한다.

    Args:
        master_key: 32바이트 마스터 암호화 키.

    Returns:
        32바이트 DB 전용 암호화 키.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"stardustfs-metadata-db",
        info=b"db-encryption-key",
    )
    return hkdf.derive(master_key)


def _create_sources(sources_config: list[SourceConfig]) -> list[StorageSource]:
    """설정에서 StorageSource 인스턴스 목록을 생성하고 초기화한다.

    Args:
        sources_config: 소스 설정 목록.

    Returns:
        초기화된 StorageSource 인스턴스 목록.
    """
    sources: list[StorageSource] = []
    for cfg in sources_config:
        source_type = cfg["type"]
        if source_type == "directory":
            source = DirectorySource(cfg["id"], cfg["path"])
        elif source_type == "loopback":
            source = LoopbackSource(cfg["id"], cfg["path"], cfg["size"])
        else:
            logger.warning("알 수 없는 소스 유형: %s", source_type)
            continue
        source.initialize()
        sources.append(source)
    return sources


def initialize_system(config_path: str) -> tuple:
    """시스템을 초기화하고 WSGI 앱과 설정을 반환한다.

    순서: 설정 로드 → 설정 검증 → 키 로드 → 스토리지 검증 → DB 초기화 → WebDAV 조립

    Args:
        config_path: JSON 설정 파일 경로.

    Returns:
        (app, config) 튜플. 검증 실패 시 sys.exit(1) 호출.
    """
    # 지연 임포트: create_webdav_app은 webdav_provider에 정의
    from stardustlib.webdav_provider import create_webdav_app

    errors: list[str] = []

    # Phase 1: 설정 로드
    try:
        loader = ConfigLoader(config_path)
        config: StardustConfig = loader.load()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error("설정 로드 실패: %s", e)
        sys.exit(1)

    # Phase 2: 설정 검증
    validation_errors = loader.validate(config)
    errors.extend(validation_errors)

    # Phase 3: 암호화 키 로드 및 검증
    key: bytes | None = None
    try:
        key = ConfigLoader.load_encryption_key(config.get("key_file"))
    except (KeyNotFoundError, InvalidKeyError) as e:
        errors.append(f"Encryption_Key 로드 실패: {e}")

    if key is not None and len(key) != 32:
        errors.append(
            f"Encryption_Key가 32바이트가 아님 (현재: {len(key)}바이트)"
        )

    # Phase 4: 스토리지 소스 검증
    for i, source_cfg in enumerate(config.get("sources", [])):
        source_type = source_cfg.get("type")
        path = source_cfg.get("path", "")

        if source_type == "directory":
            if not os.path.isdir(path):
                errors.append(
                    f"sources[{i}] Directory Source 경로 미존재: {path}"
                )
            elif not os.access(path, os.R_OK | os.W_OK):
                errors.append(
                    f"sources[{i}] Directory Source 권한 부족: {path}"
                )
        elif source_type == "loopback":
            if not os.path.isfile(path):
                parent = os.path.dirname(path)
                if parent and not os.access(parent, os.W_OK):
                    errors.append(
                        f"sources[{i}] Loopback Source 생성 불가: {path}"
                    )

    # Phase 5: Metadata Store 초기화 (SQLCipher 암호화, HKDF 파생 키)
    metadata_store: MetadataStore | None = None
    if key is not None and len(key) == 32:
        try:
            db_key = _derive_db_key(key)
            metadata_store = MetadataStore(config["metadata_db"], db_key)
            metadata_store.initialize()
        except Exception as e:
            errors.append(f"Metadata Store 초기화 실패: {e}")

    # 모든 검증 결과 확인
    if errors:
        for err in errors:
            logger.error(err)
        sys.exit(1)

    # Phase 6: 컴포넌트 조립
    assert key is not None  # Phase 3에서 검증 완료
    encryption_engine = EncryptionEngine(key)
    sources = _create_sources(config["sources"])
    assert metadata_store is not None  # Phase 5에서 검증 완료
    jbod_manager = JBODManager(sources, metadata_store, encryption_engine)
    app = create_webdav_app(config, jbod_manager, encryption_engine)

    logger.info("StardustFS 준비 완료")
    return app, config
