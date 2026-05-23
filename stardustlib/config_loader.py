"""설정 파일 로드 및 검증.

JSON 설정 파일을 파싱하고, 검증 규칙을 적용하여 StardustConfig를 반환한다.
암호화 키는 별도 키 파일 또는 환경변수에서 로드한다.
"""

import json
import logging
import os
from pathlib import Path

from stardustlib.exceptions import InvalidKeyError, KeyNotFoundError
from stardustlib.models import StardustConfig

logger = logging.getLogger(__name__)

# 검증 상수
MIN_LOOPBACK_SIZE = 10_485_760          # 10MB
MAX_LOOPBACK_SIZE = 2_199_023_255_552   # 2TB
MIN_PORT = 1
MAX_PORT = 65535
SUPPORTED_VERSION = 1
REQUIRED_KEY_LENGTH = 32


class ConfigLoader:
    """JSON 설정 파일 로드 및 검증."""

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path

    def load(self) -> StardustConfig:
        """설정 파일을 파싱하여 StardustConfig를 반환한다.

        - JSON 파싱 실패 시 예외 발생
        - webdav.host는 보안상 항상 "127.0.0.1"로 강제
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

        # webdav.host 보안 강제
        if "webdav" in data and isinstance(data["webdav"], dict):
            data["webdav"]["host"] = "127.0.0.1"

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
        elif version != SUPPORTED_VERSION:
            errors.append(
                f"version: 지원되지 않는 버전입니다 (현재 {SUPPORTED_VERSION}만 지원)"
            )

        # sources 검증
        sources = config.get("sources")  # type: ignore[attr-defined]
        if not isinstance(sources, list) or len(sources) == 0:
            errors.append("sources: 최소 1개의 스토리지 소스가 필요합니다")
        else:
            for i, source in enumerate(sources):
                errors.extend(self._validate_source(i, source))

        # webdav 검증
        webdav = config.get("webdav")  # type: ignore[attr-defined]
        if isinstance(webdav, dict):
            port = webdav.get("port")
            if not isinstance(port, int):
                errors.append("webdav.port: 정수여야 합니다")
            elif not (MIN_PORT <= port <= MAX_PORT):
                errors.append(
                    f"webdav.port: {MIN_PORT}~{MAX_PORT} 범위여야 합니다"
                )

        # key_file 검증
        key_file = config.get("key_file")  # type: ignore[attr-defined]
        if key_file is not None and isinstance(key_file, str):
            if not Path(key_file).exists():
                errors.append(
                    f"key_file: 파일이 존재하지 않습니다: {key_file}"
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
