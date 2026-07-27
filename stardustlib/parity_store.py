"""패리티 스토어 (호스트 역할).

다른 사용자의 암호문 청크를 이 기기의 스토리지 소스에 보관한다. 호스트는 내용을
복호화할 수 없고(소유자 키 없음), 인가된 소유자에게만 청크를 제공한다.

내 청크든 타 사용자 청크든 같은 소스를 쓰므로 용량 집계가 한 곳으로 모인다
(`get_available_space()`가 보관 청크를 반영한다 — Property 8). 인덱스는 메타데이터
DB의 `hosted_chunks` 테이블이고, 물리 경로는 내 청크와 구분되는 접두사(`p/<hh>/…`)를
쓴다. 인가는 경로가 아니라 DB의 `owner_user_id`로 집행한다.

쿼터는 서버가 정한 호스팅 상한(max_bytes)이며, 소스 공간 부족도 같은
QuotaExceededError(→ p2p 507)로 거부해 요청자의 홀더 배제 로직이 그대로 동작한다.
"""

from __future__ import annotations

import logging
import os

from stardustlib import chunker

logger = logging.getLogger(__name__)

# 보관 청크 물리 경로 접두사. 내 청크(`<hh>/<hex32>_cNNNN`)와 한눈에 구분된다.
PARITY_PREFIX = "p"


class QuotaExceededError(Exception):
    """패리티 스토어 쿼터 초과(또는 소스 공간 부족)."""


def _validate_chunk_id(chunk_id: str) -> None:
    if not chunk_id or any(sep in chunk_id for sep in ("/", "\\")) or ".." in chunk_id:
        raise ValueError(f"유효하지 않은 chunk_id: {chunk_id!r}")


