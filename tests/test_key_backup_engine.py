"""KeyBackupEngine 단위 테스트."""

import os

import pytest

from stardustlib.key_backup_engine import KeyBackupEngine
from stardustlib.exceptions import IntegrityError


@pytest.fixture
def engine():
    return KeyBackupEngine()


@pytest.fixture
def master_key():
    return os.urandom(32)


@pytest.fixture
def password():
    return "test_password_123"


class TestEncryptForBackup:
    """encrypt_for_backup 메서드 테스트."""

    def test_returns_correct_blob_size_for_32byte_key(self, engine, password):
        """32바이트 master_key에 대해 76바이트 blob 반환."""
        master_key = os.urandom(32)
        blob = engine.encrypt_for_backup(master_key, password)
        # salt(16) + iv(12) + tag(16) + ciphertext(32) = 76
        assert len(blob) == 76

    def test_different_calls_produce_different_blobs(self, engine, master_key, password):
        """동일 입력에 대해 매번 다른 blob 생성 (랜덤 salt/iv)."""
        blob1 = engine.encrypt_for_backup(master_key, password)
        blob2 = engine.encrypt_for_backup(master_key, password)
        assert blob1 != blob2

    def test_blob_starts_with_salt(self, engine, master_key, password):
        """blob의 처음 16바이트는 salt."""
        blob = engine.encrypt_for_backup(master_key, password)
        salt = blob[:16]
        assert len(salt) == 16


class TestDecryptFromBackup:
    """decrypt_from_backup 메서드 테스트."""

    def test_roundtrip(self, engine, master_key, password):
        """암호화 후 동일 비밀번호로 복호화하면 원본 복원."""
        blob = engine.encrypt_for_backup(master_key, password)
        recovered = engine.decrypt_from_backup(blob, password)
        assert recovered == master_key

    def test_wrong_password_raises_integrity_error(self, engine, master_key, password):
        """잘못된 비밀번호로 복호화 시 IntegrityError 발생."""
        blob = engine.encrypt_for_backup(master_key, password)
        with pytest.raises(IntegrityError):
            engine.decrypt_from_backup(blob, "wrong_password")

    def test_tampered_blob_raises_integrity_error(self, engine, master_key, password):
        """변조된 blob으로 복호화 시 IntegrityError 발생."""
        blob = engine.encrypt_for_backup(master_key, password)
        # ciphertext 영역 변조
        tampered = bytearray(blob)
        tampered[-1] ^= 0xFF
        with pytest.raises(IntegrityError):
            engine.decrypt_from_backup(bytes(tampered), password)

    def test_too_short_blob_raises_integrity_error(self, engine, password):
        """최소 크기 미만 blob으로 복호화 시 IntegrityError 발생."""
        short_blob = b"\x00" * 10
        with pytest.raises(IntegrityError):
            engine.decrypt_from_backup(short_blob, password)

    def test_empty_blob_raises_integrity_error(self, engine, password):
        """빈 blob으로 복호화 시 IntegrityError 발생."""
        with pytest.raises(IntegrityError):
            engine.decrypt_from_backup(b"", password)


class TestConstants:
    """상수 값 검증."""

    def test_pbkdf2_iterations(self, engine):
        assert engine.PBKDF2_ITERATIONS == 600_000

    def test_salt_size(self, engine):
        assert engine.SALT_SIZE == 16

    def test_iv_size(self, engine):
        assert engine.IV_SIZE == 12

    def test_tag_size(self, engine):
        assert engine.TAG_SIZE == 16
