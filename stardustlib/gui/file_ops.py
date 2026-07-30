"""파일 동작 — 전송(업로드/다운로드), 파일 조작, 백업/복구.

StardustApp에 믹스인으로 결합한다. 선택된 행을 읽어 actions를 호출하고 결과를
상태바에 표시하는 계층으로, 위젯 구성은 panel_files.py가 담당한다.

업로드는 파일 선택만 받고 곧바로 시작한다. 진행은 별도 창이 아니라 파일 목록의
행으로 보여준다(_render_uploads) — 사용자가 보고 있는 화면에서 상태가 바뀌는 편이
창을 하나 더 띄우는 것보다 읽기 쉽다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from tkinter import filedialog, messagebox

from stardustlib.gui import actions
from stardustlib.gui.format import human_bytes, shorten
from stardustlib.gui.widgets.form_dialog import INT, SAVE, Field, FormDialog
from stardustlib.gui.widgets.vpath_picker import VPathPicker

logger = logging.getLogger(__name__)


@dataclass
class UploadItem:
    """업로드 큐의 한 항목. 목록에 진행 행으로 그려진다."""

    local: str
    name: str
    dest: str                 # 선택 시점의 가상 폴더(고정)
    state: str = "queued"     # queued | running | failed | exists
    error: str = ""


class FileOpsMixin:
    """파일 목록에서 실행하는 동작들."""

    # --- 전송 ---

    def _transfers_blocked(self) -> bool:
        """스토리지 초기화 중이면 안내 후 True를 반환해 전송을 막는다.

        스토리지 추가 직후 데몬이 FAT 이미지를 생성·포맷하는 동안, 반쯤 만들어진
        소스로의 업로드/다운로드를 방지한다.
        """
        if self.config_path and actions.storage_initializing(self.config_path):
            self._show_banner(self.t["transfer_init_block"], level="warning")
            return True
        return False

    def _upload(self) -> None:
        """파일을 고르면 곧바로 업로드를 시작한다(별도 업로드 창 없음)."""
        if not self.config_path:
            self._show_banner(self.t["need_config"], level="warning")
            return
        if self._transfers_blocked():
            return
        paths = filedialog.askopenfilenames(
            parent=self.root, title=self.t["upload_pick"])
        if not paths:
            return
        dest = self.vpath  # 탐색 중 폴더가 바뀌어도 대상은 고정
        known = {(i.dest, i.local) for i in self._uploads}
        for p in paths:
            local = os.path.abspath(p)
            if not os.path.isfile(local) or (dest, local) in known:
                continue
            self._uploads.append(
                UploadItem(local=local, name=os.path.basename(local), dest=dest))
        self._render_uploads_now()
        if not self._uploading:
            self._upload_next()

    def _render_uploads_now(self) -> None:
        """진행 행을 즉시 반영한다(다음 새로고침을 기다리지 않도록)."""
        self._populate(getattr(self, "_last_rows", []))

    def _upload_next(self) -> None:
        """큐의 다음 항목을 워커에 넘긴다(파일 단위 순차)."""
        pending = [i for i in self._uploads if i.state == "queued"]
        if not pending:
            self._uploading = False
            self._set_progress(0, 0)
            self._set_status(self.t["upload_all_done"].format(
                ok=self._upload_ok, fail=self._upload_fail,
                skip=self._upload_skip))
            self._upload_ok = self._upload_fail = self._upload_skip = 0
            if self._upload_written:
                self._upload_written = False
                self._after_write()
            return

        self._uploading = True
        item = pending[0]
        item.state = "running"
        self._render_uploads_now()
        done_count = self._upload_ok + self._upload_fail + self._upload_skip
        total = done_count + len(pending)
        self._set_status(self.t["upload_status"].format(
            i=done_count + 1, n=total, name=shorten(item.name)))
        self._set_progress(done_count, total)

        cfg = self.config_path
        remote = item.dest.rstrip("/") + "/" + item.name

        def done(ok, payload):
            if ok:
                self._upload_ok += 1
                self._upload_written = True
                self._uploads.remove(item)
            elif isinstance(payload, actions.RemotePathExists):
                # 같은 경로에 이미 있음 — 덮어쓰지 않고 표시만 남긴다.
                self._upload_skip += 1
                item.state = "exists"
            else:
                self._upload_fail += 1
                item.state = "failed"
                item.error = str(payload)
                self._show_banner(
                    self.t["upload_log_fail"].format(
                        name=item.name, msg=payload))
            self._render_uploads_now()
            self._upload_next()

        self.worker.submit(lambda: actions.put_file(cfg, item.local, remote), done)

    def _cancel_uploads(self) -> None:
        """아직 시작하지 않은 항목만 큐에서 버린다(진행 중 파일은 끝까지 둔다)."""
        remaining = [i for i in self._uploads if i.state == "queued"]
        if not remaining:
            # 끝난 표시(실패·이미 있음)를 정리하는 용도로도 쓴다.
            self._uploads[:] = [i for i in self._uploads if i.state == "running"]
            self._render_uploads_now()
            return
        for item in remaining:
            self._uploads.remove(item)
        self._set_status(self.t["upload_cancelled"].format(n=len(remaining)))
        self._render_uploads_now()

    def _download(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            return
        if self._transfers_blocked():
            return
        remote = self._join(row["name"])
        local = filedialog.asksaveasfilename(
            title=self.t["save_to"], initialfile=row["name"]
        )
        if not local:
            return
        cfg = self.config_path
        self._submit(
            lambda: actions.get_file(cfg, remote, local),
            lambda n: self._set_status(
                self.t["download_done"].format(path=local, size=human_bytes(n))
            ),
            self.t["downloading"].format(name=row["name"]),
        )

    # --- 파일 조작 ---

    def _mkdir(self) -> None:
        t = self.t
        fields = (Field("name", t["mkdir_prompt"], required=True),)

        def submit(values, dlg) -> None:
            cfg = self.config_path
            path = self._join(values["name"])

            def done(ok, payload):
                if not ok:
                    dlg.error(str(payload))
                    return
                dlg.close()
                self._after_write()

            self._set_status(t["mkdir_busy"])
            self.worker.submit(lambda: actions.mkdir(cfg, path), done)

        FormDialog(self, t["mkdir"], fields, submit)

    def _delete(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        if len(rows) == 1:
            prompt = self.t["delete_confirm"].format(name=rows[0]["name"])
        else:
            prompt = self.t["delete_confirm_many"].format(count=len(rows))
        # 되돌릴 수 없는 동작이므로 확인은 모달로 남긴다.
        if not messagebox.askyesno(self.t["delete"], prompt):
            return
        cfg = self.config_path
        items = [(self._join(r["name"]), r["type"] == "dir") for r in rows]
        # 다중 선택도 온라인 세션 1회로 일괄 삭제 + 1회 전파.
        self._submit(lambda: actions.remove_many(cfg, items),
                     lambda _n: self._after_write(), self.t["delete_busy"])

    def _rename(self) -> None:
        """현재 폴더 안에서 이름만 바꾼다(경로는 건드리지 않는다)."""
        row = self._selected()
        if not row:
            return
        t = self.t
        old = row["name"]
        fields = (Field("name", t["rename_prompt"], initial=old, required=True),)

        def submit(values, dlg) -> None:
            new = values["name"]
            if new == old:
                dlg.close()
                return
            cfg = self.config_path
            src, dst = self._join(old), self._join(new)

            def done(ok, payload):
                if not ok:
                    dlg.error(str(payload))
                    return
                dlg.close()
                self._after_write()

            self._set_status(t["rename_busy"])
            self.worker.submit(lambda: actions.move(cfg, src, dst), done)

        FormDialog(self, t["rename"], fields, submit)

    def _move(self) -> None:
        """대상 폴더를 트리에서 골라 옮긴다."""
        row = self._selected()
        if not row:
            return
        src = self._join(row["name"])

        def picked(folder: str) -> None:
            dst = folder.rstrip("/") + "/" + row["name"]
            if dst == src:
                return
            cfg = self.config_path
            self._submit(lambda: actions.move(cfg, src, dst),
                         lambda _r: self._after_write(), self.t["move_busy"])

        VPathPicker(self, self.vpath, picked)

    def _copy(self) -> None:
        """대상 폴더를 트리에서 골라 복사한다(같은 폴더면 이름을 바꿔 준다)."""
        row = self._selected()
        if not row or row["type"] != "file":
            return
        src = self._join(row["name"])

        def picked(folder: str) -> None:
            name = row["name"]
            dst = folder.rstrip("/") + "/" + name
            if dst == src:
                dst = folder.rstrip("/") + "/" + self.t["copy_prefix"] + name
            cfg = self.config_path
            self._submit(lambda: actions.copy(cfg, src, dst),
                         lambda _r: self._after_write(), self.t["copy_busy"])

        VPathPicker(self, self.vpath, picked)

    # --- 백업 / 복구 ---

    def _selected_files(self) -> list[dict]:
        """선택된 행 중 파일만(없으면 빈 리스트).

        버튼·메뉴가 선택 상태를 따라 비활성되므로 여기서 안내 창을 띄우지 않는다.
        """
        return [r for r in self._selected_rows() if r["type"] == "file"]

    def _backup_selected(self) -> None:
        files = self._selected_files()
        if not files:
            return
        cfg = self.config_path
        paths = [self._join(r["name"]) for r in files]
        self._submit(
            lambda: actions.backup_paths(cfg, paths),
            self._show_backup_result, self.t["backup_busy"],
        )

    def _heal_selected(self) -> None:
        files = self._selected_files()
        if not files:
            return
        cfg = self.config_path
        paths = [self._join(r["name"]) for r in files]
        self._submit(
            lambda: actions.heal_paths(cfg, paths),
            self._show_backup_result, self.t["heal_busy"],
        )

    def _show_backup_result(self, results: list) -> None:
        ok = sum(1 for r in results if r.get("status") == "replicated")
        pending = sum(1 for r in results if r.get("status") != "replicated")
        text = self.t["backup_done"].format(ok=ok, pending=pending)
        # 다른 기기가 보관한 청크는 그 기기에 위임했다(데이터 왕복 없음).
        delegated = sum(r.get("delegated", 0) for r in results)
        if delegated:
            text += self.t["backup_delegated"].format(count=delegated)
        unreachable = sorted({
            d for r in results for d in r.get("unreachable", [])
        })
        if unreachable:
            text += self.t["backup_delegate_offline"].format(
                devices=", ".join(d[:8] for d in unreachable)
            )
        self._set_status(text)
        self.refresh()  # 상태 컬럼·요약 갱신

    def _restore_selected(self) -> None:
        files = self._selected_files()
        if not files:
            return
        cfg = self.config_path
        paths = [self._join(r["name"]) for r in files]
        self._submit(
            lambda: actions.restore_paths(cfg, paths),
            self._show_restore_result, self.t["restore_busy"],
        )

    def _show_restore_result(self, results: list) -> None:
        ok = sum(1 for r in results if r.get("status") == "restored")
        failed = sum(1 for r in results if r.get("status") != "restored")
        self._set_status(self.t["restore_done"].format(ok=ok, failed=failed))
        self.refresh()  # 상태 컬럼·요약 갱신

    def _announce_selected(self) -> None:
        """선택 파일의 백업을 데몬에 즉시 요청한다(주기 대기 없음)."""
        files = self._selected_files()
        if not files:
            return
        cfg = self.config_path
        paths = [self._join(r["name"]) for r in files]
        self._submit(
            lambda: actions.announce_paths(cfg, paths),
            self._show_announce_result, self.t["announce_busy"],
        )

    def _show_announce_result(self, result: dict) -> None:
        """announce 결과를 상태바에 표시한다(데몬 미실행이면 안내)."""
        if not result.get("daemon"):
            self._show_banner(self.t["announce_no_daemon"], level="warning")
            return
        self._set_status(
            self.t["announce_done"].format(count=result.get("announced", 0))
        )


# 스토리지 추가 폼(관리 패널에서 쓴다) — 파일 동작과 같은 폼 부품을 공유한다.
def storage_fields(t: dict) -> tuple[Field, ...]:
    """루프백 스토리지 추가 입력(이미지 경로 + 크기)."""
    return (
        Field("path", t["src_loop_path"], SAVE, required=True,
              pick_title=t["src_loop_path"]),
        Field("size_mb", t["src_loop_size_prompt"], INT, initial="100",
              required=True, minimum=10),
    )
