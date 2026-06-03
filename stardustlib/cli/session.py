"""CLI 세션: 설정으로 코어 컴포넌트를 조립하고 명령에 노출한다.

Phase 1: 오프라인(로컬 코어) 빌드만 수행한다. read_file/write_file의 원격 경로는
remote_source의 전용 이벤트 루프로 자가 브리지되므로 CLI는 이벤트 루프를 소유하지
않는다. 서버 인증/remote 마운트/메타데이터 동기화(온라인 setup)는 Phase 2에서
추가한다.
"""

from __future__ import annotations

import logging

from stardustlib.config_loader import ConfigLoader

logger = logging.getLogger(__name__)


class CLISession:
    """단발 CLI 명령이 사용하는 코어 컴포넌트 묶음."""

    def __init__(self, jbod_manager, metadata_store) -> None:
        self.jbod = jbod_manager
        self.metadata = metadata_store

    @classmethod
    def open(cls, config_path: str) -> "CLISession":
        """설정을 로드하고 로컬 코어를 조립한다.

        실패 시 _build_core가 sys.exit(1)을 호출한다.
        """
        # 순환 import 방지를 위해 함수 내부에서 import (stardustfs가 cli를 호출)
        from stardustfs import _build_core

        config = ConfigLoader(config_path).load()
        jbod_manager, metadata_store, _enc, _db_key = _build_core(config)
        return cls(jbod_manager, metadata_store)

    def close(self) -> None:
        """리소스를 정리한다."""
        try:
            self.metadata.close()
        except Exception as e:  # noqa: BLE001 — 종료 경로, 로깅만
            logger.debug("metadata_store 종료 중 예외: %s", e)
