"""GUI 백엔드(actions) 및 import 스모크 테스트 (Tk 비의존 부분만)."""

import json

from stardustlib.gui import actions
from stardustlib.gui.app import _human


def test_imports():
    # app/worker import 가능(헤드리스에서 tkinter import 자체는 가능, Tk() 미호출)
    import stardustlib.gui.app  # noqa: F401
    import stardustlib.gui.worker  # noqa: F401


def test_human_size():
    assert _human(0) == "0 B"
    assert _human(512) == "512 B"
    assert _human(1024) == "1.0 KiB"
    assert _human(1536) == "1.5 KiB"
    assert _human(1048576) == "1.0 MiB"


def test_create_config_generates_key(tmp_path):
    base = tmp_path / "setup"
    cfg_path = actions.create_config(
        str(base), "https://s.example", "dev-1", generate_key=True
    )
    assert __import__("os").path.exists(cfg_path)
    cfg = json.loads(open(cfg_path, encoding="utf-8").read())
    assert cfg["version"] == 2
    assert cfg["server"]["url"] == "https://s.example"
    assert cfg["server"]["device_name"] == "dev-1"
    assert cfg["sources"][0]["type"] == "directory"
    key = base / "master.key"
    assert key.exists() and key.stat().st_size == 32
    assert (base / "storage").is_dir()


def test_create_config_offline_no_key(tmp_path):
    base = tmp_path / "setup2"
    cfg_path = actions.create_config(
        str(base), "", "dev-2", generate_key=False
    )
    cfg = json.loads(open(cfg_path, encoding="utf-8").read())
    assert cfg["server"]["url"] is None          # 빈 URL → 오프라인
    assert not (base / "master.key").exists()    # 복원 모드: 키 미생성


def test_created_config_validates(tmp_path):
    from stardustlib.config_loader import ConfigLoader

    base = tmp_path / "setup3"
    cfg_path = actions.create_config(
        str(base), "https://s.example", "dev-3", generate_key=True
    )
    loader = ConfigLoader(cfg_path)
    config = loader.load()
    assert loader.validate(config) == []         # 생성된 설정은 검증 통과


def test_daemon_status_not_running(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"version": 2, "metadata_db": str(tmp_path / "meta.db")}),
        encoding="utf-8",
    )
    status = actions.daemon_status(str(cfg))
    assert status["running"] is False
