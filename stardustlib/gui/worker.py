"""GUI 워커 스레드 / 결과 큐 브리지.

Tkinter는 단일 스레드이고 위젯은 메인 스레드에서만 갱신해야 하므로, 블로킹/네트워크
작업은 단일 워커 스레드에서 순차 실행하고 결과를 큐로 메인 스레드에 전달한다.
모든 코어 작업이 같은 워커 스레드에서 실행되므로 sqlite 스레드 제약도 만족한다.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any


class Worker:
    """단일 백그라운드 스레드에서 작업을 순차 실행한다.

    코어(sqlite 세션)를 쓰는 작업은 모두 같은 Worker 하나에서 돌아야 한다. 제어 파일
    읽기·로컬 HTTP 조회처럼 세션을 쓰지 않는 가벼운 폴링은 별도 Worker에 두어, 큰
    전송이 도는 동안에도 상태 표시가 멈추지 않게 한다.
    """

    def __init__(self, name: str = "stardust-gui-worker") -> None:
        self._tasks: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=name
        )
        self._thread.start()

    def submit(
        self, fn: Callable[[], Any], on_done: Callable[[bool, Any], None]
    ) -> None:
        """작업 fn을 큐잉한다. 완료 시 on_done(ok, result_or_exc)이 메인 스레드에서
        호출된다(poll을 통해)."""
        self._tasks.put((fn, on_done))

    def _run(self) -> None:
        while True:
            fn, on_done = self._tasks.get()
            try:
                result = fn()
                self._results.put((on_done, True, result))
            except Exception as e:  # noqa: BLE001 — UI에 전달
                self._results.put((on_done, False, e))

    def poll(self) -> None:
        """메인 스레드(Tk)에서 주기 호출. 완료된 결과의 콜백을 실행한다."""
        try:
            while True:
                on_done, ok, payload = self._results.get_nowait()
                on_done(ok, payload)
        except queue.Empty:
            pass
