"""파일 동작 — 다운로드, 파일 조작, 백업/복구.

StardustApp에 믹스인으로 결합한다. 선택된 행을 읽어 actions를 호출하고 결과를
상태바에 표시하는 계층으로, 위젯 구성은 panel_files.py, 업로드 큐는 uploads.py가
담당한다.
"""

from __future__ import annotations

import logging
from tkinter import filedialog, messagebox

from stardustlib.gui import actions, errors
from stardustlib.gui.format import human_bytes
from stardustlib.gui.widgets.form_dialog import Field, FormDialog
from stardustlib.gui.widgets.vpath_picker import VPathPicker

logger = logging.getLogger(__name__)


class FileOpsMixin:
    """파일 목록에서 실행하는 동작들."""

    # --- 전송 ---

    def _download(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            return
        remote = self._join(row["name"])

        def pick() -> None:
            local = filedialog.asksaveasfilename(
                parent=self.root, title=self.t["save_to"],
                initialfile=row["name"],
            )
            if not local:
                return
            cfg = self.config_path
            self._submit(
                lambda: actions.get_file(cfg, remote, local),
                lambda n: self._set_status(
                    self.t["download_done"].format(
                        path=local, size=human_bytes(n))
                ),
                self.t["downloading"].format(name=row["name"]),
            )

        self._check_storage_ready(pick)

    # --- 파일 조작 ---

    def _form_job(self, dlg, fn, busy: str) -> None:
        """폼에서 시작한 쓰기 작업 — 실패는 폼 안에, 성공하면 닫고 새로고침한다."""
        def done(ok, payload):
            self._job_end(busy)
            if not ok:
                dlg.error(errors.describe(payload, self.t))
                return
            dlg.close()
            self._after_write()

        self._job_begin(busy, busy)
        self.worker.submit(fn, done)

    def _mkdir(self) -> None:
        t = self.t
        fields = (Field("name", t["mkdir_prompt"], required=True),)

        def submit(values, dlg) -> None:
            cfg, path = self.config_path, self._join(values["name"])
            self._form_job(dlg, lambda: actions.mkdir(cfg, path), t["mkdir_busy"])

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
        if not messagebox.askyesno(self.t["delete"], prompt, parent=self.root):
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
            self._form_job(dlg, lambda: actions.move(cfg, src, dst),
                           t["rename_busy"])

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

    def _run_on_selected_files(self, action, on_ok, busy_key: str) -> None:
        """선택 파일의 가상 경로 목록으로 action(config, paths)을 실행한다."""
        files = self._selected_files()
        if not files:
            return
        cfg = self.config_path
        paths = [self._join(r["name"]) for r in files]
        self._submit(lambda: action(cfg, paths), on_ok, self.t[busy_key])

    def _backup_selected(self) -> None:
        self._run_on_selected_files(
            actions.backup_paths, self._show_backup_result, "backup_busy")

    def _heal_selected(self) -> None:
        self._run_on_selected_files(
            actions.heal_paths, self._show_backup_result, "heal_busy")

    def _restore_selected(self) -> None:
        self._run_on_selected_files(
            actions.restore_paths, self._show_restore_result, "restore_busy")

    def _announce_selected(self) -> None:
        """선택 파일의 백업을 데몬에 즉시 요청한다(주기 대기 없음)."""
        self._run_on_selected_files(
            actions.announce_paths, self._show_announce_result, "announce_busy")

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

    def _show_restore_result(self, results: list) -> None:
        ok = sum(1 for r in results if r.get("status") == "restored")
        failed = sum(1 for r in results if r.get("status") != "restored")
        self._set_status(self.t["restore_done"].format(ok=ok, failed=failed))
        self.refresh()  # 상태 컬럼·요약 갱신

    def _show_announce_result(self, result: dict) -> None:
        """announce 결과를 상태바에 표시한다(데몬 미실행이면 안내)."""
        if not result.get("daemon"):
            self._show_banner(self.t["announce_no_daemon"], level="warning")
            return
        self._set_status(
            self.t["announce_done"].format(count=result.get("announced", 0))
        )
