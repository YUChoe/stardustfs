"""암호문 청크 분할/결합 (리플리케이션용).

암호화는 상위(encryption_engine)에서 수행하고, 청킹은 그 암호문(opaque bytes) 위에서
고정 크기로 나눈다. ``join(split(x)) == x`` 가 성립한다(스펙 replication-parity).
호스트는 청크 암호문만 보관하며 내용을 복호화할 수 없다.
"""

from __future__ import annotations

import hashlib
import uuid

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

# 청크 파일을 담을 서브디렉토리 이름 길이(hex 문자 수). 1단계 = 16^2 = 256개.
#
# 근거(실측): pyfatfs는 순수 파이썬이라 파일 생성 시 부모 디렉토리 엔트리를 선형
# 스캔한다. 2 GiB FAT32 이미지에 파일을 평면으로 채우면 500개 배치마다 13s→24s→
# 36s→50s로 느려져 O(n^2)이 된다. 앞 2 hex로 256개 서브디렉토리에 분산하면 배치당
# ~12s로 평탄해진다(선형). 깊이를 더 늘리면 서브디렉토리마다 최소 1클러스터를
# 점유해 큰 볼륨에서 낭비가 크고 작은 볼륨은 여유가 없으므로 1단계를 기본으로 둔다.
SHARD_HEX_LEN = 2

# 한 디렉토리가 감당할 청크 수 상한. 실측에서 평면 배치는 500개까지는 기준선 속도를
# 유지했고 1000개 시점에 배치 시간이 2배가 됐다. 이 경계 아래로 유지하도록 샤딩 깊이를
# 정한다(초과가 예상되면 한 단계 더 깊게 나눈다).
MAX_CHUNKS_PER_SHARD = 512


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


def chunk_range(
    offset: int, length: int, size: int = DEFAULT_CHUNK_SIZE
) -> list[int]:
    """[offset, offset+length) 범위를 덮는 청크 인덱스 목록을 순서대로 반환한다.

    부분 읽기에서 실제로 가져와야 할 청크만 고르는 데 쓴다. length가 0이면 빈 목록.

    Raises:
        ValueError: size가 1 미만이거나 offset/length가 음수일 때.
    """
    if size <= 0:
        raise ValueError("청크 크기는 1 이상이어야 합니다")
    if offset < 0 or length < 0:
        raise ValueError("offset/length는 0 이상이어야 합니다")
    if length == 0:
        return []
    first = offset // size
    last = (offset + length - 1) // size
    return list(range(first, last + 1))


def shard_prefix(
    chunk_hash: str, hex_len: int = SHARD_HEX_LEN, depth: int = 1
) -> str:
    """청크 암호문 해시에서 서브디렉토리 경로를 만든다.

    샤드 키로 파일 단위 식별자(uuid 접두사)를 쓰면 한 파일의 모든 청크가 같은
    디렉토리로 몰려 디렉토리 엔트리 폭증이 재현된다. 청크마다 달라지는 암호문 해시를
    써야 균등하게 분산된다.

    depth가 2 이상이면 `ab/cd`처럼 단계마다 hex_len자씩 잘라 계층을 만든다.

    Raises:
        ValueError: hex_len/depth가 1 미만이거나 해시가 필요한 길이보다 짧을 때.
    """
    if hex_len <= 0:
        raise ValueError("hex_len은 1 이상이어야 합니다")
    if depth <= 0:
        raise ValueError("depth는 1 이상이어야 합니다")
    need = hex_len * depth
    if len(chunk_hash) < need:
        raise ValueError(
            f"청크 해시가 너무 짧습니다: {len(chunk_hash)} < {need}"
        )
    return "/".join(
        chunk_hash[i * hex_len:(i + 1) * hex_len] for i in range(depth)
    )


def shard_depth_for(
    capacity_bytes: int, chunk_size: int = DEFAULT_CHUNK_SIZE,
    hex_len: int = SHARD_HEX_LEN,
) -> int:
    """소스 용량에 맞는 샤딩 깊이를 정한다.

    예상 청크 수(capacity/chunk_size)를 샤드 수(16^(hex_len*depth))로 나눈 값이
    MAX_CHUNKS_PER_SHARD를 넘지 않는 최소 깊이를 고른다. 각 서브디렉토리가 최소
    1클러스터를 점유하므로 필요 이상으로 깊게 나누지 않는다(작은 볼륨에서는 낭비,
    큰 볼륨에서만 한 단계 더 깊어진다).

    Raises:
        ValueError: chunk_size가 1 미만일 때.
    """
    if chunk_size <= 0:
        raise ValueError("청크 크기는 1 이상이어야 합니다")
    expected = max(0, capacity_bytes) // chunk_size
    depth = 1
    while depth < 4:
        shards = (16 ** hex_len) ** depth
        if expected <= shards * MAX_CHUNKS_PER_SHARD:
            return depth
        depth += 1
    return depth


def chunk_ref(index: int) -> str:
    """청크의 저장 식별자 `<uuid32>_c<index:04d>`를 만든다.

    uuid는 청크마다 새로 뽑는다(파일 단위로 공유하지 않는다). 앞 32자가 hex UUID라
    orphan GC의 관리 파일 판정(`^[0-9a-f]{32}_`)과 호환된다.

    Raises:
        ValueError: index가 음수일 때.
    """
    if index < 0:
        raise ValueError("청크 인덱스는 0 이상이어야 합니다")
    return f"{uuid.uuid4().hex}_c{index:04d}"
