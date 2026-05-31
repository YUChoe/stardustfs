#!/usr/bin/env python3
"""KeyBackupEngine 및 P2P Path Traversal Property-Based Tests.

Property 5: Key 백업 라운드트립
Property 6: 잘못된 비밀번호로 복호화 실패
Property 7: Path Traversal 방지

주의: KeyBackupEngine은 PBKDF2 600,000 iterations를 사용하므로 1회 암복호화가
수백 ms 걸린다. Property 5/6의 max_examples를 낮게 유지한다.
"""

from __future__ import annotations

import os
import sys

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.exceptions import IntegrityError
from stardustlib.key_backup_engine import KeyBackupEngine


# ============================================================
# Property 5: Key 백업 라운드트립
# ============================================================
# 임의의 32바이트 master_key와 8자 이상 비밀번호로 encrypt→decrypt 시
# 원본 master_key가 정확히 복원되어야 한다.


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    master_key=st.binary(min_size=32, max_size=32),
    password=st.text(min_size=8, max_size=64),
)
def test_property5_key_backup_roundtrip(master_key, password):
    """encrypt_for_backup → decrypt_from_backup은 원본 master_key를 복원한다."""
    engine = KeyBackupEngine()
    blob = engine.encrypt_for_backup(master_key, password)
    restored = engine.decrypt_from_backup(blob, password)
    assert restored == master_key


@settings(max_examples=15, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    master_key=st.binary(min_size=32, max_size=32),
    password=st.text(min_size=8, max_size=32),
)
def test_property5_blob_structure(master_key, password):
    """백업 blob은 salt(16)+iv(12)+tag(16)+ciphertext 구조를 가진다."""
    engine = KeyBackupEngine()
    blob = engine.encrypt_for_backup(master_key, password)
    # 최소 크기: 16 + 12 + 16 + len(master_key)
    expected_min = (
        engine.SALT_SIZE + engine.IV_SIZE + engine.TAG_SIZE + len(master_key)
    )
    assert len(blob) == expected_min


# ============================================================
# Property 6: 잘못된 비밀번호로 복호화 실패
# ============================================================
# 올바른 비밀번호로 암호화한 blob을 다른 비밀번호로 복호화하면
# IntegrityError가 발생해야 한다.


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    master_key=st.binary(min_size=32, max_size=32),
    pw1=st.text(min_size=8, max_size=32),
    pw2=st.text(min_size=8, max_size=32),
)
def test_property6_wrong_password_fails(master_key, pw1, pw2):
    """다른 비밀번호로 복호화 시 IntegrityError가 발생한다."""
    if pw1 == pw2:
        return  # 같은 비밀번호는 성공해야 하므로 제외
    engine = KeyBackupEngine()
    blob = engine.encrypt_for_backup(master_key, pw1)
    try:
        engine.decrypt_from_backup(blob, pw2)
        raise AssertionError("잘못된 비밀번호인데 복호화가 성공함")
    except IntegrityError:
        pass  # 기대한 동작


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    master_key=st.binary(min_size=32, max_size=32),
    password=st.text(min_size=8, max_size=32),
    flip_index=st.integers(min_value=0, max_value=43),
)
def test_property6_tampered_blob_fails(master_key, password, flip_index):
    """blob의 임의 바이트를 변조하면 복호화가 실패한다."""
    engine = KeyBackupEngine()
    blob = bytearray(engine.encrypt_for_backup(master_key, password))
    idx = flip_index % len(blob)
    blob[idx] ^= 0xFF  # 1바이트 변조
    try:
        engine.decrypt_from_backup(bytes(blob), password)
        # salt 변조의 경우에도 파생 키가 달라져 tag 검증 실패해야 함
        raise AssertionError("변조된 blob인데 복호화가 성공함")
    except IntegrityError:
        pass


# ============================================================
# Property 7: Path Traversal 방지
# ============================================================
# ".." 세그먼트를 포함하거나 소스 루트를 벗어나는 경로는 거부되고,
# 루트 내부의 정상 경로는 통과해야 한다.


def _make_p2p_validator(root: str):
    """P2PServer._validate_path를 단독 호출하기 위한 경량 래퍼.

    P2PServer 전체 초기화 없이 _validate_path 로직만 검증한다.
    """
    from stardustlib.p2p_server import P2PServer

    server = P2PServer.__new__(P2PServer)
    # _source_root 프로퍼티가 참조하는 jbod_manager를 흉내내는 최소 객체
    class _FakeSource:
        path = root

    class _FakeJbod:
        sources = [_FakeSource()]

    server._jbod_manager = _FakeJbod()
    return server


@settings(max_examples=200)
@given(
    segments=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1, max_size=8,
        ),
        min_size=1, max_size=5,
    ),
)
def test_property7_safe_paths_pass(segments):
    """'..' 없는 루트 내부 상대 경로는 통과한다 (None 반환)."""
    with_root = os.path.normpath("/srv/data")
    server = _make_p2p_validator(with_root)
    rel = "/".join(segments)
    result = server._validate_path(rel)
    assert result is None


@settings(max_examples=200)
@given(
    prefix=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1, max_size=8,
        ),
        min_size=0, max_size=3,
    ),
    suffix=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1, max_size=8,
        ),
        min_size=0, max_size=3,
    ),
)
def test_property7_dotdot_paths_rejected(prefix, suffix):
    """'..' 세그먼트를 포함하는 경로는 거부된다 (Response 반환)."""
    server = _make_p2p_validator(os.path.normpath("/srv/data"))
    parts = prefix + [".."] + suffix
    path = "/".join(parts)
    result = server._validate_path(path)
    # None이 아니면 거부(에러 Response)
    assert result is not None


@settings(max_examples=100)
@given(depth=st.integers(min_value=1, max_value=8))
def test_property7_escape_root_rejected(depth):
    """연속된 '..'로 루트를 벗어나려는 시도는 거부된다."""
    server = _make_p2p_validator(os.path.normpath("/srv/data"))
    path = "/".join([".."] * depth) + "/etc/passwd"
    result = server._validate_path(path)
    assert result is not None
