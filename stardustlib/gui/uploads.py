"""업로드 큐 — 파일 선택·끌어다 놓기, 순차 업로드, 같은 이름 충돌 처리.

진행은 별도 창이 아니라 파일 목록의 행으로 보여준다 — 사용자가 보고 있는 화면에서
상태가 바뀌는 편이 창을 하나 더 띄우는 것보다 읽기 쉽다. 업로드 큐는 서버 목록과 한
자료구조에 섞지 않는다: 3초 폴링이 목록을 통째로 다시 그려도 진행 행이 살아남아야 한다.

같은 이름이 이미 있으면 배치가 끝난 뒤 한 번만 묻는다(덮어쓰기 / 둘 다 유지 /
건너뛰기). 파일마다 묻으면 수십 개를 올릴 때 창이 수십 번 뜬다.

바이트 단위 진행률과 진행 중 파일의 취소는 없다 — put_file이 파일 단위 원자적
호출이고 데몬 위임(/ctl/put)도 동기 POST라 중간 상태를 받을 수 없다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from tkinter import filedialog

from stardustlib.gui import actions, dnd, errors
from stardustlib.gui.format import shorten
from stardustlib.gui.widgets.choice_dialog import ChoiceDialog

logger = logging.getLogger(__name__)

# 업로드 진행 행의 상태별 표시 키.
_STATE_KEYS = {
    "queued": "up_state_queued",
    "running": "up_state_running",
    "failed": "up_state_failed",
    "exists": "up_state_exists",
}

# 끝난(다시 손대지 않으면 그대로 남는) 상태.
_FINISHED = ("failed", "exists")


@dataclass
class UploadItem:
    """업로드 큐의 한 항목. 목록에 진행 행으로 그려진다."""

    local: str
    name: str
    dest: str                 # 선택 시점의 가상 폴더(고정)
    state: str = "queued"     # queued | running | failed | exists
    error: str = ""
    mode: str = "error"       # 같은 이름이 있을 때: error | overwrite | rename


class UploadsMixin:
    """업로드 큐와 그 표시."""

    # --- 시작 ---

    def _check_storage_ready(self, then) -> None:
        """스토리지가 초기화 중이 아니면 then()을 부른다(아니면 안내만).

        루프백 FAT 이미지를 열어 보는 확인이라 메인 스레드에서 하지 않는다. 세션을
        쓰지 않으므로 io 워커에서 돌려, 큰 전송이 도는 중에도 바로 답한다.
        """
        cfg = self.config_path
        if not cfg:
            self._show_banner(self.t["need_config"], level="warning")
            return

        def done(ok, initializing) -> None:
            if ok and initializing:
                self._show_banner(self.t["transfer_init_block"], level="warning")
                return
            then()

        self.io_worker.submit(lambda: actions.storage_initializing(cfg), done)

    def _upload(self) -> None:
        """파일을 고르면 곧바로 업로드를 시작한다(별도 업로드 창 없음)."""
        def pick() -> None:
            paths = filedialog.askopenfilenames(
                parent=self.root, title=self.t["upload_pick"])
            if paths:
                self._enqueue_uploads(list(paths))

        self._check_storage_ready(pick)

    def _register_drop(self, widget) -> bool:
        """파일 목록을 드롭 대상으로 등록한다(tkinterdnd2가 없으면 False)."""
        return dnd.register(widget, self._on_drop)

    def _on_drop(self, paths: list[str]) -> None:
        """끌어다 놓은 파일을 현재 폴더에 올린다. 폴더는 건너뛰고 알린다."""
        if not self.config_path:
            self._show_banner(self.t["need_config"], level="warning")
            return
        files = [p for p in paths if os.path.isfile(p)]
        if len(files) < len(paths):
            self._show_banner(self.t["upload_drop_dirs"], level="warning")
        if files:
            self._check_storage_ready(lambda: self._enqueue_uploads(files))

    def _enqueue_uploads(self, paths: list[str]) -> None:
        """파일들을 현재 폴더 대상으로 큐에 넣고, 쉬고 있으면 업로드를 시작한다.

        같은 파일이 실패·건너뜀 행으로 남아 있으면 그 행을 지우고 다시 넣는다(재시도).
        이미 대기·진행 중이면 중복으로 넣지 않는다.
        """
        dest = self.vpath  # 탐색 중 폴더가 바뀌어도 대상은 고정
        active = {(i.dest, i.local) for i in self._uploads
                  if i.state not in _FINISHED}
        for p in paths:
            local = os.path.abspath(p)
            if not os.path.isfile(local) or (dest, local) in active:
                continue
            self._uploads[:] = [i for i in self._uploads
                                if (i.dest, i.local) != (dest, local)]
            self._uploads.append(
                UploadItem(local=local, name=os.path.basename(local), dest=dest))
            active.add((dest, local))
        self._render_uploads_now()
        if not self._uploading:
            self._upload_next()

    # --- 진행 ---

    def _render_uploads_now(self) -> None:
        """진행 행을 즉시 반영한다(다음 새로고침을 기다리지 않도록)."""
        self._populate(getattr(self, "_last_rows", []))

    def _upload_next(self) -> None:
        """큐의 다음 항목을 워커에 넘긴다(파일 단위 순차)."""
        pending = [i for i in self._uploads if i.state == "queued"]
        if not pending:
            conflicts = [i for i in self._uploads
                         if i.state == "exists" and i.mode == "error"]
            if conflicts:
                self._ask_conflicts(conflicts)
                return
            self._finish_uploads()
            return

        self._uploading = True
        item = pending[0]
        item.state = "running"
        self._render_uploads_now()
        done_count = self._upload_ok + self._upload_fail + self._upload_skip
        total = done_count + len(pending)
        self._set_progress(done_count, total)
        self._job_begin("upload", self.t["upload_status"].format(
            i=done_count + 1, n=total, name=shorten(item.name)))

        cfg = self.config_path
        remote = item.dest.rstrip("/") + "/" + item.name

        def done(ok, payload):
            if ok:
                self._upload_ok += 1
                self._upload_written = True
                self._uploads.remove(item)
            elif isinstance(payload, actions.RemotePathExists):
                # 같은 경로에 이미 있음 — 배치가 끝나면 한 번에 묻는다.
                item.state = "exists"
            else:
                self._upload_fail += 1
                item.state = "failed"
                item.error = errors.describe(payload, self.t)
                logger.warning("업로드 실패 %s: %r", item.local, payload)
                # 실패마다 배너를 새로 띄우지 않고 이번 배치의 실패 수로 갱신한다.
                self._show_banner(self.t["upload_fail_banner"].format(
                    count=self._upload_fail, name=item.name, msg=item.error))
            self._render_uploads_now()
            self._upload_next()

        mode = item.mode
        self.worker.submit(
            lambda: actions.put_file(cfg, item.local, remote, on_conflict=mode),
            done)

    def _ask_conflicts(self, items: list[UploadItem]) -> None:
        """같은 이름 충돌을 한 번에 묻고, 고른 방식으로 다시 큐에 넣는다."""
        t = self.t

        def chosen(choice: str) -> None:
            if choice in ("overwrite", "rename"):
                for item in items:
                    item.mode = choice
                    item.state = "queued"
            else:
                self._upload_skip += len(items)
                for item in items:
                    if item in self._uploads:
                        self._uploads.remove(item)
            self._render_uploads_now()
            self._upload_next()

        ChoiceDialog(
            self, t["conflict_title"],
            t["conflict_msg"].format(count=len(items)),
            (("rename", t["conflict_rename"]),
             ("overwrite", t["conflict_overwrite"]),
             ("skip", t["conflict_skip"])),
            chosen, cancel_key="skip",
        )

    def _finish_uploads(self) -> None:
        """배치를 마무리한다: 진행 표시를 거두고 결과를 요약한다."""
        self._uploading = False
        self._set_progress(0, 0)
        self._job_end("upload")
        self._set_status(self.t["upload_all_done"].format(
            ok=self._upload_ok, fail=self._upload_fail, skip=self._upload_skip))
        self._upload_ok = self._upload_fail = self._upload_skip = 0
        self._render_uploads_now()
        if self._upload_written:
            self._upload_written = False
            self._after_write()

    # --- 정리 ---

    def _cancel_uploads(self) -> None:
        """아직 시작하지 않은 항목만 큐에서 버린다(진행 중 파일은 끝까지 둔다)."""
        remaining = [i for i in self._uploads if i.state == "queued"]
        if not remaining:
            return
        for item in remaining:
            self._uploads.remove(item)
        self._set_status(self.t["upload_cancelled"].format(n=len(remaining)))
        self._render_uploads_now()

    def _clear_uploads(self) -> None:
        """끝난 표시(실패·이미 있음)를 목록에서 지운다."""
        self._uploads[:] = [i for i in self._uploads if i.state not in _FINISHED]
        self._render_uploads_now()

    def _upload_counts(self) -> tuple[int, int]:
        """(대기 중 수, 끝난 표시 수) — 정리 버튼 노출과 메뉴 활성에 쓴다."""
        queued = sum(1 for i in self._uploads if i.state == "queued")
        finished = sum(1 for i in self._uploads if i.state in _FINISHED)
        return queued, finished

    def _render_uploads(self) -> None:
        """현재 폴더가 대상인 업로드 항목을 진행 행으로 얹는다."""
        for item in getattr(self, "_uploads", []):
            if item.dest != self.vpath:
                continue  # 다른 폴더에 올리는 중
            state = self.t[_STATE_KEYS[item.state]]
            tag = "upload_failed" if item.state == "failed" else "upload"
            iid = self.tree.insert(
                "", 0, values=(f"⬆  {item.name}", "", state), tags=(tag,))
            self._rows[iid] = {"type": "file", "name": item.name,
                               "upload": True}
