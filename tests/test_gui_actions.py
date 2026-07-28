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


def test_storage_overview_local_only_when_offline(tmp_path):
    # 오프라인 설정 → 강등 모드: 이 디바이스 로컬 소스만, online=False
    base = tmp_path / "setup-ov"
    cfg_path = actions.create_config(
        str(base), "", "dev-ov", generate_key=True
    )
    sid = actions.add_source(
        cfg_path, "loopback", str(base / "v.img"), size=10 * 1024 * 1024
    )
    actions.invalidate(cfg_path)
    ov = actions.storage_overview(cfg_path)
    assert ov["online"] is False            # 서버 미설정 → 강등
    ids = {s["source_id"] for s in ov["sources"]}
    assert sid in ids                       # 추가한 루프백 포함
    assert all(s["self"] for s in ov["sources"])  # 강등 모드는 전부 이 기기
    actions.invalidate(cfg_path)


def test_browse_includes_backup_summary(tmp_path):
    base = tmp_path / "setup-bs"
    cfg_path = actions.create_config(
        str(base), "", "dev-bs", generate_key=True
    )
    d = actions.browse(cfg_path, "/")
    assert set(d["backup_summary"]) == {"none", "pending", "replicated", "total"}
    actions.invalidate(cfg_path)


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
    img = tmp_path / "extra.img"
    before = len(actions.list_sources(cfg_path))
    sid = actions.add_source(cfg_path, "loopback", str(img),
                             size=10 * 1024 * 1024)
    srcs = actions.list_sources(cfg_path)
    assert len(srcs) == before + 1
    assert any(s["id"] == sid and s["path"] == _os.path.abspath(str(img))
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


def test_restore_paths_requires_online(tmp_path):
    """restore_paths는 온라인 액션 — 오프라인 설정에서는 '로그인 필요'로 실패한다."""
    import pytest

    base = tmp_path / "setup-restore"
    cfg_path = actions.create_config(
        str(base), "", "dev-r", generate_key=True
    )
    # 오프라인(server.url 없음) → 온라인 세션 불가 → RuntimeError(로그인 필요)
    with pytest.raises(RuntimeError):
        actions.restore_paths(cfg_path, ["/some/file.txt"])


def test_announce_paths_without_daemon(tmp_path):
    """announce_paths는 데몬 미실행 시 daemon=False로 알린다(예외 없음).

    온라인 세션이 필요 없는 경량 요청이므로 오프라인 설정에서도 호출 가능하다.
    """
    base = tmp_path / "setup-announce"
    cfg_path = actions.create_config(
        str(base), "", "dev-an", generate_key=True
    )
    result = actions.announce_paths(cfg_path, ["/some/file.txt"])
    assert result["daemon"] is False
    assert result["announced"] == 0


# --- daemon 감독: 정지→실행 전이에서 조회 세션 재개방 ---

class _DaemonStub:
    """App._on_daemon만 떼어 검증하기 위한 최소 스텁(Tk 불필요)."""

    def __init__(self) -> None:
        self.t = {
            "daemon_running": "daemon {pid}",
            "daemon_stopped": "stopped",
            "daemon_stale": "stale",
            "daemon_unknown": "unknown",
        }
        self._daemon_was_running = None
        self.reopened = 0
        self.ensured = 0

    def _daemon_dot(self, text, color) -> None:
        pass

    def _reopen_after_daemon_start(self) -> None:
        self.reopened += 1

    def _ensure_daemon(self) -> None:
        self.ensured += 1


def test_daemon_stopped_triggers_start():
    """미실행이면 감독 로직이 daemon을 기동한다(GUI만 쓰는 사용자 경로)."""
    from stardustlib.gui.app import StardustApp

    stub = _DaemonStub()
    StardustApp._on_daemon(stub, True, {"running": False})
    assert stub.ensured == 1
    assert stub.reopened == 0


def test_daemon_start_transition_reopens_session():
    """정지→실행 전이에서만 조회 세션을 다시 연다.

    daemon이 없을 때 연 세션은 루프백 FAT 이미지가 없어 소스가 비활성이므로,
    daemon이 이미지를 만든 뒤 다시 열어야 스토리지가 정상으로 보인다.
    """
    from stardustlib.gui.app import StardustApp

    stub = _DaemonStub()
    StardustApp._on_daemon(stub, True, {"running": False})
    StardustApp._on_daemon(stub, True, {"running": True, "pid": 1})
    assert stub.reopened == 1
    # 계속 실행 중이면 매 폴링마다 다시 열지 않는다
    StardustApp._on_daemon(stub, True, {"running": True, "pid": 1})
    assert stub.reopened == 1


def test_daemon_running_at_startup_does_not_reopen():
    """GUI 시작 시 이미 실행 중이면 세션을 다시 열 필요가 없다."""
    from stardustlib.gui.app import StardustApp

    stub = _DaemonStub()
    StardustApp._on_daemon(stub, True, {"running": True, "pid": 7})
    assert stub.reopened == 0
    assert stub.ensured == 0


# --- 수동 백업: 타 device 청크는 위임(데이터 왕복 없음) ---

class _StubChunk:
    def __init__(self, index: int, device_id: str | None) -> None:
        self.index = index
        self.device_id = device_id


class _StubRemote:
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.announced: list[str] = []

    def announce_backup(self, virtual_path: str) -> bool:
        self.announced.append(virtual_path)
        return self.reachable


class _StubSession:
    def __init__(self, chunks: list, remotes: dict) -> None:
        self.metadata = self
        self._chunks = chunks
        self.storage_pool = self
        self.device_id = "self-dev"
        self._remote_devices = remotes

    def get_chunks(self, virtual_path: str) -> list:
        return self._chunks


def test_remote_chunk_devices_excludes_local():
    """로컬 보관 청크(device_id 없음/자기 기기)는 위임 대상이 아니다."""
    session = _StubSession(
        [_StubChunk(0, None), _StubChunk(1, "self-dev"),
         _StubChunk(2, "dev-B"), _StubChunk(3, "dev-B")],
        {},
    )
    assert actions._remote_chunk_devices(session, "/f") == {"dev-B"}


def test_delegate_backup_calls_each_remote_once():
    """보관 기기마다 1회씩 위임한다(청크 수만큼 반복하지 않는다)."""
    remote_b, remote_c = _StubRemote(), _StubRemote()
    session = _StubSession([], {"dev-B": remote_b, "dev-C": remote_c})

    failed = actions._delegate_backup(session, "/f", {"dev-B", "dev-C"})
    assert failed == []
    assert remote_b.announced == ["/f"]
    assert remote_c.announced == ["/f"]


def test_delegate_backup_reports_unreachable():
    """도달 불가·미마운트 기기는 실패로 보고한다(로컬 전송 강행 없음)."""
    session = _StubSession([], {"dev-B": _StubRemote(reachable=False)})

    failed = actions._delegate_backup(session, "/f", {"dev-B", "dev-missing"})
    assert sorted(failed) == ["dev-B", "dev-missing"]


# --- 복제 진행 표시 (GUI 상태바) ---

class _ProgressStub:
    """StardustApp._show_progress / _mgmt_poll만 떼어 검증하기 위한 최소 스텁."""

    def __init__(self) -> None:
        self.t = {
            "ready": "준비됨",
            "backup_progress": "백업 중: {name} {done}/{total} 청크",
            "backup_progress_reading": "읽는 중: {name} {done}/{total}",
        }
        self._showing_progress = False
        self.status_text = "이전 상태"
        self.refreshed = 0
        self.scheduled: list[int] = []
        self.root = self

    def _set_status(self, text: str) -> None:
        self.status_text = text

    def _refresh_mgmt(self) -> None:
        self.refreshed += 1

    def _mgmt_poll(self) -> None:  # after()에 넘길 콜백(재예약은 하지 않는다)
        pass

    def after(self, delay: int, _fn) -> None:  # root.after 대역
        self.scheduled.append(delay)


def test_progress_shown_in_status_bar():
    from stardustlib.gui.app import StardustApp

    stub = _ProgressStub()
    StardustApp._show_progress(stub, True, {
        "active": True, "path": "/movies/big.mp4", "stage": "storing",
        "done": 42, "total": 188,
    })
    assert stub.status_text == "백업 중: big.mp4 42/188 청크"
    assert stub._showing_progress is True


def test_progress_reading_stage_has_own_message():
    from stardustlib.gui.app import StardustApp

    stub = _ProgressStub()
    StardustApp._show_progress(stub, True, {
        "active": True, "path": "/a/b.bin", "stage": "reading",
        "done": 3, "total": 20,
    })
    assert stub.status_text == "읽는 중: b.bin 3/20"


def test_progress_cleared_when_finished():
    """진행이 끝나면 상태바를 기본 문구로 되돌린다."""
    from stardustlib.gui.app import StardustApp

    stub = _ProgressStub()
    StardustApp._show_progress(stub, True, {
        "active": True, "path": "/f", "stage": "storing",
        "done": 1, "total": 2,
    })
    StardustApp._show_progress(stub, True, {"active": False})
    assert stub.status_text == "준비됨"
    assert stub._showing_progress is False


def test_progress_poll_failure_keeps_status():
    """데몬 미실행·조회 실패면 기존 상태바를 건드리지 않는다."""
    from stardustlib.gui.app import StardustApp

    stub = _ProgressStub()
    StardustApp._show_progress(stub, False, None)
    StardustApp._show_progress(stub, True, None)
    assert stub.status_text == "이전 상태"


def test_backup_start_and_finish_refresh_storage_panel():
    """백업 시작·종료 시 스토리지 패널을 즉시 갱신한다.

    백업은 daemon이 수행하므로 GUI의 쓰기 경로(_after_write)를 타지 않는다 —
    여기서 당겨오지 않으면 용량이 멈춘 것처럼 보인다.
    """
    from stardustlib.gui.app import StardustApp

    stub = _ProgressStub()
    StardustApp._show_progress(stub, True, {
        "active": True, "path": "/a/big.bin", "stage": "storing",
        "done": 1, "total": 10,
    })
    assert stub._showing_progress is True
    assert stub.refreshed == 1, "백업 시작 시 갱신하지 않음"

    # 진행 중 반복 호출은 추가 갱신을 유발하지 않는다(주기 폴링이 맡는다)
    StardustApp._show_progress(stub, True, {
        "active": True, "path": "/a/big.bin", "stage": "storing",
        "done": 5, "total": 10,
    })
    assert stub.refreshed == 1

    StardustApp._show_progress(stub, True, {"active": False})
    assert stub._showing_progress is False
    assert stub.refreshed == 2, "백업 종료 시 최종 용량을 반영하지 않음"
    assert stub.status_text == "준비됨"


def test_mgmt_poll_shortens_interval_during_backup():
    """백업 중에는 패널 갱신 주기를 줄인다."""
    from stardustlib.gui.app import (
        _MGMT_POLL_BACKUP_MS,
        _MGMT_POLL_IDLE_MS,
        StardustApp,
    )

    stub = _ProgressStub()
    StardustApp._mgmt_poll(stub)
    assert stub.scheduled[-1] == _MGMT_POLL_IDLE_MS

    stub._showing_progress = True
    StardustApp._mgmt_poll(stub)
    assert stub.scheduled[-1] == _MGMT_POLL_BACKUP_MS
    assert _MGMT_POLL_BACKUP_MS < _MGMT_POLL_IDLE_MS


def test_daemon_live_sources_empty_without_daemon(tmp_path):
    """데몬이 없으면 빈 dict — 서버 레지스트리 값을 그대로 쓴다."""
    assert actions._daemon_live_sources(str(tmp_path / "none.db")) == {}
