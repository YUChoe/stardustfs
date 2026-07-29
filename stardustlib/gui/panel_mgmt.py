"""스토리지·디바이스 관리 패널(메인 창 하단).

디바이스▸소스 2계층 트리와 스토리지 추가/분리 동작. 레지스트리를 단일 원천으로
쓰고, 비로그인·오프라인이면 이 기기 로컬만 보이도록 강등된 응답을 그대로 그린다.
StardustApp에 믹스인으로 결합한다.
"""

from __future__ import annotations

import logging
from tkinter import filedialog, messagebox, simpledialog, ttk

from stardustlib.gui import actions
from stardustlib.gui.format import human_bytes

logger = logging.getLogger(__name__)

# 패널 갱신 주기(ms). 백업이 도는 동안에는 용량이 계속 변하므로 짧게 돌려 진행이
# 화면에 반영되게 한다(유휴 시에는 서버 조회를 아끼려 길게).
_MGMT_POLL_IDLE_MS = 15000
_MGMT_POLL_BACKUP_MS = 3000


class MgmtPanelMixin:
    """디바이스▸소스 트리 + 스토리지 추가/분리."""

    def _build_mgmt_panel(self, parent) -> None:
        """디바이스▸소스 2계층 트리 + 액션(스토리지 추가/분리) 패널."""
        t = self.t
        header = ttk.Frame(parent, padding=(8, 4))
        header.pack(fill="x")
        ttk.Label(header, text=t["mgmt_panel_title"]).pack(side="left")

        tv = ttk.Frame(parent)
        tv.pack(fill="both", expand=True, padx=6)
        self.mgmt_tree = ttk.Treeview(
            tv, columns=("status", "cap"), show="tree headings",
            selectmode="browse",
        )
        self.mgmt_tree.heading("#0", text=f"{t['col_device']} / {t['col_src_name']}")
        self.mgmt_tree.column("#0", width=320, anchor="w")
        self.mgmt_tree.heading("status", text=t["col_status"])
        self.mgmt_tree.column("status", width=90, anchor="w")
        self.mgmt_tree.heading("cap", text=t["col_capacity"])
        self.mgmt_tree.column("cap", width=160, anchor="w")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.mgmt_tree.yview)
        self.mgmt_tree.configure(yscrollcommand=sb.set)
        self.mgmt_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.mgmt_tree.bind("<<TreeviewSelect>>", self._mgmt_on_select)
        self._mgmt_meta: dict = {}  # iid → {kind, self, id}

        bar = ttk.Frame(parent, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text=t["src_add_loop"],
                   command=self._mgmt_add_storage).pack(side="left")
        self.mgmt_detach_btn = ttk.Button(
            bar, text=t["src_remove"], command=self._mgmt_detach, state="disabled")
        self.mgmt_detach_btn.pack(side="left", padx=4)

    def _cap_str(self, d: dict) -> str:
        total, used = d.get("total"), d.get("used")
        if total:
            if used is None:
                return human_bytes(total)
            return f"{human_bytes(used)} / {human_bytes(total)}"
        if used is not None:
            return human_bytes(used)
        return "-"

    def _refresh_mgmt(self) -> None:
        """레지스트리 단일 원천으로 디바이스·소스 트리를 갱신한다(비로그인/오프라인은 강등)."""
        if not self.config_path or not getattr(self, "mgmt_tree", None):
            return
        cfg = self.config_path
        self.worker.submit(
            lambda: actions.storage_and_devices(cfg), self._populate_mgmt)

    def _mgmt_poll(self) -> None:
        """스토리지·디바이스 패널 주기 갱신. 백업 중에는 주기를 줄인다.

        백업은 daemon이 수행하므로 GUI의 쓰기 경로(_after_write)를 타지 않는다.
        진행 중 용량이 멈춘 것처럼 보이지 않도록 여기서 따라간다.
        """
        self._refresh_mgmt()
        delay = (
            _MGMT_POLL_BACKUP_MS if self._showing_progress
            else _MGMT_POLL_IDLE_MS
        )
        self.root.after(delay, self._mgmt_poll)

    def _populate_mgmt(self, ok, data) -> None:
        tv = getattr(self, "mgmt_tree", None)
        if tv is None or not tv.winfo_exists():
            return
        tv.delete(*tv.get_children())
        self._mgmt_meta.clear()
        self.mgmt_detach_btn.config(state="disabled")
        if not ok or not isinstance(data, dict):
            return
        # 디바이스 온라인/전체 요약을 같은 응답에서 계산한다(별도 GET /devices 없음).
        # 서버 미도달(강등)일 때는 이 기기 로컬만 보이므로 카운트를 건드리지 않는다.
        if data.get("online"):
            devices = data.get("devices", [])
            self._show_device_summary({
                "online": sum(1 for d in devices if d.get("online")),
                "total": len(devices),
            })
        t = self.t
        for d in data.get("devices", []):
            name = d.get("name") or "?"
            if d.get("self"):
                name = f"{name} ({t['this_device']})"
            dstatus = t["status_online"] if d.get("online") else t["status_offline"]
            did = tv.insert("", "end", text=name, values=(dstatus, ""), open=True)
            self._mgmt_meta[did] = {"kind": "device", "self": bool(d.get("self"))}
            for s in d.get("sources", []):
                if not s.get("online"):
                    st = t["status_offline"]
                else:
                    st = (t["src_ready"] if s.get("state") == "ready"
                          else t["src_initializing"])
                # Treeview가 계층을 들여쓰므로 텍스트에 공백을 넣지 않는다.
                sid = tv.insert(
                    did, "end", text=s.get("source_id") or "",
                    values=(st, self._cap_str(s)))
                self._mgmt_meta[sid] = {
                    "kind": "source", "self": bool(d.get("self")),
                    "id": s.get("source_id")}

    def _mgmt_on_select(self, _e=None) -> None:
        sel = self.mgmt_tree.selection()
        meta = self._mgmt_meta.get(sel[0], {}) if sel else {}
        can_detach = meta.get("kind") == "source" and meta.get("self")
        self.mgmt_detach_btn.config(state="normal" if can_detach else "disabled")

    def _mgmt_add_storage(self) -> None:
        """이 기기에 루프백 스토리지를 추가한다(add_source→포맷→데몬 리로드)."""
        t = self.t
        cfg = self.config_path
        if not cfg:
            messagebox.showwarning(t["app_title"], t["need_config"])
            return
        path = filedialog.asksaveasfilename(
            title=t["src_loop_path"], defaultextension=".img")
        if not path:
            return
        mb = simpledialog.askinteger(
            "loopback", t["src_loop_size_prompt"], initialvalue=100, minvalue=10)
        if not mb:
            return
        try:
            sid = actions.add_source(cfg, "loopback", path, size=mb * 1024 * 1024)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t["err"], str(e))
            return
        self._set_status(t["src_init_busy"])
        self._refresh_mgmt()  # '초기화 중' 즉시 표시

        def _fmt():
            actions.create_storage_image(cfg, sid)
            actions.invalidate(cfg)

        def _fmt_done(ok, payload):
            if not ok:
                self._set_status(self.t["err_status"].format(msg=payload))
                messagebox.showerror(self.t["err"], str(payload))
                return
            self._set_status(self.t["ready"])
            self._reload_daemon()
            self._refresh_mgmt()
            self.refresh()

        self.worker.submit(_fmt, _fmt_done)

    def _mgmt_detach(self) -> None:
        """선택한 이 기기의 소스를 evacuate 후 분리하고 빈 이미지를 삭제한다."""
        t = self.t
        cfg = self.config_path
        sel = self.mgmt_tree.selection()
        if not sel:
            return
        meta = self._mgmt_meta.get(sel[0], {})
        if meta.get("kind") != "source" or not meta.get("self"):
            messagebox.showinfo(t["src_remove"], t["src_remote_no_detach"])
            return
        sid = meta.get("id")
        if not messagebox.askyesno(
            t["src_remove"], t["src_detach_confirm"].format(id=sid)
        ):
            return

        def done(ok, report):
            if not ok:
                messagebox.showerror(t["err"], str(report))
                return
            if report.get("detached"):
                messagebox.showinfo(t["src_remove"], t["src_detach_done"].format(
                    moved=len(report.get("moved", []))))
                self._reload_daemon()
                img = report.get("image_path")
                if img:
                    self.worker.submit(
                        lambda: actions.delete_storage_image(img),
                        lambda *_a: None)
            else:
                messagebox.showwarning(
                    t["src_remove"], t["src_detach_blocked"].format(
                        unmoved=len(report.get("unmoved", []))))
            self._refresh_mgmt()
            self.refresh()

        self._set_status(t["src_detach_busy"])
        self.worker.submit(lambda: actions.detach_source(cfg, sid), done)
