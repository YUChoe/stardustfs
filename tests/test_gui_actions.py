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


def test_replica_counts_empty_when_offline(tmp_path):
    # 오프라인(server.url 없음) 설정 → 온라인 조회 불가 → 빈 dict(상태 컬럼만)
    base = tmp_path / "setup-rc"
    cfg_path = actions.create_config(
        str(base), "", "dev-rc", generate_key=True
    )
    assert actions.replica_counts(cfg_path, "/", ["a", "b"]) == {}
    # 대상 없음(names=[]) → 서버 조회 자체를 건너뜀
    assert actions.replica_counts(cfg_path, "/", []) == {}


def test_metadata_mtime(tmp_path):
    base = tmp_path / "setup-mt"
    cfg_path = actions.create_config(
        str(base), "", "dev-mt", generate_key=True
    )
    # 설정 생성 시 metadata_db 경로가 잡히고, 코어 동작 후 파일이 생긴다.
    mt = actions.metadata_mtime(cfg_path)
    assert isinstance(mt, float) and mt >= 0.0


def test_created_config_validates(tmp_path):
    from stardustlib.config_loader import ConfigLoader

    base = tmp_path / "setup3"
    cfg_path = actions.create_config(
        str(base), "https://s.example", "dev-3", generate_key=True
    )
    loader = ConfigLoader(cfg_path)
    config = loader.load()
    assert loader.validate(config) == []         # 생성된 설정은 검증 통과


def test_source_add_remove(tmp_path):
    import os as _os

    base = tmp_path / "s"
    cfg_path = actions.create_config(
        str(base), "https://s.example", "dev", generate_key=True
    )
    extra = tmp_path / "extra"
    extra.mkdir()
    before = len(actions.list_sources(cfg_path))
    sid = actions.add_source(cfg_path, "directory", str(extra))
    srcs = actions.list_sources(cfg_path)
    assert len(srcs) == before + 1
    assert any(s["id"] == sid and s["path"] == _os.path.abspath(str(extra))
               for s in srcs)
    actions.remove_source(cfg_path, sid)
    assert all(s["id"] != sid for s in actions.list_sources(cfg_path))


def test_is_logged_in_reflects_credential_store(tmp_path):
    import os as _os

    base = tmp_path / "li"
    cfg_path = actions.create_config(
        str(base), "https://s.example", "dev", generate_key=True
    )
    assert actions.is_logged_in(cfg_path) is False
    # 자격증명 저장소 파일이 생기면 True
    cred = _os.path.join(str(base), "metadata.db.credentials.json")
    with open(cred, "w", encoding="utf-8") as f:
        f.write("{}")
    assert actions.is_logged_in(cfg_path) is True


def test_daemon_status_not_running(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"version": 2, "metadata_db": str(tmp_path / "meta.db")}),
        encoding="utf-8",
    )
    status = actions.daemon_status(str(cfg))
    assert status["running"] is False
