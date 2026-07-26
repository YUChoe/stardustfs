"""chunker 단위/속성 테스트 (split/join 라운드트립)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stardustlib import chunker


@given(
    blob=st.binary(max_size=6000),
    size=st.integers(min_value=1, max_value=512),
)
def test_split_join_roundtrip(blob, size):
    assert chunker.join(chunker.split(blob, size)) == blob


def test_empty_blob():
    assert chunker.split(b"") == []
    assert chunker.join([]) == b""


def test_indices_and_boundaries():
    parts = chunker.split(b"abcdef", 2)
    assert [i for i, _ in parts] == [0, 1, 2]
    assert [d for _, d in parts] == [b"ab", b"cd", b"ef"]


def test_last_chunk_smaller():
    parts = chunker.split(b"abcde", 2)
    assert [d for _, d in parts] == [b"ab", b"cd", b"e"]


def test_join_unordered():
    assert chunker.join([(2, b"e"), (0, b"ab"), (1, b"cd")]) == b"abcde"


def test_size_zero_raises():
    with pytest.raises(ValueError):
        chunker.split(b"x", 0)


def test_chunk_count():
    assert chunker.chunk_count(0, 4) == 0
    assert chunker.chunk_count(4, 4) == 1
    assert chunker.chunk_count(5, 4) == 2
    assert chunker.chunk_count(9, 4) == 3


# ------------------------------------------------------------------
# 청크 배치·샤딩 헬퍼
# ------------------------------------------------------------------

def test_chunk_range_single_chunk():
    """한 청크 안에 들어가는 범위는 그 인덱스만 반환한다."""
    assert chunker.chunk_range(0, 10, 100) == [0]
    assert chunker.chunk_range(50, 50, 100) == [0]


def test_chunk_range_crosses_boundary():
    """청크 경계를 걸치는 범위는 관련 인덱스를 모두 반환한다."""
    assert chunker.chunk_range(95, 10, 100) == [0, 1]
    assert chunker.chunk_range(0, 250, 100) == [0, 1, 2]
    assert chunker.chunk_range(100, 100, 100) == [1]


def test_chunk_range_zero_length_is_empty():
    """길이 0은 가져올 청크가 없다."""
    assert chunker.chunk_range(0, 0, 100) == []
    assert chunker.chunk_range(500, 0, 100) == []


def test_chunk_range_rejects_invalid_args():
    """음수 인자와 0 이하 청크 크기는 규격 에러."""
    with pytest.raises(ValueError):
        chunker.chunk_range(-1, 10, 100)
    with pytest.raises(ValueError):
        chunker.chunk_range(0, -1, 100)
    with pytest.raises(ValueError):
        chunker.chunk_range(0, 10, 0)


def test_chunk_range_matches_split_indices():
    """chunk_range(0, len)은 split이 만드는 인덱스 전체와 일치한다."""
    blob = b"x" * 1050
    expected = [idx for idx, _part in chunker.split(blob, 100)]
    assert chunker.chunk_range(0, len(blob), 100) == expected


def test_shard_prefix_takes_leading_hex():
    """샤드 이름은 해시 앞 hex_len자다."""
    digest = chunker.chunk_hash(b"data")
    assert chunker.shard_prefix(digest) == digest[:2]
    assert chunker.shard_prefix(digest, 3) == digest[:3]


def test_shard_prefix_rejects_invalid():
    """hex_len이 0 이하이거나 해시가 짧으면 규격 에러."""
    with pytest.raises(ValueError):
        chunker.shard_prefix("abcdef", 0)
    with pytest.raises(ValueError):
        chunker.shard_prefix("a", 2)


def test_shard_prefix_distributes_by_content():
    """서로 다른 청크 내용은 여러 샤드로 흩어진다."""
    shards = {
        chunker.shard_prefix(chunker.chunk_hash(bytes([i]) * 16))
        for i in range(64)
    }
    assert len(shards) > 1


def test_chunk_ref_format_and_uniqueness():
    """chunk_ref는 <uuid32>_c<4자리 인덱스>이고 매번 새 UUID를 쓴다."""
    ref = chunker.chunk_ref(7)
    head, _, tail = ref.partition("_")
    assert len(head) == 32
    int(head, 16)                    # hex UUID
    assert tail == "c0007"
    # 같은 인덱스라도 참조는 매번 다르다(파일 단위 UUID 공유 금지).
    assert chunker.chunk_ref(7) != ref


def test_chunk_ref_matches_managed_file_pattern():
    """orphan GC의 관리 파일 판정(^[0-9a-f]{32}_)과 호환된다."""
    import re

    assert re.match(r"^[0-9a-f]{32}_", chunker.chunk_ref(0))


def test_chunk_ref_rejects_negative_index():
    """음수 인덱스는 규격 에러."""
    with pytest.raises(ValueError):
        chunker.chunk_ref(-1)
