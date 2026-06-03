"""패리티 스토어 (호스트 역할).

다른 사용자의 암호문 청크를 로컬에 보관한다. 호스트는 내용을 복호화할 수 없고
(소유자 키 없음), 인가된 소유자에게만 청크를 제공한다. 쿼터(최대 바이트)를 집행한다.

index.json: chunk_id -> {"owner": user_id, "size": int}. 청크 암호문은
<dir>/<chunk_id>.bin 으로 저장한다.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """패리티 스토어 쿼터 초과."""


def _validate_chunk_id(chunk_id: str) -> None:
    if not chunk_id or any(sep in chunk_id for sep in ("/", "\\")) or ".." in chunk_id:
        raise ValueError(f"유효하지 않은 chunk_id: {chunk_id!r}")


class ParityStore:
    """타 사용자 청크 암호문 로컬 보관소."""

    def __init__(self, base_dir: str, max_bytes: int | None = None) -> None:
        self._dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self._index_path = os.path.join(base_dir, "index.json")
        self._max_bytes = max_bytes
        self._index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict[str, dict]:
        if not os.path.exists(self._index_path):
            return {}
        try:
            with open(self._index_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            logger.warning("패리티 index 손상, 비어있는 것으로 시작")
            return {}

    def _save_index(self) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._index, f)
        os.replace(tmp, self._index_path)

    def _chunk_path(self, chunk_id: str) -> str:
        return os.path.join(self._dir, chunk_id + ".bin")

    def used_bytes(self) -> int:
        return sum(meta.get("size", 0) for meta in self._index.values())

    def store(self, chunk_id: str, owner_user_id: str, data: bytes) -> None:
        """청크 암호문을 보관한다. 소유자 불일치 시 PermissionError, 쿼터 초과 시
        QuotaExceededError."""
        _validate_chunk_id(chunk_id)
        existing = self._index.get(chunk_id)
        if existing is not None and existing.get("owner") != owner_user_id:
            raise PermissionError("청크 소유자 불일치")

        prev_size = existing.get("size", 0) if existing else 0
        projected = self.used_bytes() - prev_size + len(data)
        if self._max_bytes is not None and projected > self._max_bytes:
            raise QuotaExceededError(
                f"패리티 쿼터 초과: {projected} > {self._max_bytes}"
            )

        path = self._chunk_path(chunk_id)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self._index[chunk_id] = {"owner": owner_user_id, "size": len(data)}
        self._save_index()

    def fetch(self, chunk_id: str, requester_user_id: str) -> bytes:
        """소유자에게만 청크 암호문을 반환한다.

        없으면 FileNotFoundError, 소유자 아니면 PermissionError.
        """
        _validate_chunk_id(chunk_id)
        meta = self._index.get(chunk_id)
        if meta is None:
            raise FileNotFoundError(chunk_id)
        if meta.get("owner") != requester_user_id:
            raise PermissionError("청크 소유자만 조회할 수 있습니다")
        with open(self._chunk_path(chunk_id), "rb") as f:
            return f.read()

    def delete(self, chunk_id: str, requester_user_id: str) -> None:
        """소유자만 삭제한다(멱등). 소유자 아니면 PermissionError."""
        _validate_chunk_id(chunk_id)
        meta = self._index.get(chunk_id)
        if meta is None:
            return
        if meta.get("owner") != requester_user_id:
            raise PermissionError("청크 소유자만 삭제할 수 있습니다")
        try:
            os.remove(self._chunk_path(chunk_id))
        except OSError:
            pass
        del self._index[chunk_id]
        self._save_index()

    def exists(self, chunk_id: str) -> bool:
        return chunk_id in self._index

    def owner_of(self, chunk_id: str) -> str | None:
        meta = self._index.get(chunk_id)
        return meta.get("owner") if meta else None
