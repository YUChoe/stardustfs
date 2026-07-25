#!/usr/bin/env python3
"""자동 디바이스 발견(remote 소스 자동 마운트) 단위 테스트.

stardustfs._mount_remote_sources의 마운트 대상 선정 로직을 검증한다:
- config에 명시된 remote 소스
- 자동 발견: 내 다른 디바이스(my_devices)에서 자기 자신/중복 제외
- p2p.auto_mount_devices=false면 자동 발견 비활성

RemoteSource는 네트워크를 타므로 가짜로 패치하여 (source_id, device_id) 쌍만 캡처한다.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stardustfs


class _FakeRemoteSource:
    """RemoteSource 대역. initialize는 no-op, source_id만 보유."""

    def __init__(self, source_id, device_id, auth_client, server_url):
        self.source_id = source_id
        self.device_id = device_id
        self._active = False

    def initialize(self):
        self._active = True

    @property
    def is_active(self):
        return self._active


class _FakeStoragePool:
    def __init__(self):
        self.added = []
        self.registered = {}

    def add_source(self, source):
        self.added.append(source)

    def register_remote_device(self, device_id, remote):
        self.registered[device_id] = remote


def _mounted_pairs(storage_pool):
    """마운트된 (source_id, device_id) 집합."""
    return {(s.source_id, s.device_id) for s in storage_pool.added}


def test_auto_mount_excludes_self_and_mounts_others():
    """자동 발견은 자기 자신을 빼고 다른 디바이스를 마운트한다."""
    config = {"sources": [], "p2p": {"auto_mount_devices": True}}
    storage_pool = _FakeStoragePool()
    my_devices = [
        {"id": "dev-self", "name": "PC-A"},
        {"id": "dev-b", "name": "PC-B"},
        {"id": "dev-c", "name": "PC-C"},
    ]

    with patch("stardustlib.remote_source.RemoteSource", _FakeRemoteSource):
        stardustfs._mount_remote_sources(
            config, storage_pool, auth_client=None, server_url="http://x",
            my_devices=my_devices, self_device_id="dev-self",
        )

    pairs = _mounted_pairs(storage_pool)
    assert ("remote-dev-b", "dev-b") in pairs
    assert ("remote-dev-c", "dev-c") in pairs
    # 자기 자신은 마운트 안 됨
    assert not any(d == "dev-self" for _s, d in pairs)


def test_auto_mount_disabled():
    """auto_mount_devices=false면 자동 발견하지 않는다."""
    config = {"sources": [], "p2p": {"auto_mount_devices": False}}
    storage_pool = _FakeStoragePool()
    my_devices = [{"id": "dev-self"}, {"id": "dev-b"}]

    with patch("stardustlib.remote_source.RemoteSource", _FakeRemoteSource):
        stardustfs._mount_remote_sources(
            config, storage_pool, auth_client=None, server_url="http://x",
            my_devices=my_devices, self_device_id="dev-self",
        )

    assert storage_pool.added == []


def test_config_explicit_takes_priority_no_duplicate():
    """config에 명시된 device_id는 자동 발견에서 중복 마운트되지 않는다."""
    config = {
        "sources": [
            {"type": "remote", "id": "my-laptop", "device_id": "dev-b"},
        ],
        "p2p": {"auto_mount_devices": True},
    }
    storage_pool = _FakeStoragePool()
    my_devices = [{"id": "dev-self"}, {"id": "dev-b"}, {"id": "dev-c"}]

    with patch("stardustlib.remote_source.RemoteSource", _FakeRemoteSource):
        stardustfs._mount_remote_sources(
            config, storage_pool, auth_client=None, server_url="http://x",
            my_devices=my_devices, self_device_id="dev-self",
        )

    pairs = _mounted_pairs(storage_pool)
    # config 명시 소스는 그 id로 마운트
    assert ("my-laptop", "dev-b") in pairs
    # dev-b는 자동 발견에서 중복 마운트 안 됨 (remote-dev-b 없음)
    assert ("remote-dev-b", "dev-b") not in pairs
    # dev-c는 자동 발견됨
    assert ("remote-dev-c", "dev-c") in pairs


def test_auto_mount_default_true_when_no_p2p_config():
    """p2p 설정이 없어도 자동 발견은 기본 활성(True)이다."""
    config = {"sources": []}  # p2p 키 없음
    storage_pool = _FakeStoragePool()
    my_devices = [{"id": "dev-self"}, {"id": "dev-b"}]

    with patch("stardustlib.remote_source.RemoteSource", _FakeRemoteSource):
        stardustfs._mount_remote_sources(
            config, storage_pool, auth_client=None, server_url="http://x",
            my_devices=my_devices, self_device_id="dev-self",
        )

    pairs = _mounted_pairs(storage_pool)
    assert ("remote-dev-b", "dev-b") in pairs


def test_no_my_devices_only_config_sources():
    """my_devices가 없으면 config 명시 소스만 마운트한다."""
    config = {
        "sources": [
            {"type": "remote", "id": "r1", "device_id": "dev-x"},
        ],
        "p2p": {"auto_mount_devices": True},
    }
    storage_pool = _FakeStoragePool()

    with patch("stardustlib.remote_source.RemoteSource", _FakeRemoteSource):
        stardustfs._mount_remote_sources(
            config, storage_pool, auth_client=None, server_url="http://x",
            my_devices=None, self_device_id="dev-self",
        )

    pairs = _mounted_pairs(storage_pool)
    assert pairs == {("r1", "dev-x")}
