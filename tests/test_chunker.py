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
