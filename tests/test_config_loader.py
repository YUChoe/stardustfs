"""ConfigLoader 단위 테스트."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from stardustlib.config_loader import ConfigLoader
from stardustlib.exceptions import InvalidKeyError, KeyNotFoundError


@pytest.fixture
def valid_config(tmp_path: Path) -> dict:
    """유효한 설정 딕셔너리를 반환한다."""
    source_dir = tmp_path / "storage"
    source_dir.mkdir()
    return {
        "version": 1,
        "webdav": {
            "host": "127.0.0.1",
            "port": 8080,
            "username": "admin",
            "password": "secret",
        },
        "sources": [
            {
                "type": "directory",
                "id": "dir-001",
                "path": str(source_dir),
            }
        ],
        "metadata_db": str(tmp_path / "meta.db"),
        "key_file": None,
    }


@pytest.fixture
def config_file(tmp_path: Path, valid_config: dict) -> Path:
    """유효한 설정 파일을 생성하여 경로를 반환한다."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(valid_config), encoding="utf-8")
    return cfg_path


class TestConfigLoaderLoad:
    """ConfigLoader.load() 테스트."""

    def test_load_valid_config(self, config_file: Path, valid_config: dict):
        loader = ConfigLoader(str(config_file))
        config = loader.load()
        assert config["version"] == 1
        assert config["webdav"]["host"] == "127.0.0.1"
        assert config["webdav"]["port"] == 8080
        assert len(config["sources"]) == 1

    def test_load_forces_host_to_localhost(self, tmp_path: Path, valid_config: dict):
        """webdav.host가 다른 값이어도 127.0.0.1로 강제된다."""
        valid_config["webdav"]["host"] = "0.0.0.0"
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(valid_config), encoding="utf-8")

        loader = ConfigLoader(str(cfg_path))
        config = loader.load()
        assert config["webdav"]["host"] == "127.0.0.1"

    def test_load_file_not_found(self):
        loader = ConfigLoader("/nonexistent/path/config.json")
        with pytest.raises(FileNotFoundError):
            loader.load()

    def test_load_invalid_json(self, tmp_path: Path):
        cfg_path = tmp_path / "bad.json"
        cfg_path.write_text("{ invalid json }", encoding="utf-8")
        loader = ConfigLoader(str(cfg_path))
        with pytest.raises(json.JSONDecodeError):
            loader.load()


class TestConfigLoaderValidate:
    """ConfigLoader.validate() 테스트."""

    def test_valid_config_no_errors(self, valid_config: dict):
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert errors == []

    def test_invalid_version(self, valid_config: dict):
        valid_config["version"] = 2
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("version" in e for e in errors)

    def test_empty_sources(self, valid_config: dict):
        valid_config["sources"] = []
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("sources" in e for e in errors)

    def test_directory_source_relative_path(self, valid_config: dict):
        valid_config["sources"] = [
            {"type": "directory", "id": "d1", "path": "relative/path"}
        ]
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("절대 경로" in e for e in errors)

    def test_loopback_size_too_small(self, valid_config: dict):
        valid_config["sources"] = [
            {"type": "loopback", "id": "l1", "path": "/tmp/loop.img", "size": 100}
        ]
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("size" in e for e in errors)

    def test_loopback_size_too_large(self, valid_config: dict):
        valid_config["sources"] = [
            {
                "type": "loopback",
                "id": "l1",
                "path": "/tmp/loop.img",
                "size": 3_000_000_000_000,
            }
        ]
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("size" in e for e in errors)

    def test_port_out_of_range(self, valid_config: dict):
        valid_config["webdav"]["port"] = 70000
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("port" in e for e in errors)

    def test_port_zero(self, valid_config: dict):
        valid_config["webdav"]["port"] = 0
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("port" in e for e in errors)

    def test_key_file_not_exists(self, valid_config: dict):
        valid_config["key_file"] = "/nonexistent/key.bin"
        loader = ConfigLoader("")
        errors = loader.validate(valid_config)
        assert any("key_file" in e for e in errors)


class TestLoadEncryptionKey:
    """ConfigLoader.load_encryption_key() 테스트."""

    def test_load_from_key_file(self, tmp_path: Path):
        key_data = os.urandom(32)
        key_path = tmp_path / "master.key"
        key_path.write_bytes(key_data)

        result = ConfigLoader.load_encryption_key(key_file=str(key_path))
        assert result == key_data

    def test_load_from_env_var(self, monkeypatch):
        key_data = "A" * 32
        monkeypatch.setenv("STARDUST_KEY", key_data)

        result = ConfigLoader.load_encryption_key()
        assert result == key_data.encode("utf-8")

    def test_key_file_priority_over_env(self, tmp_path: Path, monkeypatch):
        """키 파일이 환경변수보다 우선한다."""
        file_key = os.urandom(32)
        key_path = tmp_path / "master.key"
        key_path.write_bytes(file_key)

        monkeypatch.setenv("STARDUST_KEY", "B" * 32)

        result = ConfigLoader.load_encryption_key(key_file=str(key_path))
        assert result == file_key

    def test_key_not_found_raises(self, monkeypatch):
        monkeypatch.delenv("STARDUST_KEY", raising=False)
        with pytest.raises(KeyNotFoundError):
            ConfigLoader.load_encryption_key()

    def test_invalid_key_length_from_file(self, tmp_path: Path):
        key_path = tmp_path / "short.key"
        key_path.write_bytes(b"short")

        with pytest.raises(InvalidKeyError):
            ConfigLoader.load_encryption_key(key_file=str(key_path))

    def test_invalid_key_length_from_env(self, monkeypatch):
        monkeypatch.setenv("STARDUST_KEY", "too_short")
        with pytest.raises(InvalidKeyError):
            ConfigLoader.load_encryption_key()

    def test_key_file_not_found_raises(self):
        with pytest.raises(KeyNotFoundError):
            ConfigLoader.load_encryption_key(key_file="/nonexistent/key.bin")
