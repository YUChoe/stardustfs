"""파일 동작 — 전송(업로드/다운로드), 파일 조작, 백업/복구.

StardustApp에 믹스인으로 결합한다. 선택된 행을 읽어 actions를 호출하고 결과를
상태바에 표시하는 계층으로, 위젯 구성은 panel_files.py가 담당한다.
"""

from __future__ import annotations

import logging
from tkinter import filedialog, messagebox, simpledialog

from stardustlib.gui import actions
from stardustlib.gui.format import human_bytes

logger = logging.getLogger(__name__)


class FileOpsMixin:
    """파일 목록에서 실행하는 동작들."""

    # --- 전송 ---

    def _transfers_blocked(self) -> bool:
        """스토리지 초기화 중이면 안내 후 True를 반환해 전송을 막는다.

        스토리지 추가 직후 데몬이 FAT 이미지를 생성·포맷하는 동안, 반쯤 만들어진
        소스로의 업로드/다운로드를 방지한다.
        """
        if self.config_path and actions.storage_initializing(self.config_path):
            messagebox.showinfo(self.t["app_title"], self.t["transfer_init_block"])
            return True
        return False

    def _upload(self) -> None:
        if not self.config_path:
            messagebox.showwarning(self.t["app_title"], self.t["need_config"])
            return
        if self._transfers_blocked():
            return
        from stardustlib.gui.upload_dialog import UploadDialog

        UploadDialog(self)

    def _download(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            messagebox.showinfo(self.t["app_title"], self.t["download_pick"])
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
        name = simpledialog.askstring(self.t["mkdir"], self.t["mkdir_prompt"])
        if not name:
            return
        cfg = self.config_path
        path = self._join(name)
        self._submit(lambda: actions.mkdir(cfg, path),
                     lambda _r: self._after_write(), self.t["mkdir_busy"])

    def _delete(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        if len(rows) == 1:
            prompt = self.t["delete_confirm"].format(name=rows[0]["name"])
        else:
            prompt = self.t["delete_confirm_many"].format(count=len(rows))
        if not messagebox.askyesno(self.t["delete"], prompt):
            return
        cfg = self.config_path
        items = [(self._join(r["name"]), r["type"] == "dir") for r in rows]
        # 다중 선택도 온라인 세션 1회로 일괄 삭제 + 1회 전파.
        self._submit(lambda: actions.remove_many(cfg, items),
                     lambda _n: self._after_write(), self.t["delete_busy"])

    def _move(self) -> None:
        row = self._selected()
        if not row:
            return
        src = self._join(row["name"])
        dst = simpledialog.askstring(self.t["move"], self.t["move_prompt"],
                                     initialvalue=src)
        if not dst or dst == src:
            return
        cfg = self.config_path
        self._submit(lambda: actions.move(cfg, src, dst),
                     lambda _r: self._after_write(), self.t["move_busy"])

    def _copy(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            messagebox.showinfo(self.t["app_title"], self.t["copy_pick"])
            return
        src = self._join(row["name"])
        dst = simpledialog.askstring(self.t["copy"], self.t["copy_prompt"],
                                     initialvalue=self._join("copy-" + row["name"]))
        if not dst:
            return
        cfg = self.config_path
        self._submit(lambda: actions.copy(cfg, src, dst),
                     lambda _r: self._after_write(), self.t["copy_busy"])

    # --- 백업 / 복구 ---

    def _selected_files(self) -> list[dict]:
        """선택된 행 중 파일만(없으면 안내 후 빈 리스트)."""
        files = [r for r in self._selected_rows() if r["type"] == "file"]
        if not files:
            messagebox.showinfo(self.t["app_title"], self.t["backup_pick"])
        return files

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
            self._set_status(self.t["announce_no_daemon"])
            return
        self._set_status(
            self.t["announce_done"].format(count=result.get("announced", 0))
        )
