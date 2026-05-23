"""StardustFS 예외 클래스 정의."""


class InsufficientStorageError(Exception):
    """모든 활성 소스의 여유 공간이 부족할 때 발생."""


class DecryptionError(Exception):
    """잘못된 키로 복호화 시도 시 발생."""


class IntegrityError(Exception):
    """암호화된 파일의 인증 태그 검증 실패 시 발생 (변조/손상)."""


class KeyNotFoundError(Exception):
    """키 파일과 환경변수 모두에서 키를 찾을 수 없을 때 발생."""


class InvalidKeyError(Exception):
    """키 길이가 32바이트가 아닐 때 발생."""
