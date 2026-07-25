"""암호문 청크 분할/결합 (리플리케이션용).

암호화는 상위(encryption_engine)에서 수행하고, 청킹은 그 암호문(opaque bytes) 위에서
고정 크기로 나눈다. ``join(split(x)) == x`` 가 성립한다(스펙 replication-parity).
호스트는 청크 암호문만 보관하며 내용을 복호화할 수 없다.
"""

from __future__ import annotations

import hashlib

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


def chunk_hash(data: bytes) -> str:
    """청크 암호문의 SHA-256 hex(64자)를 반환한다.

    내용 검증용 해시다. 대상은 평문이 아니라 암호문 바이트이므로 서버·홀더가 이 값을
    보관해도 평문 정보가 노출되지 않는다. chunk_id(위치 식별자, file_ref:idx의 해시)와
    는 별개로, 홀더가 돌려준 바이트가 복제 시점과 같은지 확인하는 데 쓴다.
    """
    return hashlib.sha256(data).hexdigest()


def split(blob: bytes, size: int = DEFAULT_CHUNK_SIZE) -> list[tuple[int, bytes]]:
    """blob을 고정 크기 청크로 나눈다. (idx, bytes) 목록을 인덱스 순서로 반환.

    마지막 청크는 size 이하. 빈 입력은 빈 목록을 반환한다(join([])==b"").
    """
    if size <= 0:
        raise ValueError("청크 크기는 1 이상이어야 합니다")
    return [
        (idx, blob[off:off + size])
        for idx, off in enumerate(range(0, len(blob), size))
    ]


def join(parts: list[tuple[int, bytes]]) -> bytes:
    """(idx, bytes) 목록을 인덱스 순서로 결합해 원본 blob을 복원한다."""
    return b"".join(data for _idx, data in sorted(parts, key=lambda p: p[0]))


def chunk_count(total_size: int, size: int = DEFAULT_CHUNK_SIZE) -> int:
    """total_size 바이트를 size 청크로 나눌 때의 청크 수."""
    if size <= 0:
        raise ValueError("청크 크기는 1 이상이어야 합니다")
    return (total_size + size - 1) // size
