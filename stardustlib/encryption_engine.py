"""AES-256-GCM 암호화/복호화 엔진.

파일 데이터를 AES-256-GCM 모드로 암호화/복호화하며,
GCM 인증 태그를 통한 무결성 검증을 수행한다.

파일 헤더 구조: [MAGIC(4B)][VERSION(1B)][MODE_ID(1B)][IV(16B)][TAG(16B)][CIPHERTEXT...]
"""

import os
from typing import IO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.exceptions import InvalidTag

from stardustlib.exceptions import DecryptionError, IntegrityError, InvalidKeyError


class EncryptionEngine:
    """AES-256-GCM 암호화 엔진.

    파일별 고유 IV(CSPRNG 128비트)를 생성하고,
    암호화된 파일 헤더에 IV, 모드 식별자, 인증 태그를 저장한다.
    """

    HEADER_SIZE: int = 38  # 4 + 1 + 1 + 16 + 16
    MAGIC: bytes = b'SDFS'
    VERSION: int = 1
    MODE_GCM: int = 1

    def __init__(self, key: bytes) -> None:
        """EncryptionEngine 초기화.

        Args:
            key: 정확히 32바이트(256비트)의 암호화 키.

        Raises:
            InvalidKeyError: 키 길이가 32바이트가 아닐 때.
        """
        if not self.validate_key(key):
            raise InvalidKeyError(
                f"키 길이가 32바이트여야 합니다 (현재: {len(key)}바이트)"
            )
        self._key = key

    def encrypt(self, plaintext: bytes) -> bytes:
        """AES-256-GCM으로 데이터를 암호화하여 헤더 포함 바이트열 반환.

        Args:
            plaintext: 암호화할 원본 데이터 (빈 바이트열 허용).

        Returns:
            MAGIC + VERSION + MODE_ID + IV + TAG + CIPHERTEXT 형태의 바이트열.
        """
        iv = self.generate_iv()

        cipher = Cipher(algorithms.AES(self._key), modes.GCM(iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()
        tag = encryptor.tag  # 16바이트 인증 태그

        header = self.MAGIC + bytes([self.VERSION, self.MODE_GCM]) + iv + tag
        return header + ciphertext

    def decrypt(self, encrypted_data: bytes) -> bytes:
        """암호화된 데이터를 복호화하여 원본 반환.

        GCM 인증 태그를 검증하여 무결성을 확인한다.

        Args:
            encrypted_data: 헤더 포함 암호화 데이터.

        Returns:
            복호화된 원본 데이터.

        Raises:
            DecryptionError: 잘못된 파일 형식(매직 바이트 불일치) 또는
                            데이터 길이가 헤더 크기 미만일 때.
            IntegrityError: 인증 태그 검증 실패 (키 불일치 또는 데이터 변조).
        """
        if len(encrypted_data) < self.HEADER_SIZE:
            raise DecryptionError("데이터 길이가 헤더 크기 미만입니다")

        if encrypted_data[:4] != self.MAGIC:
            raise DecryptionError("잘못된 파일 형식")

        # 헤더 파싱
        iv = encrypted_data[6:22]
        tag = encrypted_data[22:38]
        ciphertext = encrypted_data[38:]

        # AES-256-GCM 복호화 + 인증 태그 검증
        cipher = Cipher(algorithms.AES(self._key), modes.GCM(iv, tag))
        decryptor = cipher.decryptor()

        try:
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        except InvalidTag:
            raise IntegrityError(
                "인증 태그 검증 실패: 키 불일치 또는 데이터 변조"
            )

        return plaintext

    def encrypt_stream(self, input_stream: IO[bytes], output_stream: IO[bytes]) -> None:
        """스트림 기반 암호화. 대용량 파일 지원.

        input_stream의 전체 내용을 읽어 암호화한 후
        헤더와 암호문을 output_stream에 기록한다.

        Args:
            input_stream: 원본 데이터를 읽을 스트림.
            output_stream: 암호화된 데이터를 기록할 스트림.
        """
        plaintext = input_stream.read()
        encrypted = self.encrypt(plaintext)
        output_stream.write(encrypted)

    def decrypt_stream(self, input_stream: IO[bytes], output_stream: IO[bytes]) -> None:
        """스트림 기반 복호화. 대용량 파일 지원.

        input_stream의 전체 내용을 읽어 복호화한 후
        원본 데이터를 output_stream에 기록한다.

        Args:
            input_stream: 암호화된 데이터를 읽을 스트림.
            output_stream: 복호화된 데이터를 기록할 스트림.

        Raises:
            DecryptionError: 잘못된 파일 형식.
            IntegrityError: 인증 태그 검증 실패.
        """
        encrypted_data = input_stream.read()
        plaintext = self.decrypt(encrypted_data)
        output_stream.write(plaintext)

    @staticmethod
    def generate_iv() -> bytes:
        """CSPRNG으로 128비트(16바이트) IV를 생성.

        Returns:
            16바이트의 암호학적으로 안전한 랜덤 IV.
        """
        return os.urandom(16)

    @staticmethod
    def validate_key(key: bytes) -> bool:
        """키 길이가 정확히 32바이트인지 검증.

        Args:
            key: 검증할 암호화 키.

        Returns:
            키가 정확히 32바이트이면 True, 아니면 False.
        """
        return isinstance(key, bytes) and len(key) == 32
