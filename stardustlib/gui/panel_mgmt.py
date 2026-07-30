"""스토리지·디바이스 관리 패널(메인 창 하단).

디바이스▸소스 2계층 트리와 스토리지 추가/분리 동작. 레지스트리를 단일 원천으로
쓰고, 비로그인·오프라인이면 이 기기 로컬만 보이도록 강등된 응답을 그대로 그린다.
StardustApp에 믹스인으로 결합한다.
"""

from __future__ import annotations

import logging
import os
from tkinter import messagebox, ttk

from stardustlib.gui import actions, prefs
from stardustlib.gui.file_ops import storage_fields
from stardustlib.gui.format import human_bytes
from stardustlib.gui.widgets import tooltip
from stardustlib.gui.widgets.form_dialog import FormDialog

logger = logging.getLogger(__name__)

# 패널 갱신 주기(ms). 백업이 도는 동안에는 용량이 계속 변하므로 짧게 돌려 진행이
# 화면에 반영되게 한다(유휴 시에는 서버 조회를 아끼려 길게).
_MGMT_POLL_IDLE_MS = 15000
_MGMT_POLL_BACKUP_MS = 3000

# 이 비율 이상 차면 경고 색으로 표시한다.
_FULL_RATIO = 0.9


class MgmtPanelMixin:
    """디바이스▸소스 트리 + 스토리지 추가/분리."""

    def _build_mgmt_panel(self, parent) -> None:
        """디바이스▸소스 2계층 트리 + 액션(스토리지 추가/분리) 패널.

        액션 바를 트리보다 먼저 pack한다 — pack은 선언 순서로 공간을 떼어 가므로
        expand=True인 트리를 먼저 배치하면 소스가 여럿일 때 트리가 패널 높이를 전부
        가져가고 액션 바가 1px로 붕괴한다(스토리지 추가·분리에 도달할 수 없게 된다).
        """
        t = self.t
        header = ttk.Frame(parent, padding=(8, 4))
        header.pack(fill="x")
        self._mgmt_toggle = ttk.Button(
            header, text="", width=3, style="Toolbutton",
            command=self._toggle_mgmt)
        self._mgmt_toggle.pack(side="left", padx=(0, 4))
        ttk.Label(header, text=t["mgmt_panel_title"]).pack(side="left")

        bar = ttk.Frame(parent, padding=(8, 6))
        bar.pack(fill="x", side="bottom")
        self.mgmt_actionbar = bar
        ttk.Button(bar, text=t["src_add_loop"],
                   command=self._mgmt_add_storage).pack(side="left")
        self.mgmt_detach_btn = ttk.Button(
            bar, text=t["src_remove"], command=self._mgmt_detach, state="disabled")
        self.mgmt_detach_btn.pack(side="left", padx=4)

        # 트리 컨테이너는 _apply_mgmt_collapsed가 전담해 pack한다 — 여기서 미리
        # pack하면 접었다 펼 때 pack 순서가 액션 바보다 앞으로 가 액션 바가 붕괴한다.
        tv = ttk.Frame(parent)
        self._mgmt_body = tv
        self.mgmt_tree = ttk.Treeview(
            tv, columns=("status", "cap"), show="tree headings",
            selectmode="browse",
        )
        self.mgmt_tree.heading("#0", text=f"{t['col_device']} / {t['col_src_name']}")
        self.mgmt_tree.column("#0", width=320, anchor="w")
        self.mgmt_tree.heading("status", text=t["col_status"])
        self.mgmt_tree.column("status", width=90, anchor="w")
        self.mgmt_tree.heading("cap", text=t["col_capacity"])
        self.mgmt_tree.column("cap", width=190, anchor="w")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.mgmt_tree.yview)
        self.mgmt_tree.configure(yscrollcommand=sb.set)
        self.mgmt_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.mgmt_tree.bind("<<TreeviewSelect>>", self._mgmt_on_select)
        self.mgmt_tree.bind("<Motion>", self._mgmt_motion)
        self._apply_mgmt_tags()
        self._mgmt_meta: dict = {}  # iid → {kind, self, id, path}
        tooltip.attach(self.mgmt_tree, lambda: getattr(self, "_mgmt_tip", ""))
        self._apply_mgmt_collapsed()

    def _apply_mgmt_tags(self) -> None:
        """가득 찬 소스 경고 색을 현재 테마에 맞춘다."""
        from stardustlib.gui import theme

        tree = getattr(self, "mgmt_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        tree.tag_configure(
            "full", foreground=theme.text_colour(
                "danger", dark=self.theme == "dark"))

    # --- 접기 ---

    def _toggle_mgmt(self) -> None:
        self._mgmt_collapsed = not self._mgmt_collapsed
        prefs.save(mgmt_collapsed=self._mgmt_collapsed)
        self._apply_mgmt_collapsed()

    def _apply_mgmt_collapsed(self) -> None:
        """접힘 상태를 화면에 반영한다(트리만 숨기고 헤더·액션 바는 남긴다).

        펼 때는 `before=`를 쓰지 않는다 — 액션 바보다 앞 순서로 넣으면 expand인
        트리가 공간을 먼저 가져가 액션 바가 1px로 붕괴한다.
        """
        collapsed = self._mgmt_collapsed
        self._mgmt_toggle.config(text="▸" if collapsed else "▾")
        body = getattr(self, "_mgmt_body", None)
        if body is None:
            return
        if collapsed:
            body.pack_forget()
        else:
            body.pack(fill="both", expand=True, padx=6)

    # --- 표시 ---

    def _source_label(self, s: dict) -> str:
        """소스를 사람이 읽을 이름으로 — '루프백 · dev-a.img'.

        응답에 경로가 없으면 소스 ID를 그대로 쓴다(추측하지 않는다).
        """
        path = s.get("path") or ""
        kind = s.get("type") or s.get("kind") or ""
        base = os.path.basename(path) if path else ""
        if not base:
            return s.get("source_id") or ""
        kind_label = self.t.get(f"src_kind_{kind}", kind) if kind else ""
        return f"{kind_label} · {base}" if kind_label else base

    def _cap_str(self, d: dict) -> str:
        total, used = d.get("total"), d.get("used")
        if total:
            if used is None:
                return human_bytes(total)
            pct = round(used / total * 100)
            return f"{human_bytes(used)} / {human_bytes(total)} ({pct}%)"
        if used is not None:
            return human_bytes(used)
        return "-"

    @staticmethod
    def _is_full(d: dict) -> bool:
        total, used = d.get("total"), d.get("used")
        return bool(total) and used is not None and used / total >= _FULL_RATIO

    def _mgmt_motion(self, event) -> None:
        """행 위에서 소스 전체 경로·ID를 툴팁으로 보여준다."""
        iid = self.mgmt_tree.identify_row(event.y)
        meta = self._mgmt_meta.get(iid, {})
        if meta.get("kind") != "source":
            self._mgmt_tip = ""
            return
        parts = [meta.get("id") or ""]
        if meta.get("path"):
            parts.append(meta["path"])
        self._mgmt_tip = "\n".join(p for p in parts if p)

    # --- 갱신 ---

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
                    did, "end", text=self._source_label(s),
                    values=(st, self._cap_str(s)),
                    tags=("full",) if self._is_full(s) else ())
                self._mgmt_meta[sid] = {
                    "kind": "source", "self": bool(d.get("self")),
                    "id": s.get("source_id"), "path": s.get("path") or ""}

    def _mgmt_on_select(self, _e=None) -> None:
        sel = self.mgmt_tree.selection()
        meta = self._mgmt_meta.get(sel[0], {}) if sel else {}
        can_detach = meta.get("kind") == "source" and meta.get("self")
        self.mgmt_detach_btn.config(state="normal" if can_detach else "disabled")

    # --- 동작 ---

    def _mgmt_add_storage(self) -> None:
        """이 기기에 루프백 스토리지를 추가한다(add_source→포맷→데몬 리로드)."""
        t = self.t
        if not self.config_path:
            self._show_banner(t["need_config"], level="warning")
            return

        def submit(values, dlg) -> None:
            cfg = self.config_path
            try:
                sid = actions.add_source(
                    cfg, "loopback", values["path"],
                    size=values["size_mb"] * 1024 * 1024)
            except Exception as e:  # noqa: BLE001 — 사유를 폼 안에 남긴다
                dlg.error(str(e))
                return
            dlg.close()
            self._set_status(t["src_init_busy"])
            self._refresh_mgmt()  # '초기화 중' 즉시 표시

            def _fmt():
                actions.create_storage_image(cfg, sid)
                actions.invalidate(cfg)

            def _fmt_done(ok, payload):
                if not ok:
                    self._show_banner(str(payload))
                    return
                self._set_status(self.t["ready"])
                self._reload_daemon()
                self._refresh_mgmt()
                self.refresh()

            self.worker.submit(_fmt, _fmt_done)

        FormDialog(self, t["src_add_loop"], storage_fields(t), submit)

    def _mgmt_detach(self) -> None:
        """선택한 이 기기의 소스를 evacuate 후 분리하고 빈 이미지를 삭제한다."""
        t = self.t
        cfg = self.config_path
        sel = self.mgmt_tree.selection()
        if not sel:
            return
        meta = self._mgmt_meta.get(sel[0], {})
        if meta.get("kind") != "source" or not meta.get("self"):
            self._show_banner(t["src_remote_no_detach"], level="warning")
            return
        sid = meta.get("id")
        # 데이터를 옮기는 되돌리기 어려운 동작이라 확인은 모달로 남긴다.
        if not messagebox.askyesno(
            t["src_remove"], t["src_detach_confirm"].format(id=sid)
        ):
            return

        def done(ok, report):
            if not ok:
                self._show_banner(str(report))
                return
            if report.get("detached"):
                self._set_status(t["src_detach_done"].format(
                    moved=len(report.get("moved", []))))
                self._reload_daemon()
                img = report.get("image_path")
                if img:
                    self.worker.submit(
                        lambda: actions.delete_storage_image(img),
                        lambda *_a: None)
            else:
                self._show_banner(
                    t["src_detach_blocked"].format(
                        unmoved=len(report.get("unmoved", []))),
                    level="warning")
            self._refresh_mgmt()
            self.refresh()

        self._set_status(t["src_detach_busy"])
        self.worker.submit(lambda: actions.detach_source(cfg, sid), done)
