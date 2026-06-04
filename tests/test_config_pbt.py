#!/usr/bin/env python3
"""ConfigLoader 관련 Property-Based Tests (hypothesis).

Property 1: v2 설정 검증 일관성
Property 2: v1→v2 마이그레이션 필드 보존
Property 8: 백업 파일명 고유성
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stardustlib.config_loader import (
    MAX_DEVICE_NAME_LEN,
    MAX_P2P_PORT,
    MAX_SYNC_INTERVAL,
    MIN_DEVICE_NAME_LEN,
    MIN_P2P_PORT,
    MIN_SYNC_INTERVAL,
    ConfigLoader,
)


def _make_loader() -> ConfigLoader:
    """임의 경로의 ConfigLoader (검증 전용, 파일 접근 없음)."""
    return ConfigLoader("dummy-config.json")


def _base_v2_config() -> dict:
    """검증을 통과하는 최소 v2 설정 골격.

    sources는 remote(파일시스템 접근 불필요) 하나로 구성하여
    경로 존재 여부에 의존하지 않게 한다.
    """
    return {
        "version": 2,
        "server": {"url": None, "device_name": "dev"},
        "sync": {"interval_seconds": 30, "conflict_strategy": "copy"},
        "p2p": {"port": 9090, "enabled": False},
        "sources": [
            {"type": "remote",
             "id": "r1",
             "device_id": "550e8400-e29b-41d4-a716-446655440000"}
        ],
        "metadata_db": "meta.db",
        "key_file": None,
    }


# ============================================================
# Property 1: v2 설정 검증 일관성
# ============================================================
# 임의의 v2 설정에서 특정 필드를 유효/무효 값으로 설정했을 때
# 검증 결과(에러 유무)가 일관되게 나와야 한다.


@settings(max_examples=200)
@given(interval=st.integers())
def test_property1_sync_interval_validation_consistency(interval):
    """sync.interval_seconds 검증은 범위 [MIN, MAX] 여부와 정확히 일치한다."""
    config = _base_v2_config()
    config["sync"]["interval_seconds"] = interval
    errors = _make_loader().validate(config)

    has_interval_error = any("interval_seconds" in e for e in errors)
    in_range = MIN_SYNC_INTERVAL <= interval <= MAX_SYNC_INTERVAL
    assert has_interval_error == (not in_range)


@settings(max_examples=200)
@given(port=st.integers())
def test_property1_p2p_port_validation_consistency(port):
    """p2p.port 검증은 범위 [MIN, MAX] 여부와 정확히 일치한다."""
    config = _base_v2_config()
    config["p2p"]["port"] = port
    errors = _make_loader().validate(config)

    has_port_error = any(e.startswith("p2p.port") for e in errors)
    in_range = MIN_P2P_PORT <= port <= MAX_P2P_PORT
    assert has_port_error == (not in_range)


@settings(max_examples=200)
@given(name=st.text())
def test_property1_device_name_validation_consistency(name):
    """server.device_name 검증은 길이 [MIN, MAX] 여부와 정확히 일치한다."""
    config = _base_v2_config()
    config["server"]["device_name"] = name
    errors = _make_loader().validate(config)

    has_name_error = any("device_name" in e for e in errors)
    in_range = MIN_DEVICE_NAME_LEN <= len(name) <= MAX_DEVICE_NAME_LEN
    assert has_name_error == (not in_range)


@settings(max_examples=200)
@given(enabled=st.one_of(st.booleans(), st.integers(), st.text(), st.none()))
def test_property1_p2p_enabled_must_be_bool(enabled):
    """p2p.enabled는 boolean일 때만 통과한다."""
    config = _base_v2_config()
    config["p2p"]["enabled"] = enabled
    errors = _make_loader().validate(config)

    has_enabled_error = any(e.startswith("p2p.enabled") for e in errors)
    assert has_enabled_error == (not isinstance(enabled, bool))


@settings(max_examples=200)
@given(device_id=st.text())
def test_property1_remote_device_id_uuid_validation(device_id):
    """remote source의 device_id는 UUID 형식일 때만 통과한다."""
    import re
    config = _base_v2_config()
    config["sources"][0]["device_id"] = device_id
    errors = _make_loader().validate(config)

    has_uuid_error = any("device_id" in e for e in errors)
    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )
    is_valid_uuid = bool(uuid_re.match(device_id))
    assert has_uuid_error == (not is_valid_uuid)


# ============================================================
# Property 2: v1→v2 마이그레이션 필드 보존
# ============================================================
# 임의의 유효한 v1 설정을 마이그레이션하면
# (1) version==2, (2) 원본 필드 보존, (3) server/sync/p2p 기본값 추가.


@settings(max_examples=100)
@given(
    port=st.integers(min_value=1, max_value=65535),
    db_name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1, max_size=20,
    ),
    username=st.text(min_size=1, max_size=20),
)
def test_property2_migration_preserves_fields(port, db_name, username):
    """v1→v2 마이그레이션 후 원본 필드가 보존되고 기본 섹션이 추가된다."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path = os.path.join(d, "config.json")
        v1 = {
            "version": 1,
            "webdav": {"host": "127.0.0.1", "port": port,
                       "username": username, "password": "pw"},
            "sources": [
                {"type": "loopback", "id": "v1",
                 "path": "/abs/vol.img", "size": 10_485_760}
            ],
            "metadata_db": f"{db_name}.db",
            "key_file": None,
        }
        Path(cfg_path).write_text(json.dumps(v1), encoding="utf-8")

        loader = ConfigLoader(cfg_path)
        v2 = loader.migrate_v1_to_v2(v1)

        # (1) version == 2
        assert v2["version"] == 2
        # (2) 원본 필드 보존 (레거시 webdav는 제거됨)
        assert "webdav" not in v2
        assert v2["sources"] == v1["sources"]
        assert v2["metadata_db"] == v1["metadata_db"]
        assert v2["key_file"] == v1["key_file"]
        # (3) 기본 섹션 추가
        assert "server" in v2
        assert "sync" in v2 and v2["sync"]["interval_seconds"] == 30
        assert "p2p" in v2 and v2["p2p"]["port"] == 9090
        # 백업 파일 생성 확인
        assert Path(cfg_path + ".v1.bak").exists()


# ============================================================
# Property 8: 백업 파일명 고유성
# ============================================================
# 기존 백업 파일이 N개 있을 때, _resolve_backup_path는
# 아직 존재하지 않는 고유한 경로를 반환해야 한다.


@settings(max_examples=100)
@given(existing_count=st.integers(min_value=0, max_value=20))
def test_property8_backup_name_uniqueness(existing_count):
    """기존 백업 N개가 있어도 충돌하지 않는 고유 경로를 반환한다."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(os.path.join(d, "config.json"))
        cfg_path.write_text("{}", encoding="utf-8")

        base = str(cfg_path) + ".v1.bak"
        # 기존 백업 파일들을 미리 생성
        if existing_count >= 1:
            Path(base).write_text("bak", encoding="utf-8")
        for n in range(1, existing_count):
            Path(f"{base}.{n}").write_text("bak", encoding="utf-8")

        resolved = ConfigLoader._resolve_backup_path(cfg_path)

        # 반환된 경로는 아직 존재하지 않아야 함 (고유성)
        assert not resolved.exists()
        # 반환 경로는 백업 접두사로 시작해야 함
        assert str(resolved).startswith(base)
