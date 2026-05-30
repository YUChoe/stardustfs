"""StardustFS 예외 클래스 정의."""


class StardustError(Exception):
    """StardustFS 모든 예외의 기본 클래스."""


class InsufficientStorageError(StardustError):
    """모든 활성 소스의 여유 공간이 부족할 때 발생."""


class DecryptionError(StardustError):
    """잘못된 키로 복호화 시도 시 발생."""


class IntegrityError(StardustError):
    """암호화된 파일의 인증 태그 검증 실패 시 발생 (변조/손상)."""


class KeyNotFoundError(StardustError):
    """키 파일과 환경변수 모두에서 키를 찾을 수 없을 때 발생."""


class InvalidKeyError(StardustError):
    """키 길이가 32바이트가 아닐 때 발생."""


# MVP2 추가 예외

class AuthenticationError(StardustError):
    """인증 실패 (잘못된 자격 증명, 토큰 만료 등)."""


class SyncError(StardustError):
    """메타데이터 동기화 실패."""


class DeviceRegistrationError(StardustError):
    """디바이스 등록 실패."""


class P2PConnectionError(StardustError):
    """P2P 연결 실패."""


class ConfigMigrationError(StardustError):
    """설정 파일 마이그레이션 실패."""
