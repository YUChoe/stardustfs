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


def test_daemon_status_not_running(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"version": 2, "metadata_db": str(tmp_path / "meta.db")}),
        encoding="utf-8",
    )
    status = actions.daemon_status(str(cfg))
    assert status["running"] is False
