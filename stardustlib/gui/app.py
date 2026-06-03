"""StardustFS Tkinter GUI.

파일 탐색기(목록/업로드/다운로드/폴더/삭제/이동/복사) + 디바이스 + daemon 제어 +
로그인/로그아웃. 네트워크/파일 작업은 워커 스레드에서 수행하고 결과를 메인 스레드로
전달한다(actions/worker 참조).
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from stardustlib.gui import actions
from stardustlib.gui.worker import Worker


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
        self._auto_started = False  # 구동 시 daemon 1회 자동 시작용

        root.title("StardustFS")
        root.geometry("780x520")
        self._build_widgets()
        self.root.after(80, self._tick)
        self.root.after(200, self._refresh_daemon)
        if self.config_path:
            self.refresh()
        else:
            self._set_status("설정 파일을 선택하세요 (설정...).")

    # --- 위젯 구성 ---

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="새 설정...", command=self._new_config).pack(side="left")
        ttk.Button(top, text="설정...", command=self._choose_config).pack(side="left", padx=4)
        self.cfg_label = ttk.Label(top, text=self.config_path or "(미선택)")
        self.cfg_label.pack(side="left", padx=6)
        ttk.Button(top, text="로그인", command=self._login).pack(side="right")
        ttk.Button(top, text="로그아웃", command=self._logout).pack(side="right", padx=4)

        dframe = ttk.Frame(self.root, padding=(6, 0))
        dframe.pack(fill="x")
        self.daemon_label = ttk.Label(dframe, text="daemon: ?")
        self.daemon_label.pack(side="left")
        ttk.Button(dframe, text="daemon 시작", command=self._daemon_start).pack(side="left", padx=4)
        ttk.Button(dframe, text="daemon 정지", command=self._daemon_stop).pack(side="left")
        ttk.Button(dframe, text="디바이스", command=self._devices).pack(side="right")
        ttk.Button(dframe, text="스토리지", command=self._sources).pack(side="right", padx=4)

        pframe = ttk.Frame(self.root, padding=6)
        pframe.pack(fill="x")
        ttk.Button(pframe, text="↑ 상위", command=self._up).pack(side="left")
        self.path_var = tk.StringVar(value=self.vpath)
        entry = ttk.Entry(pframe, textvariable=self.path_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self._go())
        ttk.Button(pframe, text="이동", command=self._go).pack(side="left")
        ttk.Button(pframe, text="새로고침", command=self.refresh).pack(side="left", padx=4)

        cols = ("type", "name", "size", "owner")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", selectmode="browse")
        for c, w in (("type", 60), ("name", 380), ("size", 110), ("owner", 110)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", self._on_double)

        tb = ttk.Frame(self.root, padding=6)
        tb.pack(fill="x")
        for text, cmd in (
            ("업로드", self._upload), ("다운로드", self._download),
            ("새 폴더", self._mkdir), ("삭제", self._delete),
            ("이동/이름변경", self._move), ("복사", self._copy),
        ):
            ttk.Button(tb, text=text, command=cmd).pack(side="left", padx=2)

        self.status = ttk.Label(self.root, text="", relief="sunken", anchor="w", padding=4)
        self.status.pack(fill="x", side="bottom")

    # --- 워커 브리지 ---

    def _tick(self) -> None:
        self.worker.poll()
        self.root.after(80, self._tick)

    def _submit(self, fn, on_ok=None, busy: str = "처리 중...") -> None:
        if not self.config_path:
            messagebox.showwarning("StardustFS", "먼저 설정 파일을 선택하세요.")
            return
        self._set_status(busy)

        def done(ok, payload):
            if ok:
                self._set_status("준비됨")
                if on_ok:
                    on_ok(payload)
            else:
                self._set_status(f"오류: {payload}")
                messagebox.showerror("오류", str(payload))

        self.worker.submit(fn, done)

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    # --- 동작 ---

    def _new_config(self) -> None:
        import socket

        base = filedialog.askdirectory(
            title="설정/저장 폴더 선택 (비어 있는 폴더 권장)"
        )
        if not base:
            return
        server_url = simpledialog.askstring(
            "새 설정", "서버 URL (비우면 오프라인 전용):",
            initialvalue="https://stardustfs.noizze.net",
        )
        if server_url is None:
            return
        device_name = simpledialog.askstring(
            "새 설정", "디바이스 이름:", initialvalue=socket.gethostname()
        )
        if not device_name:
            return
        generate_key = messagebox.askyesno(
            "암호화 키",
            "이 디바이스에서 새 암호화 키를 생성할까요?\n\n"
            "예 = 첫 디바이스(새 키 생성)\n"
            "아니오 = 기존 계정(로그인 후 서버 백업에서 복원)",
        )

        def make():
            return actions.create_config(
                base, server_url.strip() or None, device_name.strip(),
                generate_key=generate_key,
            )

        def done(ok, payload):
            if not ok:
                messagebox.showerror("오류", str(payload))
                return
            self.config_path = payload
            self.cfg_label.config(text=payload)
            self.vpath = "/"
            self.path_var.set("/")
            self._auto_started = False  # 새 설정에 대해 자동 시작 재허용
            self.refresh()
            self._refresh_daemon()
            if generate_key:
                messagebox.showinfo(
                    "새 설정", "설정과 새 키를 생성했습니다. 로그인 후 사용하세요."
                )
            else:
                messagebox.showinfo(
                    "새 설정",
                    "설정을 생성했습니다. '로그인'에서 키 백업 암호까지 입력하면 "
                    "서버 백업에서 키가 복원됩니다.",
                )

        self._set_status("설정 생성 중...")
        self.worker.submit(make, done)

    def _choose_config(self) -> None:
        path = filedialog.askopenfilename(
            title="설정 파일 선택", filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")]
        )
        if path:
            self.config_path = path
            self.cfg_label.config(text=path)
            self.vpath = "/"
            self.path_var.set("/")
            self._auto_started = False
            self.refresh()
            self._refresh_daemon()

    def refresh(self) -> None:
        self.vpath = self.path_var.get() or "/"
        cfg = self.config_path
        vp = self.vpath
        # 목록 + 용량을 한 세션에서 조회(스토리지 초기화 1회).
        self._submit(lambda: actions.browse(cfg, vp), self._show_browse, "조회 중...")

    def _populate(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        for r in rows:
            size = _human(r["size"]) if r["type"] == "file" else ""
            iid = self.tree.insert(
                "", "end", values=(r["type"], r["name"], size, r["owner"])
            )
            self._rows[iid] = r

    def _show_browse(self, d: dict) -> None:
        self._populate(d["rows"])
        self._set_status(
            f"용량: 사용 {_human(d['used'])} / 총 {_human(d['total'])} "
            f"(가용 {_human(d['available'])}) · 보류 {d['pending']}"
        )

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
        cur = self.vpath.rstrip("/")
        parent = cur.rsplit("/", 1)[0] or "/"
        self.path_var.set(parent)
        self.refresh()

    def _go(self) -> None:
        self.refresh()

    def _upload(self) -> None:
        local = filedialog.askopenfilename(title="업로드할 파일")
        if not local:
            return
        remote = self._join(os.path.basename(local))
        cfg = self.config_path
        self._submit(
            lambda: actions.put_file(cfg, local, remote),
            lambda _n: self.refresh(), f"업로드 중: {os.path.basename(local)}",
        )

    def _download(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            messagebox.showinfo("StardustFS", "다운로드할 파일을 선택하세요.")
            return
        remote = self._join(row["name"])
        local = filedialog.asksaveasfilename(
            title="저장 위치", initialfile=row["name"]
        )
        if not local:
            return
        cfg = self.config_path
        self._submit(
            lambda: actions.get_file(cfg, remote, local),
            lambda n: self._set_status(f"다운로드 완료: {local} ({_human(n)})"),
            f"다운로드 중: {row['name']}",
        )

    def _mkdir(self) -> None:
        name = simpledialog.askstring("새 폴더", "폴더 이름:")
        if not name:
            return
        cfg = self.config_path
        path = self._join(name)
        self._submit(lambda: actions.mkdir(cfg, path), lambda _r: self.refresh(),
                     "폴더 생성 중...")

    def _delete(self) -> None:
        row = self._selected()
        if not row:
            return
        if not messagebox.askyesno("삭제", f"'{row['name']}'을(를) 삭제할까요?"):
            return
        cfg = self.config_path
        path = self._join(row["name"])
        recursive = row["type"] == "dir"
        self._submit(lambda: actions.remove(cfg, path, recursive),
                     lambda _r: self.refresh(), "삭제 중...")

    def _move(self) -> None:
        row = self._selected()
        if not row:
            return
        src = self._join(row["name"])
        dst = simpledialog.askstring("이동/이름변경", "대상 가상 경로:", initialvalue=src)
        if not dst or dst == src:
            return
        cfg = self.config_path
        self._submit(lambda: actions.move(cfg, src, dst), lambda _r: self.refresh(),
                     "이동 중...")

    def _copy(self) -> None:
        row = self._selected()
        if not row or row["type"] != "file":
            messagebox.showinfo("StardustFS", "복사할 파일을 선택하세요.")
            return
        src = self._join(row["name"])
        dst = simpledialog.askstring("복사", "대상 가상 경로:",
                                     initialvalue=self._join("copy-" + row["name"]))
        if not dst:
            return
        cfg = self.config_path
        self._submit(lambda: actions.copy(cfg, src, dst), lambda _r: self.refresh(),
                     "복사 중...")

    def _sources(self) -> None:
        if not self.config_path:
            messagebox.showwarning("StardustFS", "먼저 설정 파일을 선택하세요.")
            return
        cfg = self.config_path
        win = tk.Toplevel(self.root)
        win.title("스토리지 소스")
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
            d = filedialog.askdirectory(title="디렉토리 소스로 추가할 폴더")
            if not d:
                return
            try:
                actions.add_source(cfg, "directory", d)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("오류", str(e))
                return
            reload()
            self.refresh()

        def add_loop():
            path = filedialog.asksaveasfilename(
                title="루프백 이미지 경로", defaultextension=".img")
            if not path:
                return
            mb = simpledialog.askinteger("루프백", "크기(MB):",
                                         initialvalue=100, minvalue=10)
            if not mb:
                return
            try:
                actions.add_source(cfg, "loopback", path, size=mb * 1024 * 1024)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("오류", str(e))
                return
            reload()
            self.refresh()

        def remove():
            sel = tree.selection()
            if not sel:
                return
            sid = tree.item(sel[0], "values")[0]
            if not messagebox.askyesno(
                "제거",
                f"소스 '{sid}'를 설정에서 제거할까요?\n물리 데이터는 삭제되지 "
                "않으나 해당 소스의 파일은 접근 불가가 되며, 실행 중인 daemon은 "
                "재시작해야 반영됩니다.",
            ):
                return
            actions.remove_source(cfg, sid)
            reload()
            self.refresh()

        bar = ttk.Frame(win, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="디렉토리 추가", command=add_dir).pack(side="left")
        ttk.Button(bar, text="루프백 추가", command=add_loop).pack(side="left", padx=4)
        ttk.Button(bar, text="제거", command=remove).pack(side="left")
        ttk.Button(bar, text="닫기", command=win.destroy).pack(side="right")
        reload()

    def _devices(self) -> None:
        cfg = self.config_path
        self._submit(lambda: actions.devices_list(cfg), self._show_devices,
                     "디바이스 조회 중...")

    def _show_devices(self, devs: list[dict]) -> None:
        win = tk.Toplevel(self.root)
        win.title("내 디바이스")
        win.geometry("420x260")
        tree = ttk.Treeview(win, columns=("id", "name", "online", "self"),
                            show="headings")
        for c, w in (("id", 90), ("name", 160), ("online", 80), ("self", 60)):
            tree.heading(c, text=c)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True)
        for d in devs:
            tree.insert("", "end", values=(
                d["id"], d["name"], "online" if d["online"] else "offline",
                "this" if d["self"] else "",
            ))

    def _login(self) -> None:
        if not self.config_path:
            messagebox.showwarning("StardustFS", "먼저 설정 파일을 선택하세요.")
            return
        email = simpledialog.askstring("로그인", "이메일:")
        if not email:
            return
        password = simpledialog.askstring("로그인", "비밀번호:", show="*")
        if password is None:
            return
        key_pw = simpledialog.askstring(
            "로그인", "마스터키 백업 암호(선택, 없으면 비움):", show="*"
        ) or None
        cfg = self.config_path
        self._submit(
            lambda: actions.login(cfg, email, password, key_pw),
            lambda _r: self._set_status(f"로그인 성공: {email}"), "로그인 중...",
        )

    def _logout(self) -> None:
        cfg = self.config_path
        self._submit(lambda: actions.logout(cfg),
                     lambda _r: self._set_status("로그아웃 완료"), "로그아웃 중...")

    # --- daemon ---

    def _refresh_daemon(self) -> None:
        if self.config_path:
            cfg = self.config_path
            self.worker.submit(lambda: actions.daemon_status(cfg), self._on_daemon)
        self.root.after(5000, self._refresh_daemon)

    def _on_daemon(self, ok, payload) -> None:
        if not ok:
            self.daemon_label.config(text="daemon: ?")
            return
        if payload.get("running"):
            self.daemon_label.config(
                text=f"daemon: 실행 중 (pid={payload.get('pid')})"
            )
        elif payload.get("stale"):
            self.daemon_label.config(text="daemon: stale(비정상 종료?)")
        else:
            self.daemon_label.config(text="daemon: 미실행")
            # 구동 시 미실행이면 1회 자동 시작 (이후/수동 정지 시 재시작 안 함)
            if self.config_path and not self._auto_started:
                self._auto_started = True
                self._set_status("daemon 자동 시작 중...")
                self._daemon_start()

    def _daemon_start(self) -> None:
        cfg = self.config_path
        self._submit(lambda: actions.daemon_start(cfg),
                     lambda pid: self._set_status(f"daemon 시작(pid={pid})"),
                     "daemon 시작 중...")

    def _daemon_stop(self) -> None:
        cfg = self.config_path
        self._submit(lambda: actions.daemon_stop(cfg),
                     lambda _r: self._set_status("daemon 정지 요청"), "daemon 정지 중...")


def run_gui(config_path: str | None) -> None:
    """GUI를 실행한다 (블로킹)."""
    root = tk.Tk()
    StardustApp(root, config_path)
    root.mainloop()
