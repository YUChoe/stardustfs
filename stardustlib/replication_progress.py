"""복제 진행 상태 추적 (GUI·제어 채널 노출용).

대용량 파일 백업은 읽기·전송에 수 분이 걸리는데, 그동안 아무 표시가 없으면 멈춘
것과 구분되지 않는다. ReplicationManager가 청크 단위로 이 추적기를 갱신하고,
daemon 제어 채널(`/ctl/progress`)이 스냅샷을 읽어 GUI에 노출한다.

메모리에만 보관한다(파일 기록 없음). 스냅샷에는 가상 경로와 수치만 담고 파일
내용·키·토큰은 담지 않는다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# 단계 이름. reading=로컬 청크 읽기, storing=홀더 전송.
STAGE_READING = "reading"
STAGE_STORING = "storing"


@dataclass(frozen=True)
class ProgressSnapshot:
    """복제 진행 스냅샷(경로·수치만)."""

    virtual_path: str
    stage: str
    done: int
    total: int
    secured: int
    elapsed: float

    def as_dict(self) -> dict:
        """제어 채널 응답용 dict. 사용자 데이터는 포함하지 않는다."""
        return {
            "active": True,
            "path": self.virtual_path,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "secured": self.secured,
            "elapsed": round(self.elapsed, 1),
        }


class ProgressTracker:
    """진행 상태를 메모리에 보관한다(스레드 안전).

    복제는 워커 스레드에서, 조회는 데몬 이벤트 루프에서 일어나므로 락으로 보호한다.
    추적 실패가 복제를 막아서는 안 되므로 모든 메서드는 예외를 던지지 않는다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: str | None = None
        self._stage = STAGE_READING
        self._done = 0
        self._total = 0
        self._secured = 0
        self._started = 0.0

    def begin(self, virtual_path: str, total: int, stage: str) -> None:
        """새 복제 수행을 시작으로 표시한다."""
        with self._lock:
            self._path = virtual_path
            self._stage = stage
            self._total = max(0, total)
            self._done = 0
            self._secured = 0
            self._started = time.monotonic()

    def set_stage(self, stage: str, total: int | None = None) -> None:
        """단계를 바꾼다(읽기 → 전송). total을 주면 함께 갱신한다."""
        with self._lock:
            if self._path is None:
                return
            self._stage = stage
            self._done = 0
            if total is not None:
                self._total = max(0, total)

    def advance(self, done: int, secured: int | None = None) -> None:
        """처리한 청크 수를 갱신한다(단조 증가)."""
        with self._lock:
            if self._path is None:
                return
            self._done = max(self._done, done)
            if secured is not None:
                self._secured = secured

    def finish(self) -> None:
        """수행 종료(성공·실패·예외 무관). 이후 스냅샷은 비활성이다."""
        with self._lock:
            self._path = None
            self._done = 0
            self._total = 0
            self._secured = 0

    def snapshot(self) -> ProgressSnapshot | None:
        """현재 진행 스냅샷. 진행 중이 아니면 None."""
        with self._lock:
            if self._path is None:
                return None
            return ProgressSnapshot(
                virtual_path=self._path,
                stage=self._stage,
                done=self._done,
                total=self._total,
                secured=self._secured,
                elapsed=time.monotonic() - self._started,
            )
