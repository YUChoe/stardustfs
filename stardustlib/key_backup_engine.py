"""Key 백업/복원 엔진.

master_key를 사용자 비밀번호로 2차 암호화하여 백업 blob을 생성/복원한다.
PBKDF2-SHA256으로 비밀번호에서 파생 키를 생성하고, AES-256-GCM으로 암호화한다.

blob 구조: salt(16B) + iv(12B) + tag(16B) + ciphertext
"""

import os

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from stardustlib.exceptions import IntegrityError


class KeyBackupEngine:
    """Key 백업/복원 엔진.

    master_key를 사용자 비밀번호 기반 파생 키로 암호화하여
    서버에 안전하게 백업할 수 있는 blob을 생성한다.
    """

    PBKDF2_ITERATIONS: int = 600_000
    SALT_SIZE: int = 16
    IV_SIZE: int = 12
    TAG_SIZE: int = 16

    def encrypt_for_backup(self, master_key: bytes, password: str) -> bytes:
        """master_key를 비밀번호로 암호화하여 백업 blob 생성.

        1. PBKDF2-SHA256으로 비밀번호에서 32바이트 파생 키 생성
        2. AES-256-GCM으로 master_key 암호화
        3. salt + iv + tag + ciphertext 형식의 blob 반환

        Args:
            master_key: 암호화할 32바이트 마스터 키.
            password: 파생 키 생성에 사용할 비밀번호 (최소 8자 권장).

        Returns:
            salt(16B) + iv(12B) + tag(16B) + ciphertext 형식의 blob.
        """
        # 랜덤 salt 생성
        salt = os.urandom(self.SALT_SIZE)

        # PBKDF2-SHA256으로 파생 키 생성
        derived_key = self._derive_key(password, salt)

        # 랜덤 IV 생성
        iv = os.urandom(self.IV_SIZE)

        # AES-256-GCM 암호화
        aesgcm = AESGCM(derived_key)
        # AESGCM.encrypt()는 ciphertext + tag(16B)를 반환
        ct_with_tag = aesgcm.encrypt(iv, master_key, None)

        # ciphertext와 tag 분리
        ciphertext = ct_with_tag[:-self.TAG_SIZE]
        tag = ct_with_tag[-self.TAG_SIZE:]

        # blob 조립: salt + iv + tag + ciphertext
        return salt + iv + tag + ciphertext

    def decrypt_from_backup(self, blob: bytes, password: str) -> bytes:
        """백업 blob을 비밀번호로 복호화하여 master_key 복원.

        1. blob에서 salt, iv, tag, ciphertext 파싱
        2. PBKDF2-SHA256으로 파생 키 재생성
        3. AES-256-GCM으로 복호화

        Args:
            blob: encrypt_for_backup()으로 생성된 백업 blob.
            password: 암호화 시 사용한 비밀번호.

        Returns:
            복호화된 원본 master_key.

        Raises:
            IntegrityError: 비밀번호 불일치 또는 blob 변조 시.
        """
        min_size = self.SALT_SIZE + self.IV_SIZE + self.TAG_SIZE
        if len(blob) < min_size:
            raise IntegrityError("백업 blob 크기가 최소 크기 미만입니다")

        # blob 파싱
        offset = 0
        salt = blob[offset:offset + self.SALT_SIZE]
        offset += self.SALT_SIZE

        iv = blob[offset:offset + self.IV_SIZE]
        offset += self.IV_SIZE

        tag = blob[offset:offset + self.TAG_SIZE]
        offset += self.TAG_SIZE

        ciphertext = blob[offset:]

        # PBKDF2-SHA256으로 파생 키 재생성
        derived_key = self._derive_key(password, salt)

        # AES-256-GCM 복호화 (ciphertext + tag 결합)
        aesgcm = AESGCM(derived_key)
        ct_with_tag = ciphertext + tag

        try:
            master_key = aesgcm.decrypt(iv, ct_with_tag, None)
        except InvalidTag:
            raise IntegrityError(
                "복호화 실패: 비밀번호 불일치 또는 백업 데이터 변조"
            )

        return master_key

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        """PBKDF2-SHA256으로 비밀번호에서 32바이트 파생 키 생성.

        Args:
            password: 사용자 비밀번호.
            salt: 16바이트 솔트.

        Returns:
            32바이트 파생 키.
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self.PBKDF2_ITERATIONS,
        )
        return kdf.derive(password.encode("utf-8"))
