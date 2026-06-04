"""자격증명 저장소.

인증 토큰(access/refresh)과 마스터키 백업 암호(key_password)를 클라이언트 로컬
파일에 보관한다. 로그인 비밀번호는 저장하지 않는다.

- 경로: ``{metadata_db}.credentials.json`` (daemon.json/syncstate.json과 동일 규칙)
- 기록: tmp + os.replace 로 원자적(부분 기록 손상 방지)
- 권한: 소유자 전용 (POSIX 0600 / Windows icacls best-effort)
- 동시 갱신 직렬화를 위한 파일 락 헬퍼를 제공한다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT = 10.0
_LOCK_POLL = 0.1


class CredentialStoreError(Exception):
    """자격증명 저장소 읽기/쓰기 오류."""


def _restrict_permissions(path: str) -> None:
    """파일을 소유자 전용 권한으로 제한한다 (best-effort)."""
    try:
        if os.name == "posix":
            os.chmod(path, 0o600)
        else:
            # Windows: 상속 제거 후 소유자에게만 권한 부여 (best-effort)
            import getpass
            import subprocess

            user = os.environ.get("USERNAME") or getpass.getuser()
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, check=False,
            )
    except Exception as e:  # noqa: BLE001 — 권한 제한 실패는 치명적이지 않음
        logger.warning("자격증명 파일 권한 제한 실패: %s", e)


@contextmanager
def file_lock(lock_path: str, timeout: float = _LOCK_TIMEOUT) -> Iterator[None]:
    """배타적 파일 락 (O_CREAT|O_EXCL 스핀 + 타임아웃).

    단일 호스트의 daemon/CLI 동시 갱신을 직렬화한다. 타임아웃 시 TimeoutError.
    """
    deadline = time.time() + timeout
    fd: int | None = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise TimeoutError(
                    f"자격증명 락 획득 시간 초과: {lock_path}"
                )
            time.sleep(_LOCK_POLL)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lock_path)
        except OSError:
            pass


class CredentialStore:
    """``{metadata_db}.credentials.json`` 파일을 관리한다."""

    def __init__(self, metadata_db_path: str) -> None:
        self._path = metadata_db_path + ".credentials.json"

    @property
    def path(self) -> str:
        return self._path

    @property
    def lock_path(self) -> str:
        return self._path + ".lock"

    def exists(self) -> bool:
        return os.path.exists(self._path)

    def load(self) -> dict | None:
        """저장소를 읽어 dict를 반환한다. 없으면 None, 손상 시 예외."""
        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise CredentialStoreError(
                f"자격증명 저장소를 읽을 수 없습니다: {self._path}: {e}"
            ) from e
        if not isinstance(data, dict):
            raise CredentialStoreError(
                f"자격증명 저장소 형식이 올바르지 않습니다: {self._path}"
            )
        return data

    def save(self, data: dict) -> None:
        """저장소를 원자적으로 기록하고 소유자 전용 권한을 부여한다."""
        tmp = self._path + ".tmp"
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        _restrict_permissions(tmp)
        os.replace(tmp, self._path)
        _restrict_permissions(self._path)

    def clear(self) -> None:
        """저장소 파일과 잔존 tmp/lock을 삭제한다."""
        for path in (self._path, self._path + ".tmp", self.lock_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logger.debug("자격증명 파일 삭제 중 예외: %s: %s", path, e)
