"""StardustFS Tkinter GUI.

파일 탐색기(목록/업로드/다운로드/폴더/삭제/이동/복사) + 디바이스 + 스토리지 소스
관리 + daemon 제어 + 로그인/로그아웃. i18n(ko/en), 시스템 트레이 최소화(선택 의존
pystray) 지원. 창 닫기(X)는 트레이로 숨기고, 트레이 '종료'로만 실제 종료한다.

네트워크/파일 작업은 워커 스레드에서 수행하고 결과를 메인 스레드로 전달한다.
"""

from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from stardustlib.gui import actions, i18n, tray
from stardustlib.gui.worker import Worker

logger = logging.getLogger(__name__)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class StardustApp:
    """메인 윈도우."""

    def __init__(self, root: tk.Tk, config_path: str | None) -> None:
        self.root = root
        self.config_path = config_path
        self.vpath = "/"
        self.worker = Worker()
        self._rows: dict[str, dict] = {}
        self._auto_started = False
        self.lang = i18n.detect_lang()
        self.t = i18n.get_text(self.lang)

        root.title(self.t["app_title"])
        root.geometry("800x540")
        self._build_menu()
        self.body = ttk.Frame(root)
        self.body.pack(fill="both", expand=True)
        self._build_body()
        self._setup_tray()

        self.root.after(80, self._tick)
        self.root.after(200, self._refresh_daemon)
        if self.config_path:
            self.refresh()
        else:
            self._set_status(self.t["select_config_hint"])

    # --- 메뉴 / 트레이 ---

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        lang_menu = tk.Menu(menubar, tearoff=0)
        lang_menu.add_command(label=self.t["lang_ko"],
                              command=lambda: self._set_language("ko"))
        lang_menu.add_command(label=self.t["lang_en"],
                              command=lambda: self._set_language("en"))
        menubar.add_cascade(label=self.t["menu_language"], menu=lang_menu)
        self.root.config(menu=menubar)

    def _setup_tray(self) -> None:
        self.tray_icon = tray.build_icon(
            self.t["app_title"],
            lambda: self.t["tray_open"],
            lambda: self.t["tray_quit"],
            lambda: self.root.after(0, self._show_window),
            lambda: self.root.after(0, self._quit),
        )
        if self.tray_icon is not None:
            threading.Thread(
                target=self.tray_icon.run, daemon=True, name="stardust-tray"
            ).start()
            # 창 닫기(X) → 트레이로 숨김(종료 아님)
            self.root.protocol("WM_DELETE_WINDOW", self._hide_window)
        else:
            # 트레이 미사용(pystray/Pillow 미설치 등): 창 닫기 = 종료. 사유를 노출.
            self.root.protocol("WM_DELETE_WINDOW", self._quit)
            why = tray.reason()
            logger.warning(
                "시스템 트레이 비활성(%s). 창 닫기=종료. 트레이를 쓰려면 "
                "pystray/Pillow 설치: pip install -r requirements.txt", why,
            )
            self.root.after(
                600, lambda: self._set_status(self.t["tray_disabled_hint"])
            )

    def _hide_window(self) -> None:
        self.root.withdraw()
        self._set_status(self.t["tray_minimised"])

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def _quit(self) -> None:
        # 종료 시 daemon도 정지(센티넬만 즉시 생성, 대기 없음 — daemon이 ~1초 내 종료).
        if self.config_path:
            try:
                actions.daemon_signal_stop(self.config_path)
            except Exception:  # noqa: BLE001
                pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    def _set_language(self, lang: str) -> None:
        if lang == self.lang:
            return
        self.lang = lang
        self.t = i18n.get_text(lang)
        self.root.title(self.t["app_title"])
        self._build_menu()
        self.body.destroy()
        self.body = ttk.Frame(self.root)
        self.body.pack(fill="both", expand=True)
        self._build_body()
        if self.config_path:
            self.refresh()
        else:
            self._set_status(self.t["select_config_hint"])

    # --- 위젯 구성 ---

    def _build_body(self) -> None:
        t = self.t
        top = ttk.Frame(self.body, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text=t["new_config"], command=self._new_config).pack(side="left")
        ttk.Button(top, text=t["choose_config"], command=self._choose_config).pack(side="left", padx=4)
        self.cfg_label = ttk.Label(top, text=self.config_path or "—")
        self.cfg_label.pack(side="left", padx=6)
        self.login_btn = ttk.Button(top, text=t["login"], command=self._login)
        self.login_btn.pack(side="right")
        self.logout_btn = ttk.Button(top, text=t["logout"], command=self._logout)
        self.logout_btn.pack(side="right", padx=4)

        dframe = ttk.Frame(self.body, padding=(6, 0))
        dframe.pack(fill="x")
        self.daemon_label = ttk.Label(dframe, text=t["daemon_unknown"])
        self.daemon_label.pack(side="left")
        self.daemon_start_btn = ttk.Button(dframe, text=t["daemon_start"], command=self._daemon_start)
        self.daemon_start_btn.pack(side="left", padx=4)
        self.daemon_stop_btn = ttk.Button(dframe, text=t["daemon_stop"], command=self._daemon_stop)
        self.daemon_stop_btn.pack(side="left")
        ttk.Button(dframe, text=t["devices"], command=self._devices).pack(side="right")
        ttk.Button(dframe, text=t["storage"], command=self._sources).pack(side="right", padx=4)

        pframe = ttk.Frame(self.body, padding=6)
        pframe.pack(fill="x")
        ttk.Button(pframe, text=t["up"], command=self._up).pack(side="left")
        self.path_var = tk.StringVar(value=self.vpath)
        entry = ttk.Entry(pframe, textvariable=self.path_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self._go())
        ttk.Button(pframe, text=t["go"], command=self._go).pack(side="left")
        ttk.Button(pframe, text=t["refresh"], command=self.refresh).pack(side="left", padx=4)

        cols = ("type", "name", "size", "owner", "backup")
        self.tree = ttk.Treeview(self.body, columns=cols, show="headings", selectmode="browse")
        for c, head, w in (
            ("type", t["col_type"], 60), ("name", t["col_name"], 320),
            ("size", t["col_size"], 100), ("owner", t["col_owner"], 90),
            ("backup", t["col_backup"], 110),
        ):
            self.tree.heading(c, text=head)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", self._on_double)

        tb = ttk.Frame(self.body, padding=6)
        tb.pack(fill="x")
        for text, cmd in (
            (t["upload"], self._upload), (t["download"], self._download),
            (t["mkdir"], self._mkdir), (t["delete"], self._delete),
            (t["move"], self._move), (t["copy"], self._copy),
        ):
            ttk.Button(tb, text=text, command=cmd).pack(side="left", padx=2)

        self.status = ttk.Label(self.body, text="", relief="sunken", anchor="w", padding=4)
        self.status.pack(fill="x", side="bottom")

        self._refresh_login_state()
        self._set_daemon_buttons(None)

    # --- 워커 브리지 ---

    def _tick(self) -> None:
        self.worker.poll()
        self.root.after(80, self._tick)

    def _submit(self, fn, on_ok=None, busy: str | None = None) -> None:
        if not self.config_path:
            messagebox.showwarning(self.t["app_title"], self.t["need_config"])
            return
        self._set_status(busy or self.t["busy_browse"])

        def done(ok, payload):
            if ok:
                self._set_status(self.t["ready"])
                if on_ok:
                    on_ok(payload)
            else:
                self._set_status(self.t["err_status"].format(msg=payload))
                messagebox.showerror(self.t["err"], str(payload))

        self.worker.submit(fn, done)

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    @staticmethod
    def _enable(btn, on: bool) -> None:
        btn.state(["!disabled"] if on else ["disabled"])

    def _refresh_login_state(self) -> None:
        """로그인 여부에 따라 로그인/로그아웃 버튼 활성 상태를 갱신한다."""
        if not self.config_path:
            self._enable(self.login_btn, False)
            self._enable(self.logout_btn, False)
            return
        try:
            logged = actions.is_logged_in(self.config_path)
        except Exception:  # noqa: BLE001
            self._enable(self.login_btn, False)
            self._enable(self.logout_btn, False)
            return
        self._enable(self.login_btn, not logged)
        self._enable(self.logout_btn, logged)

    def _set_daemon_buttons(self, running) -> None:
        """daemon 상태(running True/False, None=미상/설정없음)에 따라 시작/정지 버튼."""
        if running is None:
            self._enable(self.daemon_start_btn, False)
            self._enable(self.daemon_stop_btn, False)
            return
        self._enable(self.daemon_start_btn, not running)
        self._enable(self.daemon_stop_btn, running)

    # --- 설정 ---

    def _new_config(self) -> None:
        import socket

        t = self.t
        base = filedialog.askdirectory(title=t["nc_pick_dir"])
        if not base:
            return
        server_url = simpledialog.askstring(
            t["new_config"], t["nc_server"],
            initialvalue="https://stardustfs.noizze.net",
        )
        if server_url is None:
            return
        device_name = simpledialog.askstring(
            t["new_config"], t["nc_device"], initialvalue=socket.gethostname()
        )
        if not device_name:
            return
        generate_key = messagebox.askyesno(t["nc_key_title"], t["nc_key_q"])

        def make():
            return actions.create_config(
                base, server_url.strip() or None, device_name.strip(),
                generate_key=generate_key,
            )

        def done(ok, payload):
            if not ok:
                messagebox.showerror(t["err"], str(payload))
                return
            self.config_path = payload
            self.cfg_label.config(text=payload)
            self.vpath = "/"
            self.path_var.set("/")
            self._auto_started = False
            self.worker.submit(lambda: actions.invalidate(None), lambda *_a: None)
            self.refresh()
            self._refresh_daemon()
            self._refresh_login_state()
            messagebox.showinfo(
                t["new_config"],
                t["nc_done_new"] if generate_key else t["nc_done_restore"],
            )

        self._set_status(t["nc_busy"])
        self.worker.submit(make, done)

    def _choose_config(self) -> None:
        path = filedialog.askopenfilename(
            title=self.t["choose_config"],
            filetypes=[("JSON", "*.json"), ("*", "*.*")],
        )
        if path:
            self.config_path = path
            self.cfg_label.config(text=path)
            self.vpath = "/"
            self.path_var.set("/")
            self._auto_started = False
            self.worker.submit(lambda: actions.invalidate(None), lambda *_a: None)
            self.refresh()
            self._refresh_daemon()
            self._refresh_login_state()

    # --- 탐색 ---

    def refresh(self) -> None:
        self.vpath = self.path_var.get() or "/"
        if not self.config_path:
            self._populate([])
            self._set_status(self.t["select_config_hint"])
            return
        # 로그인하지 않았으면 파일 목록을 보여주지 않는다(코어 초기화도 하지 않음).
        if not self._logged_in():
            self._populate([])
            self._set_status(self.t["login_required"])
            return
        cfg = self.config_path
        vp = self.vpath
        self._submit(lambda: actions.browse(cfg, vp), self._show_browse,
                     self.t["busy_browse"])

    def _logged_in(self) -> bool:
        if not self.config_path:
            return False
        try:
            return actions.is_logged_in(self.config_path)
        except Exception:  # noqa: BLE001
            return False

    def _after_write(self) -> None:
        """쓰기 작업 후: 캐시 세션 무효화(용량 갱신) + 새로고침."""
        cfg = self.config_path
        if cfg:
            self.worker.submit(lambda: actions.invalidate(cfg), lambda *_a: None)
        self.refresh()

    def _backup_label(self, status: str) -> str:
        """로컬 replication_status를 표시 라벨로 변환한다."""
        if status == "replicated":
            return self.t["bk_done"]
        if status == "pending":
            return self.t["bk_pending"]
        return self.t["bk_none"]

    def _populate(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        for r in rows:
            size = _human(r["size"]) if r["type"] == "file" else ""
            backup = self._backup_label(r.get("backup", "")) if r["type"] == "file" else ""
            iid = self.tree.insert(
                "", "end",
                values=(r["type"], r["name"], size, r["owner"], backup),
            )
            self._rows[iid] = r

    def _show_browse(self, d: dict) -> None:
        self._populate(d["rows"])
        self._set_status(self.t["cap"].format(
            used=_human(d["used"]), total=_human(d["total"]),
            avail=_human(d["available"]), pending=d["pending"],
        ))
        # 온라인이면 실제 복제본 수를 백그라운드로 조회해 백업 컬럼에 병기한다.
        # (조용한 보강 — worker 콜백은 (ok, payload) 시그니처, 실패 시 상태만 유지)
        if self._logged_in():
            cfg, vp = self.config_path, self.vpath
            self.worker.submit(
                lambda: actions.replica_counts(cfg, vp),
                lambda ok, counts: self._apply_counts(vp, counts) if ok else None,
            )

    def _apply_counts(self, vp: str, counts: dict) -> None:
        """replica_counts 결과를 백업 컬럼에 '상태 (online/min)'로 병기한다."""
        if vp != self.vpath or not counts:
            return  # 폴더가 바뀌었거나 조회 결과 없음
        for iid, row in self._rows.items():
            if row["type"] != "file":
                continue
            info = counts.get(row["name"])
            if not info:
                continue
            label = self._backup_label(row.get("backup", ""))
            text = f"{label} ({info['online']}/{info['min']})"
            self.tree.set(iid, "backup", text)

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _join(self, name: str) -> str:
        return self.vpath.rstrip("/") + "/" + name

    def _on_double(self, _e) -> None:
        row = self._selected()
        if row and row["type"] == "dir":
            self.path_var.set(self._join(row["name"]))
            self.refresh()

    def _up(self) -> None:
        parent = self.vpath.rstrip("/").rsplit("/", 1)[0] or "/"
        self.path_var.set(parent)
        self.refresh()

    def _go(self) -> None:
        self.refresh()

    # --- 전송/쓰기 ---

    def _upload(self) -> None:
        local = filedialog.askopenfilename(title=self.t["upload_pick"])
        if not local:
            return
        remote = self._join(os.path.basename(local))
        cfg = self.config_path
        self._submit(
            lambda: actions.put_file(cfg, local, remote),
            lambda _n: self._after_write(),
            self.t["uploading"].format(name=os.path.basename(local)),
        )

    def _download(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            messagebox.showinfo(self.t["app_title"], self.t["download_pick"])
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
                self.t["download_done"].format(path=local, size=_human(n))
            ),
            self.t["downloading"].format(name=row["name"]),
        )

    def _mkdir(self) -> None:
        name = simpledialog.askstring(self.t["mkdir"], self.t["mkdir_prompt"])
        if not name:
            return
        cfg = self.config_path
        path = self._join(name)
        self._submit(lambda: actions.mkdir(cfg, path), lambda _r: self._after_write(),
                     self.t["mkdir_busy"])

    def _delete(self) -> None:
        row = self._selected()
        if not row:
            return
        if not messagebox.askyesno(
            self.t["delete"], self.t["delete_confirm"].format(name=row["name"])
        ):
            return
        cfg = self.config_path
        path = self._join(row["name"])
        recursive = row["type"] == "dir"
        self._submit(lambda: actions.remove(cfg, path, recursive),
                     lambda _r: self._after_write(), self.t["delete_busy"])

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
        self._submit(lambda: actions.move(cfg, src, dst), lambda _r: self._after_write(),
                     self.t["move_busy"])

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
        self._submit(lambda: actions.copy(cfg, src, dst), lambda _r: self._after_write(),
                     self.t["copy_busy"])

    # --- 스토리지 소스 ---

    def _sources(self) -> None:
        if not self.config_path:
            messagebox.showwarning(self.t["app_title"], self.t["need_config"])
            return
        t = self.t
        cfg = self.config_path
        win = tk.Toplevel(self.root)
        win.title(t["sources_title"])
        win.geometry("580x300")
        tree = ttk.Treeview(win, columns=("id", "type", "path", "size"),
                            show="headings")
        for c, w in (("id", 120), ("type", 80), ("path", 290), ("size", 80)):
            tree.heading(c, text=c)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True)

        def reload():
            tree.delete(*tree.get_children())
            for s in actions.list_sources(cfg):
                size = (_human(s["size"]) if s.get("type") == "loopback"
                        and s.get("size") else "")
                tree.insert("", "end", values=(
                    s.get("id"), s.get("type"), s.get("path"), size))

        def add_dir():
            d = filedialog.askdirectory(title=t["src_pick_dir"])
            if not d:
                return
            try:
                actions.add_source(cfg, "directory", d)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(t["err"], str(e))
                return
            reload()
            self.refresh()

        def add_loop():
            path = filedialog.asksaveasfilename(
                title=t["src_loop_path"], defaultextension=".img")
            if not path:
                return
            mb = simpledialog.askinteger("loopback", t["src_loop_size_prompt"],
                                         initialvalue=100, minvalue=10)
            if not mb:
                return
            try:
                actions.add_source(cfg, "loopback", path, size=mb * 1024 * 1024)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(t["err"], str(e))
                return
            reload()
            self.refresh()

        def remove():
            sel = tree.selection()
            if not sel:
                return
            sid = tree.item(sel[0], "values")[0]
            if not messagebox.askyesno(
                t["src_remove"], t["src_remove_confirm"].format(id=sid)
            ):
                return
            actions.remove_source(cfg, sid)
            reload()
            self.refresh()

        bar = ttk.Frame(win, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text=t["src_add_dir"], command=add_dir).pack(side="left")
        ttk.Button(bar, text=t["src_add_loop"], command=add_loop).pack(side="left", padx=4)
        ttk.Button(bar, text=t["src_remove"], command=remove).pack(side="left")
        ttk.Button(bar, text=t["close"], command=win.destroy).pack(side="right")
        reload()

    # --- 디바이스 ---

    def _devices(self) -> None:
        cfg = self.config_path
        self._submit(lambda: actions.devices_list(cfg), self._show_devices,
                     self.t["devices_busy"])

    def _show_devices(self, devs: list[dict]) -> None:
        t = self.t
        win = tk.Toplevel(self.root)
        win.title(t["devices_title"])
        win.geometry("440x260")
        tree = ttk.Treeview(win, columns=("id", "name", "online", "self"),
                            show="headings")
        for c, w in (("id", 90), ("name", 170), ("online", 80), ("self", 60)):
            tree.heading(c, text=c)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True)
        for d in devs:
            tree.insert("", "end", values=(
                d["id"], d["name"],
                t["online"] if d["online"] else t["offline"],
                t["this_device"] if d["self"] else "",
            ))

    # --- 로그인 ---

    def _login(self) -> None:
        if not self.config_path:
            messagebox.showwarning(self.t["app_title"], self.t["need_config"])
            return
        t = self.t
        email = simpledialog.askstring(t["login"], t["login_email"])
        if not email:
            return
        password = simpledialog.askstring(t["login"], t["login_password"], show="*")
        if password is None:
            return
        key_pw = simpledialog.askstring(t["login"], t["login_keypw"], show="*") or None
        cfg = self.config_path
        self._submit(
            lambda: actions.login(cfg, email, password, key_pw),
            lambda _r: self._after_login(t["login_ok"].format(email=email)),
            t["login_busy"],
        )

    def _after_login(self, msg: str) -> None:
        self._set_status(msg)
        self._refresh_login_state()
        self.refresh()  # 로그인 후 파일 목록 표시

    def _logout(self) -> None:
        cfg = self.config_path

        def done(_r):
            self._set_status(self.t["logout_ok"])
            self._refresh_login_state()
            self.refresh()  # 로그아웃 후 목록 숨김

        self._submit(lambda: actions.logout(cfg), done, self.t["logout_busy"])

    # --- daemon ---

    def _refresh_daemon(self) -> None:
        if self.config_path:
            cfg = self.config_path
            self.worker.submit(lambda: actions.daemon_status(cfg), self._on_daemon)
        self.root.after(5000, self._refresh_daemon)

    def _on_daemon(self, ok, payload) -> None:
        if not ok:
            self.daemon_label.config(text=self.t["daemon_unknown"])
            self._set_daemon_buttons(None)
            return
        if payload.get("running"):
            self.daemon_label.config(
                text=self.t["daemon_running"].format(pid=payload.get("pid"))
            )
            self._set_daemon_buttons(True)
        elif payload.get("stale"):
            self.daemon_label.config(text=self.t["daemon_stale"])
            self._set_daemon_buttons(False)
        else:
            self.daemon_label.config(text=self.t["daemon_stopped"])
            self._set_daemon_buttons(False)
            if self.config_path and not self._auto_started:
                self._auto_started = True
                self._set_status(self.t["daemon_starting"])
                self._daemon_start()

    def _daemon_start(self) -> None:
        cfg = self.config_path
        self._submit(
            lambda: actions.daemon_start(cfg),
            lambda pid: self._set_status(self.t["daemon_started"].format(pid=pid)),
            self.t["daemon_start_busy"],
        )

    def _daemon_stop(self) -> None:
        cfg = self.config_path
        self._submit(lambda: actions.daemon_stop(cfg),
                     lambda _r: self._set_status(self.t["daemon_stop_req"]),
                     self.t["daemon_stop_busy"])


def run_gui(config_path: str | None) -> None:
    """GUI를 실행한다 (블로킹)."""
    root = tk.Tk()
    StardustApp(root, config_path)
    root.mainloop()
