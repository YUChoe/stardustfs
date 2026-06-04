#!/usr/bin/env python3
"""병합/충돌 관련 Property-Based Tests (hypothesis).

Property 3: 메타데이터 병합 정확성 (4가지 경우의 상호 배타성·완전성)
Property 4: 충돌 파일명 형식 및 고유성
"""

from __future__ import annotations

import os
import sys
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.conflict_resolver import ConflictResolver
from stardustlib.metadata_store import MetadataStore


def _classify_merge(server_v: int, local_v: int, base_v: int) -> str:
    """design.md의 병합 규칙을 그대로 구현한 참조 분류기.

    네 가지 경우를 상호 배타적으로 분류한다.
    """
    if server_v > base_v and local_v > base_v:
        return "conflict"
    if server_v > local_v:
        return "update_from_server"
    if local_v > server_v:
        return "keep_local"
    return "no_change"  # server_v == local_v


# ============================================================
# Property 3: 메타데이터 병합 정확성
# ============================================================


@settings(max_examples=500)
@given(
    server_v=st.integers(min_value=0, max_value=50),
    local_v=st.integers(min_value=0, max_value=50),
    base_v=st.integers(min_value=0, max_value=50),
)
def test_property3_merge_cases_mutually_exclusive_and_complete(
    server_v, local_v, base_v
):
    """모든 version 조합은 정확히 하나의 병합 경우로 분류된다 (완전성+상호배타성)."""
    category = _classify_merge(server_v, local_v, base_v)
    assert category in {
        "conflict", "update_from_server", "keep_local", "no_change"
    }

    # 분류가 결정적(deterministic)이며 단일하다 — 같은 입력은 같은 결과
    assert category == _classify_merge(server_v, local_v, base_v)


def _make_store(d: str) -> MetadataStore:
    """고유 디렉토리 d에 MetadataStore를 생성·초기화한다."""
    store = MetadataStore(os.path.join(d, "m.db"), b"\x00" * 32)
    store.initialize()
    return store


@settings(max_examples=500)
@given(
    server_v=st.integers(min_value=0, max_value=50),
    local_v=st.integers(min_value=0, max_value=50),
    base_v=st.integers(min_value=0, max_value=50),
)
def test_property3_detect_conflict_matches_rule(server_v, local_v, base_v):
    """ConflictResolver.detect_conflict가 충돌 규칙과 정확히 일치한다."""
    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        resolver = ConflictResolver(store, "dev")
        detected = resolver.detect_conflict(
            "/f.txt", server_v, local_v, base_v
        )
        expected = (server_v > base_v and local_v > base_v)
        assert detected == expected
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


@settings(max_examples=300)
@given(
    server_v=st.integers(min_value=0, max_value=50),
    local_v=st.integers(min_value=0, max_value=50),
    base_v=st.integers(min_value=0, max_value=50),
)
def test_property3_conflict_implies_both_advanced(server_v, local_v, base_v):
    """충돌로 분류되면 server_v와 local_v 모두 base_v보다 크다 (불변식)."""
    if _classify_merge(server_v, local_v, base_v) == "conflict":
        assert server_v > base_v
        assert local_v > base_v


# ============================================================
# Property 4: 충돌 파일명 형식 및 고유성
# ============================================================

_CONFLICT_MARKER = "(conflict - "


@settings(max_examples=200)
@given(
    dir_seg=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=0, max_size=10,
    ),
    name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1, max_size=15,
    ),
    ext=st.sampled_from(["", ".txt", ".bin", ".gz"]),
    device=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1, max_size=12,
    ),
)
def test_property4_conflict_name_format_and_extension(
    dir_seg, name, ext, device
):
    """conflict 파일명은 형식을 준수하고 (마지막) 확장자를 보존한다.

    generate_conflict_name은 rfind('.')로 마지막 확장자만 분리하므로
    단일 확장자 케이스로 검증한다.
    """
    vpath = f"/{dir_seg}/{name}{ext}" if dir_seg else f"/{name}{ext}"
    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        resolver = ConflictResolver(store, device)
        conflict_path = resolver.generate_conflict_name(vpath)

        # conflict 마커와 device 이름 포함
        assert _CONFLICT_MARKER in conflict_path
        assert device in conflict_path
        # 원본 확장자 보존 (확장자가 있는 경우)
        if ext:
            assert conflict_path.endswith(ext)
        # 같은 디렉토리에 생성됨
        import posixpath
        assert posixpath.dirname(conflict_path) == posixpath.dirname(vpath)
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


@settings(max_examples=100)
@given(
    name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1, max_size=15,
    ),
    ext=st.sampled_from(["", ".txt", ".dat"]),
)
def test_property4_conflict_name_uniqueness(name, ext):
    """이미 conflict copy가 존재하면 순번을 붙여 고유한 이름을 생성한다."""
    import time
    vpath = f"/{name}{ext}"
    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        resolver = ConflictResolver(store, "dev")
        # 첫 conflict 이름 생성 후 실제로 등록 (점유)
        first = resolver.generate_conflict_name(vpath)
        store.insert(first, "vol", "phys", 1, time.time(), time.time())

        # 두 번째 호출은 첫 번째와 달라야 함 (고유성)
        second = resolver.generate_conflict_name(vpath)
        assert second != first
        assert store.lookup(second) is None
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)
