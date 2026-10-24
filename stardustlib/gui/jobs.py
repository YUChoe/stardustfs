"""워커 브리지 — 작업 제출, 진행 중 작업 추적, 실패 표시.

두 워커를 둔다.

- `worker`: 코어 세션(sqlite)을 쓰는 모든 작업. sqlite 연결이 스레드별이라 하나여야 한다.
- `io_worker`: 세션을 쓰지 않는 가벼운 폴링(데몬 제어 파일, 로컬 진행 조회, 메타데이터
  mtime, FAT 준비 확인). 큰 업로드가 `worker`를 붙잡고 있어도 상태 표시가 멈추지 않는다.

상태 문구는 진행 중인 작업이 모두 끝났을 때만 '준비됨'으로 돌린다. 작업 하나가 끝날
때마다 무조건 '준비됨'을 쓰면 다른 작업이 아직 도는데도 끝난 것처럼 보인다.

주기 폴링은 `_submit_once`로 낸다 — 앞선 같은 폴링이 아직 큐에 있으면 다시 넣지
않아, 긴 작업 뒤에 폴링이 수십 개 쌓이지 않는다.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from typing import Any

from stardustlib.gui import errors
from stardustlib.gui.worker import Worker

logger = logging.getLogger(__name__)

# 결과 큐를 비우는 주기(ms).
_TICK_MS = 80

# 작업이 이 시간보다 오래 걸릴 때만 진행 막대(무한)를 보인다 — 짧은 조회마다 막대가
# 깜빡이지 않게 한다.
_INDICATOR_DELAY_MS = 400


class JobsMixin:
    """작업 제출과 진행 상태 관리."""

    def _init_jobs(self) -> None:
        self.worker = Worker()
        self.io_worker = Worker("stardust-gui-io")
        # 진행 중 작업: key → 상태 문구(마지막에 들어온 것을 보여준다).
        self._busy: dict[Any, str] = {}
        self._job_ids = itertools.count(1)
        self._inflight: set[str] = set()

    def _tick(self) -> None:
        self.worker.poll()
        self.io_worker.poll()
        self.root.after(_TICK_MS, self._tick)

    # --- 진행 중 작업 ---

    def _job_begin(self, key, text: str) -> None:
        """작업을 진행 중으로 등록(같은 key면 문구만 갱신)하고 상태바에 보인다."""
        self._busy[key] = text
        self._set_status(text)
        self.root.after(_INDICATOR_DELAY_MS, self._sync_busy_indicator)

    def _job_end(self, key) -> None:
        """작업을 끝내고, 남은 작업이 있으면 그 문구를, 없으면 기본 문구를 보인다."""
        self._busy.pop(key, None)
        if self._busy:
            self._set_status(next(reversed(self._busy.values())))
        elif getattr(self, "_showing_progress", False):
            # 데몬 백업이 도는 중이면 그 진행 문구로 되돌린다.
            self._set_status(getattr(self, "_progress_text", "") or self.t["ready"])
        else:
            self._set_status(self.t["ready"])
        self._sync_busy_indicator()

    def _sync_busy_indicator(self) -> None:
        self._set_activity(bool(self._busy))

    # --- 제출 ---

    def _submit(
        self,
        fn: Callable[[], Any],
        on_ok: Callable[[Any], None] | None = None,
        busy: str | None = None,
    ) -> None:
        """설정이 필요한 작업을 코어 워커에 낸다.

        실패는 사용자 문구로 바꿔 상태바와 배너에 알린다(원본 예외는 로그에 남긴다).
        """
        if not self.config_path:
            self._show_banner(self.t["need_config"], level="warning")
            return
        job = next(self._job_ids)
        self._job_begin(job, busy or self.t["busy_browse"])

        def done(ok, payload):
            self._job_end(job)
            if ok:
                if on_ok:
                    on_ok(payload)
                return
            logger.warning("GUI 작업 실패: %r", payload)
            # 실패는 모달 대신 배너로 알린다 — 실패가 이어질 때 확인 버튼만 반복해
            # 누르게 만들지 않는다.
            message = errors.describe(payload, self.t)
            self._set_status(self.t["err_status"].format(msg=message))
            self._show_banner(message)

        self.worker.submit(fn, done)

    def _submit_once(
        self,
        key: str,
        fn: Callable[[], Any],
        on_done: Callable[[bool, Any], None],
        *,
        io: bool = False,
    ) -> bool:
        """같은 key의 작업이 아직 끝나지 않았으면 내지 않는다(주기 폴링용)."""
        if key in self._inflight:
            return False
        self._inflight.add(key)

        def done(ok, payload):
            self._inflight.discard(key)
            on_done(ok, payload)

        (self.io_worker if io else self.worker).submit(fn, done)
        return True
