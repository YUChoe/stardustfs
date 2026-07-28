"""단일 인스턴스 가드 테스트 (GUI/클라이언트 중복 실행 방지).

GUI가 여러 개 뜨면 같은 스토리지를 다투고 각자 daemon을 감독해 서로의 판단을
무너뜨린다. 제어 파일 + heartbeat 신선도로 하나만 뜨게 한다.
"""

from __future__ import annotations

import os
import time

from stardustlib import daemon, single_instance
from stardustlib.single_instance import (
    InstanceLock,
    consume_focus_request,
    gui_lock_path,
    request_focus,
)


def test_second_acquire_is_rejected(tmp_path):
    """살아 있는 보유자가 있으면 두 번째 획득은 거절된다."""
    path = str(tmp_path / "gui.lock.json")
    first = InstanceLock(path)
    second = InstanceLock(path)
    try:
        assert first.acquire() is True
        assert first.held is True
        assert second.acquire() is False
        assert second.held is False
        assert first.holder()["pid"] == os.getpid()
    finally:
        first.release()


def test_release_allows_reacquire(tmp_path):
    """놓으면 제어 파일이 지워지고 다시 잡을 수 있다."""
    path = str(tmp_path / "gui.lock.json")
    lock = InstanceLock(path)
    assert lock.acquire() is True
    lock.release()
    assert not os.path.exists(path)
    assert lock.held is False

    again = InstanceLock(path)
    try:
        assert again.acquire() is True
    finally:
        again.release()


def test_release_is_idempotent(tmp_path):
    path = str(tmp_path / "gui.lock.json")
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()
    lock.release()  # 두 번째 호출이 예외를 내지 않는다


def test_stale_lock_is_taken_over(tmp_path):
    """heartbeat가 끊긴 락(죽은 프로세스·얼어붙은 창)은 인계된다."""
    path = str(tmp_path / "gui.lock.json")
    old = time.time() - (daemon._STALE_SECONDS + 5)
    daemon.write_control(path, old, old)
    assert daemon.read_control(path)["stale"] is True

    lock = InstanceLock(path)
    try:
        assert lock.acquire() is True
    finally:
        lock.release()


def test_beat_keeps_lock_fresh(tmp_path):
    """beat()가 heartbeat를 갱신해 stale로 떨어지지 않는다."""
    path = str(tmp_path / "gui.lock.json")
    lock = InstanceLock(path)
    try:
        lock.acquire()
        # heartbeat를 과거로 밀어 stale 직전 상태를 만든다
        daemon.write_control(
            path, lock.started_at, time.time() - daemon._STALE_SECONDS - 1
        )
        assert daemon.read_control(path)["running"] is False
        lock.beat()
        assert daemon.read_control(path)["running"] is True
    finally:
        lock.release()


def test_beat_does_nothing_without_lock(tmp_path):
    """락을 잡지 않은 인스턴스의 beat()는 제어 파일을 만들지 않는다."""
    path = str(tmp_path / "gui.lock.json")
    InstanceLock(path).beat()
    assert not os.path.exists(path)


def test_focus_request_roundtrip(tmp_path):
    """두 번째 실행이 남긴 포커스 요청을 보유자가 한 번만 소비한다."""
    path = str(tmp_path / "gui.lock.json")
    assert consume_focus_request(path) is False  # 요청 없음
    request_focus(path)
    assert consume_focus_request(path) is True
    assert consume_focus_request(path) is False  # 소비 후에는 없다


def test_release_clears_focus_sentinel(tmp_path):
    """락을 놓을 때 포커스 센티넬도 정리한다(다음 실행이 오해하지 않게)."""
    path = str(tmp_path / "gui.lock.json")
    lock = InstanceLock(path)
    lock.acquire()
    request_focus(path)
    lock.release()
    assert consume_focus_request(path) is False


def test_acquire_fails_when_control_file_unwritable(tmp_path, monkeypatch):
    """제어 파일을 쓸 수 없으면 획득에 실패한다.

    자기 존재를 알릴 수 없는 인스턴스를 띄우면 그다음 실행을 막을 수 없다.
    """
    path = str(tmp_path / "gui.lock.json")
    monkeypatch.setattr(single_instance, "write_control", lambda *a: False)
    lock = InstanceLock(path)
    assert lock.acquire() is False
    assert lock.held is False


def test_gui_lock_path_is_user_scoped(tmp_path, monkeypatch):
    """락 경로는 config가 아니라 사용자 단위다(GUI는 config 없이도 뜬다)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = gui_lock_path()
    assert path.endswith("gui.lock.json")
    assert os.path.isdir(os.path.dirname(path))


def test_run_gui_refuses_second_instance(tmp_path, monkeypatch):
    """이미 실행 중이면 창을 만들지 않고 1을 반환하며 포커스를 요청한다."""
    from stardustlib.gui import app as gui_app

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    holder = InstanceLock(gui_lock_path())
    assert holder.acquire() is True

    def _boom():  # Tk를 만들면 안 된다
        raise AssertionError("두 번째 인스턴스가 창을 열었다")

    monkeypatch.setattr(gui_app.tk, "Tk", _boom)
    try:
        assert gui_app.run_gui(None) == 1
        # 먼저 뜬 인스턴스가 볼 포커스 요청이 남아 있다
        assert consume_focus_request(holder.path) is True
    finally:
        holder.release()


class _InstanceStub:
    """StardustApp._instance_poll만 떼어 검증하기 위한 최소 스텁(Tk 불필요)."""

    def __init__(self, lock) -> None:
        self._instance_lock = lock
        self.shown = 0
        self.scheduled: list[int] = []
        self.root = self

    def _show_window(self) -> None:
        self.shown += 1

    def _instance_poll(self) -> None:  # after()에 넘길 콜백
        pass

    def after(self, delay: int, _fn) -> None:
        self.scheduled.append(delay)


def test_instance_poll_beats_and_reschedules(tmp_path):
    """폴링이 heartbeat를 갱신하고 다음 폴링을 예약한다."""
    from stardustlib.gui.app import StardustApp

    path = str(tmp_path / "gui.lock.json")
    lock = InstanceLock(path)
    lock.acquire()
    try:
        # heartbeat를 과거로 밀어 갱신 여부를 확인한다
        daemon.write_control(
            path, lock.started_at, time.time() - daemon._STALE_SECONDS - 1
        )
        stub = _InstanceStub(lock)
        StardustApp._instance_poll(stub)

        assert daemon.read_control(path)["running"] is True, "heartbeat 미갱신"
        assert stub.shown == 0  # 포커스 요청이 없으면 창을 건드리지 않는다
        assert stub.scheduled == [
            int(single_instance.BEAT_INTERVAL_SECONDS * 1000)
        ]
    finally:
        lock.release()


def test_instance_poll_raises_window_on_focus_request(tmp_path):
    """두 번째 실행의 포커스 요청을 보면 창을 앞으로 올린다."""
    from stardustlib.gui.app import StardustApp

    path = str(tmp_path / "gui.lock.json")
    lock = InstanceLock(path)
    lock.acquire()
    try:
        request_focus(path)
        stub = _InstanceStub(lock)
        StardustApp._instance_poll(stub)
        assert stub.shown == 1
        # 요청은 소비되어 다음 폴링에서 다시 올리지 않는다
        StardustApp._instance_poll(stub)
        assert stub.shown == 1
    finally:
        lock.release()
