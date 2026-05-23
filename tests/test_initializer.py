"""stardustlib.initializer 단위 테스트."""

import json
import os

import pytest

from stardustlib.initializer import _derive_db_key, initialize_system


class TestDeriveDbKey:
    """_derive_db_key HKDF 파생 테스트."""

    def test_returns_32_bytes(self):
        """마스터 키에서 32바이트 DB 키를 파생한다."""
        master_key = os.urandom(32)
        db_key = _derive_db_key(master_key)
        assert len(db_key) == 32

    def test_deterministic(self):
        """동일 마스터 키에서 동일 DB 키가 파생된다."""
        master_key = os.urandom(32)
        key1 = _derive_db_key(master_key)
        key2 = _derive_db_key(master_key)
        assert key1 == key2

    def test_different_master_keys_produce_different_db_keys(self):
        """다른 마스터 키에서 다른 DB 키가 파생된다."""
        key1 = _derive_db_key(os.urandom(32))
        key2 = _derive_db_key(os.urandom(32))
        assert key1 != key2

    def test_db_key_differs_from_master_key(self):
        """파생된 DB 키는 마스터 키와 다르다."""
        master_key = os.urandom(32)
        db_key = _derive_db_key(master_key)
        assert db_key != master_key


class TestInitializeSystem:
    """initialize_system 통합 테스트."""

    @pytest.fixture(autouse=True)
    def _tmp_dir(self, tmp_path):
        """각 테스트에 임시 디렉토리를 제공한다 (pytest tmp_path 사용)."""
        self.tmp_dir = str(tmp_path)

    def _write_config(self, sources: list | None = None) -> str:
        """임시 설정 파일을 생성하고 경로를 반환한다."""
        storage_dir = os.path.join(self.tmp_dir, "storage")
        os.makedirs(storage_dir, exist_ok=True)

        if sources is None:
            sources = [
                {"type": "directory", "id": "dir-001", "path": storage_dir}
            ]

        config = {
            "version": 1,
            "webdav": {
                "host": "127.0.0.1",
                "port": 8080,
                "username": "admin",
                "password": "test_pass",
            },
            "sources": sources,
            "metadata_db": os.path.join(self.tmp_dir, "metadata.db"),
            "key_file": os.path.join(self.tmp_dir, "master.key"),
        }

        # 키 파일 생성 (32바이트)
        key_path = config["key_file"]
        with open(key_path, "wb") as f:
            f.write(os.urandom(32))

        config_path = os.path.join(self.tmp_dir, "stardustfs.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        return config_path

    def test_successful_initialization(self):
        """유효한 설정으로 정상 초기화 시 (app, config) 튜플을 반환한다."""
        config_path = self._write_config()
        app, config = initialize_system(config_path)

        assert app is not None
        assert callable(app)
        assert config["version"] == 1

    def test_missing_config_file_exits(self):
        """설정 파일이 없으면 sys.exit(1)을 호출한다."""
        with pytest.raises(SystemExit) as exc_info:
            initialize_system("/nonexistent/path/config.json")
        assert exc_info.value.code == 1

    def test_invalid_json_exits(self):
        """잘못된 JSON 설정 파일이면 sys.exit(1)을 호출한다."""
        config_path = os.path.join(self.tmp_dir, "bad.json")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("{invalid json content")

        with pytest.raises(SystemExit) as exc_info:
            initialize_system(config_path)
        assert exc_info.value.code == 1

    def test_missing_key_file_exits(self):
        """키 파일이 없으면 sys.exit(1)을 호출한다."""
        storage_dir = os.path.join(self.tmp_dir, "storage")
        os.makedirs(storage_dir, exist_ok=True)

        config = {
            "version": 1,
            "webdav": {
                "host": "127.0.0.1",
                "port": 8080,
                "username": "admin",
                "password": "pass",
            },
            "sources": [
                {"type": "directory", "id": "d1", "path": storage_dir}
            ],
            "metadata_db": os.path.join(self.tmp_dir, "meta.db"),
            "key_file": os.path.join(self.tmp_dir, "nonexistent.key"),
        }

        config_path = os.path.join(self.tmp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        with pytest.raises(SystemExit) as exc_info:
            initialize_system(config_path)
        assert exc_info.value.code == 1

    def test_invalid_source_path_exits(self):
        """존재하지 않는 소스 경로가 있으면 sys.exit(1)을 호출한다."""
        sources = [
            {
                "type": "directory",
                "id": "bad-dir",
                "path": "/nonexistent/path/xyz",
            }
        ]
        # 키 파일 생성
        key_path = os.path.join(self.tmp_dir, "master.key")
        with open(key_path, "wb") as f:
            f.write(os.urandom(32))

        config = {
            "version": 1,
            "webdav": {
                "host": "127.0.0.1",
                "port": 8080,
                "username": "admin",
                "password": "pass",
            },
            "sources": sources,
            "metadata_db": os.path.join(self.tmp_dir, "meta.db"),
            "key_file": key_path,
        }

        config_path = os.path.join(self.tmp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        with pytest.raises(SystemExit) as exc_info:
            initialize_system(config_path)
        assert exc_info.value.code == 1

    def test_log_message_on_success(self, caplog):
        """성공 시 'StardustFS 준비 완료' 로그를 기록한다."""
        config_path = self._write_config()

        import logging
        with caplog.at_level(logging.INFO):
            app, _ = initialize_system(config_path)

        assert any(
            "StardustFS 준비 완료" in record.message
            for record in caplog.records
        )
