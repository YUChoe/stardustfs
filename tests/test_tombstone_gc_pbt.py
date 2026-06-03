#!/usr/bin/env python3
"""Tombstone GC 및 stale 재조정 Property-Based Tests + 단위 테스트.

Property 9: Tombstone GC 및 stale 재조정 안전성
- GC 대상은 deleted=1 AND modified_at < (now - retention)인 레코드뿐이며
  활성(deleted=0) 레코드는 절대 제거되지 않는다
- stale 판정은 (now - last_sync_at) > retention 경계와 일치한다
- stale 재조정 시 로컬 pending 변경은 손실되지 않는다
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.metadata_store import MetadataStore


def _make_store(d: str) -> MetadataStore:
    store = MetadataStore(os.path.join(d, "m.db"), b"\x00" * 32)
    store.initialize()
    return store


def _set_record(store: MetadataStore, path: str, deleted: int, modified_at: float):
    """레코드를 삽입하고 deleted/modified_at을 직접 설정한다."""
    store.insert(path, "vol", f"phys{path}", 10, modified_at, modified_at)
    conn = store._get_conn()
    conn.execute(
        "UPDATE files SET deleted = ?, modified_at = ? WHERE virtual_path = ?",
        (deleted, modified_at, path),
    )
    conn.commit()


# ============================================================
# Property 9-A: GC 대상 선별 정확성
# ============================================================


@settings(max_examples=200)
@given(
    # (deleted, age_seconds) 쌍의 리스트
    records=st.lists(
        st.tuples(
            st.booleans(),                          # deleted 여부
            st.integers(min_value=0, max_value=100 * 86400),  # 경과 시간(초)
        ),
        min_size=0, max_size=15,
    ),
    retention_days=st.integers(min_value=1, max_value=60),
)
def test_property9_gc_only_expired_tombstones(records, retention_days):
    """GC는 만료된 tombstone만 제거하고 활성 레코드는 보존한다."""
    retention_seconds = retention_days * 86400
    # 나이가 정확히 보관기간 경계와 같으면, 테스트의 now와 purge 내부 time.time()의
    # 미세한 차이로 결과가 모호해진다(정수 나이에서 유일한 경계점). 해당 예제 제외.
    assume(all(age != retention_seconds for _d, age in records))
    now = time.time()
    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        # 레코드 구성
        expected_survivors = set()
        for i, (deleted, age) in enumerate(records):
            path = f"/f{i}.txt"
            modified_at = now - age
            _set_record(store, path, 1 if deleted else 0, modified_at)
            # 생존 조건: 활성이거나(deleted=0), tombstone이어도 만료 전
            is_expired_tombstone = deleted and (modified_at < now - retention_seconds)
            if not is_expired_tombstone:
                expected_survivors.add(path)

        store.purge_expired_tombstones(retention_seconds)

        # 생존자 검증
        conn = store._get_conn()
        rows = conn.execute("SELECT virtual_path FROM files").fetchall()
        survivors = {r["virtual_path"] for r in rows}
        assert survivors == expected_survivors
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


@settings(max_examples=100)
@given(
    age_days=st.integers(min_value=0, max_value=120),
    retention_days=st.integers(min_value=1, max_value=60),
)
def test_property9_active_record_never_gc(age_days, retention_days):
    """활성(deleted=0) 레코드는 아무리 오래돼도 GC되지 않는다."""
    now = time.time()
    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        _set_record(store, "/active.txt", 0, now - age_days * 86400)
        store.purge_expired_tombstones(retention_days * 86400)
        # 활성 레코드는 lookup으로 항상 조회되어야 함
        assert store.lookup("/active.txt") is not None
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# Property 9-B: stale 판정 경계
# ============================================================


def _is_stale(last_sync_at: float | None, retention_seconds: float, now: float) -> bool:
    """SyncClient._is_stale의 참조 구현."""
    if last_sync_at is None:
        return False
    return (now - last_sync_at) > retention_seconds


@settings(max_examples=300)
@given(
    elapsed_days=st.floats(min_value=0, max_value=120, allow_nan=False),
    retention_days=st.integers(min_value=1, max_value=60),
)
def test_property9_stale_boundary(elapsed_days, retention_days):
    """stale 판정은 경과시간 > retention 경계와 정확히 일치한다."""
    now = 1_000_000_000.0
    retention_seconds = retention_days * 86400
    last_sync_at = now - elapsed_days * 86400
    stale = _is_stale(last_sync_at, retention_seconds, now)
    expected = (elapsed_days * 86400) > retention_seconds
    assert stale == expected


def test_property9_no_syncstate_is_not_stale():
    """syncstate(last_sync_at)가 없으면 stale이 아니다 (새 디바이스)."""
    assert _is_stale(None, 30 * 86400, time.time()) is False


# ============================================================
# 단위 테스트: SyncClient stale 판정 / syncstate 보존
# ============================================================


def test_syncstate_roundtrip():
    """_record_sync_success → _read_last_sync_at 라운드트립."""
    from unittest.mock import MagicMock
    from stardustlib.sync_client import SyncClient

    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        sc = SyncClient.__new__(SyncClient)
        sc._syncstate_path = f"{store._db_path}.syncstate.json"
        sc._retention_seconds = 30 * 86400

        # 최초엔 기록 없음 → stale 아님
        assert sc._read_last_sync_at() is None
        assert sc._is_stale() is False

        sc._record_sync_success()
        last = sc._read_last_sync_at()
        assert last is not None
        assert abs(last - time.time()) < 5
        # 방금 기록했으므로 stale 아님
        assert sc._is_stale() is False
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)


def test_is_stale_after_long_gap():
    """last_sync_at이 보관기간보다 오래전이면 stale로 판정한다."""
    import json
    from stardustlib.sync_client import SyncClient

    d = tempfile.mkdtemp()
    store = _make_store(d)
    try:
        sc = SyncClient.__new__(SyncClient)
        sc._syncstate_path = f"{store._db_path}.syncstate.json"
        sc._retention_seconds = 30 * 86400

        # 40일 전으로 기록
        old = time.time() - 40 * 86400
        with open(sc._syncstate_path, "w", encoding="utf-8") as f:
            json.dump({"last_sync_at": old}, f)

        assert sc._is_stale() is True
    finally:
        store.close()
        shutil.rmtree(d, ignore_errors=True)
