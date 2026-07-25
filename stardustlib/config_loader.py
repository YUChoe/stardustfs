"""설정 파일 로드 및 검증.

JSON 설정 파일을 파싱하고, 검증 규칙을 적용하여 StardustConfig를 반환한다.
암호화 키는 별도 키 파일 또는 환경변수에서 로드한다.
"""

import json
import logging
import os
import re
import shutil
from pathlib import Path

from stardustlib.exceptions import (
    ConfigMigrationError,
    InvalidKeyError,
    KeyNotFoundError,
)
from stardustlib.models import StardustConfig

logger = logging.getLogger(__name__)

# 검증 상수
MIN_LOOPBACK_SIZE = 10_485_760          # 10MB
MAX_LOOPBACK_SIZE = 2_199_023_255_552   # 2TB
MIN_PORT = 1
MAX_PORT = 65535
SUPPORTED_VERSIONS = {1, 2}
REQUIRED_KEY_LENGTH = 32

# v2 검증 상수
MIN_P2P_PORT = 1024
MAX_P2P_PORT = 65535
MIN_SYNC_INTERVAL = 10
MAX_SYNC_INTERVAL = 3600
MIN_DEVICE_NAME_LEN = 1
MAX_DEVICE_NAME_LEN = 64

# RFC 4122 UUID 패턴 (8-4-4-4-12)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ConfigLoader:
    """JSON 설정 파일 로드 및 검증."""

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path

    def load(self) -> StardustConfig:
        """설정 파일을 파싱하여 StardustConfig를 반환한다.

        - JSON 파싱 실패 시 예외 발생
        """
        path = Path(self._config_path)
        if not path.exists():
            raise FileNotFoundError(
                f"설정 파일을 찾을 수 없습니다: {self._config_path}"
            )

        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"설정 파일 JSON 파싱 실패: {e.msg}",
                e.doc,
                e.pos,
            ) from e

        config: StardustConfig = data
        return config

    def validate(self, config: StardustConfig) -> list[str]:
        """설정을 검증하고 에러 목록을 반환한다.

        반환값이 빈 리스트이면 검증 통과.
        """
        errors: list[str] = []

        # version 검증
        version = config.get("version")  # type: ignore[attr-defined]
        if not isinstance(version, int):
            errors.append("version: 정수여야 합니다")
        elif version not in SUPPORTED_VERSIONS:
            errors.append(
                f"version: 지원되지 않는 버전입니다 (1 또는 2만 지원)"
            )

        # version에 따라 분기
        if isinstance(version, int) and version == 2:
            errors.extend(self._validate_v2(config))
        elif isinstance(version, int) and version == 1:
            errors.extend(self._validate_v1(config))

        return errors

    def _validate_v1(self, config: StardustConfig) -> list[str]:
        """v1 설정 검증 (기존 로직)."""
        errors: list[str] = []

        # sources 검증
        sources = config.get("sources")  # type: ignore[attr-defined]
        if not isinstance(sources, list) or len(sources) == 0:
            errors.append("sources: 최소 1개의 스토리지 소스가 필요합니다")
        else:
            for i, source in enumerate(sources):
                errors.extend(self._validate_source(i, source))

        # key_file 검증
        key_file = config.get("key_file")  # type: ignore[attr-defined]
        if key_file is not None and isinstance(key_file, str):
            if not Path(key_file).exists():
                errors.append(
                    f"key_file: 파일이 존재하지 않습니다: {key_file}"
                )

        return errors

    def _validate_v2(self, config: StardustConfig) -> list[str]:
        """v2 설정 검증."""
        errors: list[str] = []

        # 필수 섹션 존재 확인
        data: dict = config  # type: ignore[assignment]
        for section in ("server", "sync", "p2p"):
            if section not in data or not isinstance(data.get(section), dict):
                errors.append(
                    f"{section}: 필수 섹션이 누락되었습니다"
                )

        # server 섹션 검증
        server = data.get("server")
        if isinstance(server, dict):
            errors.extend(self._validate_server(server))

        # sync 섹션 검증
        sync = data.get("sync")
        if isinstance(sync, dict):
            errors.extend(self._validate_sync(sync))

        # p2p 섹션 검증
        p2p = data.get("p2p")
        if isinstance(p2p, dict):
            errors.extend(self._validate_p2p(p2p))

        # sources 검증 (v2는 remote 타입도 허용)
        sources = data.get("sources")
        if not isinstance(sources, list) or len(sources) == 0:
            errors.append("sources: 최소 1개의 스토리지 소스가 필요합니다")
        else:
            for i, source in enumerate(sources):
                errors.extend(self._validate_source_v2(i, source))

        # key_file 검증 (server.url이 있으면 서버에서 복원 가능하므로 존재 체크 생략)
        key_file = data.get("key_file")
        server = data.get("server")
        server_url = server.get("url") if isinstance(server, dict) else None
        if key_file is not None and isinstance(key_file, str):
            if not server_url and not Path(key_file).exists():
                errors.append(
                    f"key_file: 파일이 존재하지 않습니다: {key_file}"
                )

        return errors

    def _validate_server(self, server: dict) -> list[str]:
        """server 섹션 검증."""
        errors: list[str] = []

        # url 검증: None이면 오프라인 전용이므로 허용
        url = server.get("url")
        if url is not None:
            if not isinstance(url, str):
                errors.append("server.url: 문자열이어야 합니다")
            elif not url.startswith("https://"):
                errors.append(
                    "server.url: 'https://' 스킴으로 시작해야 합니다"
                )
            elif len(url) <= len("https://"):
                errors.append(
                    "server.url: 호스트명을 포함해야 합니다"
                )

        # device_name 검증
        device_name = server.get("device_name")
        if not isinstance(device_name, str):
            errors.append("server.device_name: 문자열이어야 합니다")
        elif not (MIN_DEVICE_NAME_LEN <= len(device_name) <= MAX_DEVICE_NAME_LEN):
            errors.append(
                f"server.device_name: {MIN_DEVICE_NAME_LEN}~"
                f"{MAX_DEVICE_NAME_LEN}자 범위여야 합니다"
            )

        return errors

    def _validate_sync(self, sync: dict) -> list[str]:
        """sync 섹션 검증."""
        errors: list[str] = []

        interval = sync.get("interval_seconds")
        if not isinstance(interval, int):
            errors.append("sync.interval_seconds: 정수여야 합니다")
        elif not (MIN_SYNC_INTERVAL <= interval <= MAX_SYNC_INTERVAL):
            errors.append(
                f"sync.interval_seconds: "
                f"{MIN_SYNC_INTERVAL}~{MAX_SYNC_INTERVAL} 범위여야 합니다"
            )

        strategy = sync.get("conflict_strategy")
        if strategy != "copy":
            errors.append(
                "sync.conflict_strategy: 'copy' 값이어야 합니다"
            )

        return errors

    def _validate_p2p(self, p2p: dict) -> list[str]:
        """p2p 섹션 검증."""
        errors: list[str] = []

        port = p2p.get("port")
        if not isinstance(port, int):
            errors.append("p2p.port: 정수여야 합니다")
        elif not (MIN_P2P_PORT <= port <= MAX_P2P_PORT):
            errors.append(
                f"p2p.port: {MIN_P2P_PORT}~{MAX_P2P_PORT} 범위여야 합니다"
            )

        enabled = p2p.get("enabled")
        if not isinstance(enabled, bool):
            errors.append("p2p.enabled: boolean이어야 합니다")

        return errors

    def _validate_source_v2(self, index: int, source: dict) -> list[str]:
        """v2 개별 소스 설정을 검증한다 (remote 타입 포함)."""
        source_type = source.get("type")
        if source_type == "remote":
            return self._validate_remote_source(index, source)
        elif source_type in ("directory", "loopback"):
            return self._validate_source(index, source)
        else:
            return [
                f"sources[{index}].type: "
                f"'directory', 'loopback', 또는 'remote'이어야 합니다"
            ]

    def _validate_remote_source(self, index: int, source: dict) -> list[str]:
        """remote 소스 검증."""
        errors: list[str] = []
        prefix = f"sources[{index}]"

        device_id = source.get("device_id")
        if not isinstance(device_id, str):
            errors.append(f"{prefix}.device_id: 문자열이어야 합니다")
        elif not _UUID_RE.match(device_id):
            errors.append(
                f"{prefix}.device_id: RFC 4122 UUID 형식이어야 합니다"
            )

        return errors

    def _validate_source(self, index: int, source: dict) -> list[str]:
        """개별 소스 설정을 검증한다."""
        errors: list[str] = []
        prefix = f"sources[{index}]"
        source_type = source.get("type")

        if source_type == "directory":
            path = source.get("path", "")
            if not os.path.isabs(path):
                errors.append(f"{prefix}.path: 절대 경로여야 합니다")
            elif not os.path.isdir(path):
                errors.append(
                    f"{prefix}.path: 존재하는 디렉토리가 아닙니다: {path}"
                )
        elif source_type == "loopback":
            path = source.get("path", "")
            if not os.path.isabs(path):
                errors.append(f"{prefix}.path: 절대 경로여야 합니다")

            size = source.get("size")
            if not isinstance(size, int):
                errors.append(f"{prefix}.size: 정수여야 합니다")
            elif not (MIN_LOOPBACK_SIZE <= size <= MAX_LOOPBACK_SIZE):
                errors.append(
                    f"{prefix}.size: {MIN_LOOPBACK_SIZE}~{MAX_LOOPBACK_SIZE} "
                    f"바이트 범위여야 합니다"
                )
        else:
            errors.append(
                f"{prefix}.type: 'directory' 또는 'loopback'이어야 합니다"
            )

        return errors

    def migrate_v1_to_v2(self, config: StardustConfig) -> StardustConfig:
        """v1 설정을 v2로 마이그레이션한다.

        - 원본 파일을 "{원본}.v1.bak" 형식으로 백업
        - 기존 백업 존재 시 순번 부여 (".v1.bak.1", ".v1.bak.2", ...)
        - 기존 필드 보존 + server/sync/p2p 기본값 추가
        - 변환된 설정을 원본 경로에 JSON으로 저장
        - 백업 실패 시 ConfigMigrationError 발생
        """
        config_path = Path(self._config_path)

        # 백업 파일명 결정
        backup_path = self._resolve_backup_path(config_path)

        # 백업 수행
        try:
            shutil.copy2(str(config_path), str(backup_path))
        except OSError as e:
            logger.error(f"설정 파일 백업 실패: {e}")
            raise ConfigMigrationError(
                f"백업 파일 생성 실패: {backup_path}"
            ) from e

        # v2 설정 구성 (레거시 webdav 섹션은 더 이상 사용하지 않으므로 제거)
        data: dict = dict(config)  # type: ignore[arg-type]
        data.pop("webdav", None)
        data["version"] = 2
        data["server"] = {"url": None}
        data["sync"] = {
            "interval_seconds": 30,
            "conflict_strategy": "copy",
        }
        # P2P는 기본 활성이다(내 기기 간 파일 접근·백업 호스팅의 전제). 비활성은
        # 엔터프라이즈 격리 등 예외적인 경우이며 서버 정책으로도 제어된다.
        data["p2p"] = {"port": 9090, "enabled": True}

        # 변환된 설정을 원본 경로에 저장
        try:
            config_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"마이그레이션된 설정 저장 실패: {e}")
            raise ConfigMigrationError(
                f"마이그레이션된 설정 저장 실패: {config_path}"
            ) from e

        logger.info(
            f"v1→v2 마이그레이션 완료: 백업={backup_path}"
        )

        return data  # type: ignore[return-value]

    @staticmethod
    def _resolve_backup_path(config_path: Path) -> Path:
        """고유한 백업 파일 경로를 결정한다.

        기본: "{원본}.v1.bak"
        이미 존재하면: "{원본}.v1.bak.1", ".v1.bak.2", ...
        """
        base_backup = Path(str(config_path) + ".v1.bak")
        if not base_backup.exists():
            return base_backup

        n = 1
        while True:
            numbered = Path(f"{base_backup}.{n}")
            if not numbered.exists():
                return numbered
            n += 1

    @staticmethod
    def load_encryption_key(
        key_file: str | None = None,
        env_var: str = "STARDUST_KEY",
    ) -> bytes:
        """키 파일 또는 환경변수에서 암호화 키를 로드한다.

        우선순위: 키 파일 > 환경변수
        - 둘 다 없으면 KeyNotFoundError 발생
        - 키 길이가 32바이트가 아니면 InvalidKeyError 발생
        """
        key: bytes | None = None

        if key_file is not None:
            path = Path(key_file)
            if path.exists():
                key = path.read_bytes()
            else:
                raise KeyNotFoundError(
                    f"키 파일을 찾을 수 없습니다: {key_file}"
                )
        else:
            env_value = os.environ.get(env_var)
            if env_value is not None:
                key = env_value.encode("utf-8")
            else:
                raise KeyNotFoundError(
                    f"키 파일이 지정되지 않았고 환경변수 '{env_var}'도 "
                    f"설정되지 않았습니다"
                )

        if len(key) != REQUIRED_KEY_LENGTH:
            raise InvalidKeyError(
                f"키 길이가 {REQUIRED_KEY_LENGTH}바이트여야 합니다 "
                f"(현재: {len(key)}바이트)"
            )

        return key
