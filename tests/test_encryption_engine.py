"""EncryptionEngine 단위 테스트.

Task 3.1: AES-256-GCM 암호화/복호화 구현 검증.
Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

import io
import os

import pytest

from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import DecryptionError, IntegrityError, InvalidKeyError


@pytest.fixture
def valid_key() -> bytes:
    """유효한 32바이트 키."""
    return os.urandom(32)


@pytest.fixture
def engine(valid_key: bytes) -> EncryptionEngine:
    """EncryptionEngine 인스턴스."""
    return EncryptionEngine(valid_key)


class TestKeyValidation:
    """키 길이 검증 테스트."""

    def test_valid_key_accepted(self, valid_key: bytes) -> None:
        """32바이트 키로 정상 초기화."""
        engine = EncryptionEngine(valid_key)
        assert engine is not None

    def test_short_key_rejected(self) -> None:
        """32바이트 미만 키 거부."""
        with pytest.raises(InvalidKeyError):
            EncryptionEngine(b'\x00' * 16)

    def test_long_key_rejected(self) -> None:
        """32바이트 초과 키 거부."""
        with pytest.raises(InvalidKeyError):
            EncryptionEngine(b'\x00' * 64)

    def test_empty_key_rejected(self) -> None:
        """빈 키 거부."""
        with pytest.raises(InvalidKeyError):
            EncryptionEngine(b'')

    def test_validate_key_static(self) -> None:
        """validate_key 정적 메서드 검증."""
        assert EncryptionEngine.validate_key(os.urandom(32)) is True
        assert EncryptionEngine.validate_key(b'\x00' * 31) is False
        assert EncryptionEngine.validate_key(b'\x00' * 33) is False


class TestEncrypt:
    """암호화 테스트."""

    def test_encrypt_returns_header_plus_ciphertext(self, engine: EncryptionEngine) -> None:
        """암호화 결과 = 헤더(38B) + 암호문."""
        plaintext = b'Hello StardustFS'
        encrypted = engine.encrypt(plaintext)
        assert len(encrypted) == EncryptionEngine.HEADER_SIZE + len(plaintext)

    def test_encrypt_header_magic(self, engine: EncryptionEngine) -> None:
        """헤더 매직 바이트 확인."""
        encrypted = engine.encrypt(b'test')
        assert encrypted[:4] == b'SDFS'

    def test_encrypt_header_version(self, engine: EncryptionEngine) -> None:
        """헤더 버전 확인."""
        encrypted = engine.encrypt(b'test')
        assert encrypted[4] == 1

    def test_encrypt_header_mode(self, engine: EncryptionEngine) -> None:
        """헤더 모드 ID 확인 (GCM=1)."""
        encrypted = engine.encrypt(b'test')
        assert encrypted[5] == 1

    def test_encrypt_empty_data(self, engine: EncryptionEngine) -> None:
        """빈 데이터 암호화."""
        encrypted = engine.encrypt(b'')
        assert len(encrypted) == EncryptionEngine.HEADER_SIZE

    def test_encrypt_produces_unique_iv(self, engine: EncryptionEngine) -> None:
        """동일 데이터 암호화 시 IV가 다름 (Req 4.4)."""
        enc1 = engine.encrypt(b'same data')
        enc2 = engine.encrypt(b'same data')
        iv1 = enc1[6:22]
        iv2 = enc2[6:22]
        assert iv1 != iv2


class TestDecrypt:
    """복호화 테스트."""

    def test_roundtrip(self, engine: EncryptionEngine) -> None:
        """암호화 후 복호화 시 원본 복원 (Req 4.6)."""
        plaintext = b'Round-trip test data'
        encrypted = engine.encrypt(plaintext)
        decrypted = engine.decrypt(encrypted)
        assert decrypted == plaintext

    def test_roundtrip_empty(self, engine: EncryptionEngine) -> None:
        """빈 데이터 round-trip."""
        encrypted = engine.encrypt(b'')
        assert engine.decrypt(encrypted) == b''

    def test_roundtrip_large_data(self, engine: EncryptionEngine) -> None:
        """대용량 데이터 round-trip."""
        plaintext = os.urandom(1024 * 1024)  # 1MB
        encrypted = engine.encrypt(plaintext)
        assert engine.decrypt(encrypted) == plaintext

    def test_wrong_key_raises_integrity_error(self, valid_key: bytes) -> None:
        """잘못된 키로 복호화 시 IntegrityError (Req 4.7)."""
        engine1 = EncryptionEngine(valid_key)
        engine2 = EncryptionEngine(os.urandom(32))

        encrypted = engine1.encrypt(b'secret data')
        with pytest.raises(IntegrityError):
            engine2.decrypt(encrypted)

    def test_tampered_ciphertext_raises_integrity_error(
        self, engine: EncryptionEngine
    ) -> None:
        """변조된 암호문 복호화 시 IntegrityError (Req 4.8)."""
        encrypted = engine.encrypt(b'original data')
        # 암호문 영역(38바이트 이후) 변조
        tampered = bytearray(encrypted)
        tampered[38] ^= 0xFF
        with pytest.raises(IntegrityError):
            engine.decrypt(bytes(tampered))

    def test_tampered_tag_raises_integrity_error(
        self, engine: EncryptionEngine
    ) -> None:
        """변조된 인증 태그 복호화 시 IntegrityError."""
        encrypted = engine.encrypt(b'data')
        tampered = bytearray(encrypted)
        tampered[22] ^= 0xFF  # 태그 영역 변조
        with pytest.raises(IntegrityError):
            engine.decrypt(bytes(tampered))

    def test_invalid_magic_raises_decryption_error(
        self, engine: EncryptionEngine
    ) -> None:
        """잘못된 매직 바이트 시 DecryptionError."""
        encrypted = engine.encrypt(b'data')
        tampered = b'XXXX' + encrypted[4:]
        with pytest.raises(DecryptionError):
            engine.decrypt(tampered)

    def test_too_short_data_raises_decryption_error(
        self, engine: EncryptionEngine
    ) -> None:
        """헤더 크기 미만 데이터 시 DecryptionError."""
        with pytest.raises(DecryptionError):
            engine.decrypt(b'SDFS' + b'\x00' * 10)


class TestStream:
    """스트림 암호화/복호화 테스트."""

    def test_stream_roundtrip(self, engine: EncryptionEngine) -> None:
        """스트림 기반 round-trip."""
        plaintext = b'Stream test data'
        input_stream = io.BytesIO(plaintext)
        encrypted_stream = io.BytesIO()

        engine.encrypt_stream(input_stream, encrypted_stream)

        encrypted_stream.seek(0)
        output_stream = io.BytesIO()
        engine.decrypt_stream(encrypted_stream, output_stream)

        assert output_stream.getvalue() == plaintext


class TestGenerateIV:
    """IV 생성 테스트."""

    def test_iv_length(self) -> None:
        """IV는 16바이트."""
        iv = EncryptionEngine.generate_iv()
        assert len(iv) == 16

    def test_iv_uniqueness(self) -> None:
        """연속 생성된 IV는 서로 다름."""
        ivs = {EncryptionEngine.generate_iv() for _ in range(100)}
        assert len(ivs) == 100
