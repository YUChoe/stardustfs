"""ConfigLoader.migrate_v1_to_v2() 단위 테스트."""

import json
from pathlib import Path

import pytest

from stardustlib.config_loader import ConfigLoader
from stardustlib.exceptions import ConfigMigrationError


@pytest.fixture
def v1_config(tmp_path: Path) -> dict:
    """유효한 v1 설정 딕셔너리."""
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
        "key_file": str(tmp_path / "master.key"),
    }


@pytest.fixture
def config_file(tmp_path: Path, v1_config: dict) -> Path:
    """v1 설정 파일 생성."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(v1_config, indent=2), encoding="utf-8"
    )
    return cfg_path


class TestMigrateV1ToV2:
    """migrate_v1_to_v2() 테스트."""

    def test_migration_produces_v2(self, config_file: Path, v1_config: dict):
        """마이그레이션 후 version이 2이다."""
        loader = ConfigLoader(str(config_file))
        result = loader.migrate_v1_to_v2(v1_config)
        assert result["version"] == 2

    def test_preserves_existing_fields(
        self, config_file: Path, v1_config: dict
    ):
        """기존 sources, metadata_db, key_file은 보존되고 레거시 webdav는 제거된다."""
        loader = ConfigLoader(str(config_file))
        result = loader.migrate_v1_to_v2(v1_config)

        assert "webdav" not in result
        assert result["sources"] == v1_config["sources"]
        assert result["metadata_db"] == v1_config["metadata_db"]
        assert result["key_file"] == v1_config["key_file"]

    def test_adds_server_defaults(
        self, config_file: Path, v1_config: dict
    ):
        """server 섹션이 url: null로 추가된다."""
        loader = ConfigLoader(str(config_file))
        result = loader.migrate_v1_to_v2(v1_config)
        assert result["server"] == {"url": None}

    def test_adds_sync_defaults(
        self, config_file: Path, v1_config: dict
    ):
        """sync 섹션이 기본값으로 추가된다."""
        loader = ConfigLoader(str(config_file))
        result = loader.migrate_v1_to_v2(v1_config)
        assert result["sync"] == {
            "interval_seconds": 30,
            "conflict_strategy": "copy",
        }

    def test_adds_p2p_defaults(
        self, config_file: Path, v1_config: dict
    ):
        """p2p 섹션이 기본값으로 추가된다."""
        loader = ConfigLoader(str(config_file))
        result = loader.migrate_v1_to_v2(v1_config)
        assert result["p2p"] == {"port": 9090, "enabled": False}

    def test_creates_backup_file(
        self, config_file: Path, v1_config: dict
    ):
        """원본 파일이 .v1.bak으로 백업된다."""
        loader = ConfigLoader(str(config_file))
        loader.migrate_v1_to_v2(v1_config)

        backup = Path(str(config_file) + ".v1.bak")
        assert backup.exists()
        # 백업 내용이 원본과 동일
        backup_data = json.loads(backup.read_text(encoding="utf-8"))
        assert backup_data["version"] == 1

    def test_saves_v2_to_original_path(
        self, config_file: Path, v1_config: dict
    ):
        """변환된 v2 설정이 원본 경로에 저장된다."""
        loader = ConfigLoader(str(config_file))
        loader.migrate_v1_to_v2(v1_config)

        saved = json.loads(config_file.read_text(encoding="utf-8"))
        assert saved["version"] == 2
        assert saved["server"] == {"url": None}

    def test_backup_numbering_when_exists(
        self, config_file: Path, v1_config: dict
    ):
        """기존 백업 존재 시 순번이 부여된다."""
        # 기존 백업 생성
        backup_base = Path(str(config_file) + ".v1.bak")
        backup_base.write_text("existing backup", encoding="utf-8")

        loader = ConfigLoader(str(config_file))
        loader.migrate_v1_to_v2(v1_config)

        # .v1.bak.1이 생성되어야 함
        numbered = Path(f"{backup_base}.1")
        assert numbered.exists()
        # 기존 백업은 그대로
        assert backup_base.read_text(encoding="utf-8") == "existing backup"

    def test_backup_numbering_increments(
        self, config_file: Path, v1_config: dict
    ):
        """여러 백업 존재 시 다음 순번이 부여된다."""
        backup_base = Path(str(config_file) + ".v1.bak")
        backup_base.write_text("bak0", encoding="utf-8")
        Path(f"{backup_base}.1").write_text("bak1", encoding="utf-8")
        Path(f"{backup_base}.2").write_text("bak2", encoding="utf-8")

        loader = ConfigLoader(str(config_file))
        loader.migrate_v1_to_v2(v1_config)

        assert Path(f"{backup_base}.3").exists()

    def test_backup_failure_raises_error(
        self, tmp_path: Path, v1_config: dict
    ):
        """백업 실패 시 ConfigMigrationError가 발생한다."""
        # 읽기 전용 디렉토리에 설정 파일 생성 시뮬레이션
        # 존재하지 않는 디렉토리를 백업 대상으로 설정
        cfg_path = tmp_path / "nonexistent_dir" / "config.json"
        # 파일은 존재하지만 백업 경로의 부모가 없는 상황을 만들기 어려우므로
        # config_path 자체가 존재하지 않는 경우를 테스트
        loader = ConfigLoader(str(cfg_path))
        with pytest.raises(ConfigMigrationError):
            loader.migrate_v1_to_v2(v1_config)
