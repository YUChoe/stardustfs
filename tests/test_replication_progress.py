"""복제 진행 추적기 검증(Property 4·5).

진행 값은 단조 증가하고 종료 시 정리되며, 스냅샷은 경로·수치만 담는다.
"""

from __future__ import annotations

from stardustlib.replication_progress import (
    STAGE_READING,
    STAGE_STORING,
    ProgressTracker,
)


def test_snapshot_is_none_when_idle():
    tracker = ProgressTracker()
    assert tracker.snapshot() is None


def test_advance_is_monotonic():
    """뒤늦게 도착한 작은 값이 진행을 되돌리지 않는다(Property 4)."""
    tracker = ProgressTracker()
    tracker.begin("/f", 10, STAGE_STORING)
    tracker.advance(5)
    tracker.advance(3)
    assert tracker.snapshot().done == 5
    tracker.advance(7)
    assert tracker.snapshot().done == 7


def test_finish_clears_progress():
    """종료 후에는 비활성이다(Property 4)."""
    tracker = ProgressTracker()
    tracker.begin("/f", 3, STAGE_STORING)
    tracker.advance(2)
    tracker.finish()
    assert tracker.snapshot() is None


def test_stage_change_resets_done():
    """읽기 → 전송으로 넘어가면 진행이 다시 0부터다."""
    tracker = ProgressTracker()
    tracker.begin("/f", 10, STAGE_READING)
    tracker.advance(10)
    tracker.set_stage(STAGE_STORING, 4)
    snap = tracker.snapshot()
    assert snap.stage == STAGE_STORING
    assert snap.done == 0 and snap.total == 4


def test_updates_after_finish_are_ignored():
    """종료 뒤 늦게 도착한 갱신이 유령 진행을 만들지 않는다."""
    tracker = ProgressTracker()
    tracker.begin("/f", 5, STAGE_STORING)
    tracker.finish()
    tracker.advance(3)
    tracker.set_stage(STAGE_READING)
    assert tracker.snapshot() is None


def test_snapshot_dict_contains_only_metadata():
    """응답에 파일 내용·키·토큰이 섞이지 않는다(Property 5)."""
    tracker = ProgressTracker()
    tracker.begin("/docs/secret.txt", 8, STAGE_STORING)
    tracker.advance(3, secured=2)
    payload = tracker.snapshot().as_dict()

    assert set(payload) == {
        "active", "path", "stage", "done", "total", "secured", "elapsed"
    }
    assert payload["active"] is True
    assert payload["path"] == "/docs/secret.txt"
    assert payload["done"] == 3 and payload["secured"] == 2
    assert isinstance(payload["elapsed"], float)