class ParityStore:
    """타 사용자 청크 암호문을 스토리지 소스에 보관한다."""

    def __init__(
        self, storage_pool, metadata_store, max_bytes: int | None = None
    ) -> None:
        self._pool = storage_pool
        self._meta = metadata_store
        self._max_bytes = max_bytes

    # --- 경로/소스 선택 ---

    @staticmethod
    def _physical_path(chunk_id: str) -> str:
        """보관 청크의 소스 내 물리 경로 `p/<hh>/<chunk_id>`.

        샤딩은 디렉토리당 엔트리 수를 줄이기 위한 것이므로, 샤드 키를 만들 수 없는
        짧은 식별자는 단일 버킷에 둔다(경로 규칙은 인가와 무관하다).
        """
        try:
            shard = chunker.shard_prefix(chunk_id)
        except ValueError:
            shard = "00"
        return f"{PARITY_PREFIX}/{shard}/{chunk_id}"

    def _select_source(self, size: int):
        """청크를 놓을 로컬 소스를 고른다(여유가 가장 많은 활성 소스).

        Raises:
            QuotaExceededError: 그만한 여유가 있는 로컬 소스가 없을 때.
        """
        best = None
        best_space = -1
        for source in getattr(self._pool, "sources", []):
            if not source.is_active or source.is_remote:
                continue
            available = source.get_available_space()
            if available >= size and available > best_space:
                best = source
                best_space = available
        if best is None:
            raise QuotaExceededError(
                f"보관 청크를 놓을 소스 여유 공간이 없습니다: {size} bytes"
            )
        return best

    def _source_of(self, source_id: str):
        for source in getattr(self._pool, "sources", []):
            if source.source_id == source_id:
                return source
        return None

    # --- 쿼터 ---

    def set_max_bytes(self, max_bytes: int | None) -> None:
        """호스팅 쿼터를 갱신한다(정책 변경 반영). None이면 무제한."""
        self._max_bytes = max_bytes

    def used_bytes(self) -> int:
        """보관 중인 바이트 합계(DB 집계 — 파일을 스캔하지 않는다)."""
        return self._meta.hosted_bytes()

    # --- 보관/조회/삭제 ---

    def store(self, chunk_id: str, owner_user_id: str, data: bytes) -> str:
        """청크 암호문을 소스에 보관하고 DB에 등록한다.

        Returns:
            보관한 소스의 source_id(요청자가 카피 위치로 서버에 등록한다).

        Raises:
            ValueError: chunk_id가 규격에 맞지 않을 때.
            PermissionError: 이미 다른 사용자 소유로 보관된 chunk_id일 때.
            QuotaExceededError: 호스팅 상한 초과 또는 소스 공간 부족.
        """
        _validate_chunk_id(chunk_id)
        existing = self._meta.get_hosted_chunk(chunk_id)
        if existing is not None and existing["owner_user_id"] != owner_user_id:
            raise PermissionError("청크 소유자 불일치")

        prev_size = existing["size"] if existing else 0
        projected = self.used_bytes() - prev_size + len(data)
        if self._max_bytes is not None and projected > self._max_bytes:
            raise QuotaExceededError(
                f"패리티 쿼터 초과: {projected} > {self._max_bytes}"
            )

        source = self._select_source(len(data))
        path = self._physical_path(chunk_id)
        try:
            source.write(path, data)
        except OSError as e:
            # 소스 공간 부족은 쿼터 초과와 같은 응답(507)으로 다룬다.
            if "insufficient space" in str(e).lower():
                raise QuotaExceededError(str(e)) from e
            raise
        # 다른 소스에 있던 이전 사본은 인덱스 교체 전에 지운다(중복 점유 방지).
        if existing is not None and existing["source_id"] != source.source_id:
            self._remove_bytes(existing, best_effort=True)
        self._meta.put_hosted_chunk(
            chunk_id, owner_user_id, source.source_id, path, len(data)
        )
        return source.source_id

    def fetch(self, chunk_id: str, requester_user_id: str) -> bytes:
        """소유자에게만 청크 암호문을 반환한다.

        없으면 FileNotFoundError, 소유자 아니면 PermissionError.
        """
        _validate_chunk_id(chunk_id)
        meta = self._meta.get_hosted_chunk(chunk_id)
        if meta is None:
            raise FileNotFoundError(chunk_id)
        if meta["owner_user_id"] != requester_user_id:
            raise PermissionError("청크 소유자만 조회할 수 있습니다")
        source = self._source_of(meta["source_id"])
        if source is None or not source.is_active:
            raise FileNotFoundError(
                f"보관 소스에 접근할 수 없습니다: {meta['source_id']}"
            )
        return source.read(meta["physical_path"])

    def delete(self, chunk_id: str, requester_user_id: str) -> None:
        """소유자만 삭제한다(멱등). 소유자 아니면 PermissionError."""
        _validate_chunk_id(chunk_id)
        meta = self._meta.get_hosted_chunk(chunk_id)
        if meta is None:
            return
        if meta["owner_user_id"] != requester_user_id:
            raise PermissionError("청크 소유자만 삭제할 수 있습니다")
        self._remove_bytes(meta, best_effort=True)
        self._meta.delete_hosted_chunk(chunk_id)

    def _remove_bytes(self, meta: dict, best_effort: bool = False) -> None:
        """보관 청크의 물리 바이트를 지운다(인덱스는 호출자가 다룬다)."""
        source = self._source_of(meta["source_id"])
        if source is None:
            return
        try:
            source.delete(meta["physical_path"])
        except Exception as e:  # noqa: BLE001 — 잔여 파일은 다음 보관에서 덮어쓴다
            if not best_effort:
                raise
            logger.debug(
                "보관 청크 삭제 실패(무시): %s: %s", meta["physical_path"], e
            )

    def exists(self, chunk_id: str) -> bool:
        return self._meta.get_hosted_chunk(chunk_id) is not None

    def owner_of(self, chunk_id: str) -> str | None:
        meta = self._meta.get_hosted_chunk(chunk_id)
        return meta["owner_user_id"] if meta else None

    # --- 레거시 `.parity/` 디렉토리 이관 ---

    def migrate_legacy_dir(self, legacy_dir: str) -> dict:
        """구 버전의 `{metadata_db}.parity/` 청크를 스토리지 소스로 옮긴다.

        index.json의 소유자 정보를 DB로 옮기고 `<chunk_id>.bin`을 소스에 기록한다.
        소스 공간이 부족해 옮기지 못한 청크는 디렉토리에 남기고 로그로 알린다
        (무손실 우선 — 다음 기동에 재시도한다).

        Returns:
            {"moved": int, "left": int, "bytes": int}
        """
        import json

        index_path = os.path.join(legacy_dir, "index.json")
        if not os.path.isdir(legacy_dir) or not os.path.exists(index_path):
            return {"moved": 0, "left": 0, "bytes": 0}
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("레거시 패리티 index 읽기 실패, 이관 생략: %s", e)
            return {"moved": 0, "left": 0, "bytes": 0}
        if not isinstance(index, dict):
            return {"moved": 0, "left": 0, "bytes": 0}

        moved = left = moved_bytes = 0
        for chunk_id, meta in list(index.items()):
            owner = (meta or {}).get("owner")
            src_path = os.path.join(legacy_dir, f"{chunk_id}.bin")
            if not owner or not os.path.exists(src_path):
                continue
            if self.exists(chunk_id):
                # 이미 소스로 옮긴 청크 — 남은 파일만 정리한다.
                self._discard_legacy(src_path, index, chunk_id, index_path)
                continue
            try:
                with open(src_path, "rb") as f:
                    data = f.read()
                self.store(chunk_id, owner, data)
            except QuotaExceededError:
                left += 1
                continue
            except Exception as e:  # noqa: BLE001 — 청크 단위 실패 격리
                left += 1
                logger.warning("보관 청크 이관 실패(남김): %s: %s", chunk_id, e)
                continue
            moved += 1
            moved_bytes += len(data)
            self._discard_legacy(src_path, index, chunk_id, index_path)

        if moved:
            logger.info(
                "레거시 보관 청크 이관: %d개(%d bytes) → 스토리지 소스",
                moved, moved_bytes,
            )
        if left:
            logger.warning(
                "보관 청크 %d개를 옮기지 못해 %s에 남겼습니다(소스 공간 부족 — "
                "다음 기동에 재시도)", left, legacy_dir,
            )
        return {"moved": moved, "left": left, "bytes": moved_bytes}

    @staticmethod
    def _discard_legacy(
        src_path: str, index: dict, chunk_id: str, index_path: str
    ) -> None:
        """이관을 마친 레거시 파일과 인덱스 항목을 지운다(베스트에포트)."""
        try:
            os.remove(src_path)
        except OSError:
            pass
        index.pop(chunk_id, None)
        try:
            import json

            tmp = index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f)
            os.replace(tmp, index_path)
        except OSError as e:
            logger.debug("레거시 패리티 index 갱신 실패(무시): %s", e)
