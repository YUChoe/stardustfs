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
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from stardustlib.gui import actions, i18n, theme, tray
from stardustlib.gui.worker import Worker

try:  # 현대적 플랫 테마(Windows 11 풍). 미설치 시 기본 ttk로 폴백.
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None

logger = logging.getLogger(__name__)

# daemon 재시작 쿨다운(초): 한 번 시작하면 heartbeat 안정화까지 추가 재시작 보류.
# 상태 폴링 주기(5s)보다 충분히 커서 부팅 중 중복 시작을 막는다.
_DAEMON_RESTART_COOLDOWN = 20.0


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
        # daemon은 항상 온라인으로 감독(supervise)한다. 다음 재시작 허용 시각(쿨다운).
        self._daemon_restart_until = 0.0
        self._last_meta_mtime = 0.0
        self.lang = i18n.detect_lang()
        self.t = i18n.get_text(self.lang)
        # 디자인 시스템 기본은 Primer 다크다.
        self.theme = "dark"
        self._icon_photo = None  # 브랜드 마크 아이콘 참조(GC 방지)

        root.title(self.t["app_title"])
        root.geometry("900x620")
        root.minsize(760, 480)
        self._icon_photo = theme.set_window_icon(root)
        self._apply_theme()
        self._build_menu()
        self.body = ttk.Frame(root)
        self.body.pack(fill="both", expand=True)
        self._build_body()
        self._setup_tray()

        self.root.after(80, self._tick)
        self.root.after(150, self._apply_titlebar)  # 매핑 후 제목표시줄 색 재적용
        self.root.after(200, self._refresh_daemon)
        self.root.after(3000, self._poll_meta)
        if self.config_path:
            self.refresh()
        else:
            self._set_status(self.t["select_config_hint"])

    # --- 메뉴 / 트레이 ---

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label=self.t["new_config"], command=self._new_config)
        file_menu.add_command(label=self.t["choose_config"], command=self._choose_config)
        menubar.add_cascade(label=self.t["menu_file"], menu=file_menu)
        lang_menu = tk.Menu(menubar, tearoff=0)
        lang_menu.add_command(label=self.t["lang_ko"],
                              command=lambda: self._set_language("ko"))
        lang_menu.add_command(label=self.t["lang_en"],
                              command=lambda: self._set_language("en"))
        menubar.add_cascade(label=self.t["menu_language"], menu=lang_menu)

        theme_menu = tk.Menu(menubar, tearoff=0)
        theme_menu.add_command(label=self.t["theme_light"],
                               command=lambda: self._set_theme("light"))
        theme_menu.add_command(label=self.t["theme_dark"],
                               command=lambda: self._set_theme("dark"))
        menubar.add_cascade(label=self.t["menu_theme"], menu=theme_menu)

        # 관리: 스토리지/디바이스. daemon은 수동 토글이 아니라 항상 온라인으로
        # 감독되므로 시작/정지 메뉴를 두지 않는다(상태는 하단 점으로 표시).
        manage = tk.Menu(menubar, tearoff=0)
        manage.add_command(label=self.t["storage"], command=self._sources)
        manage.add_command(label=self.t["devices"], command=self._devices)
        menubar.add_cascade(label=self.t["menu_manage"], menu=manage)
        self.manage_menu = manage
        self.root.config(menu=menubar)

    def _update_title(self) -> None:
        """창 제목에 앱 이름 + 설정 파일명을 표시한다."""
        if self.config_path:
            name = os.path.basename(self.config_path)
            self.root.title(f"{self.t['app_title']} — {name}")
        else:
            self.root.title(self.t["app_title"])

    def _apply_theme(self) -> None:
        """현대적 테마(sv-ttk) + 맑은 고딕 폰트를 적용한다."""
        if sv_ttk is not None:
            try:
                sv_ttk.set_theme(self.theme)
            except Exception:  # noqa: BLE001
                pass
        self._apply_font()
        # 디자인 시스템(Primer) 팔레트를 sv_ttk 위에 재지정한다.
        try:
            theme.apply_palette(self.root, ttk.Style(), dark=self.theme == "dark")
        except Exception:  # noqa: BLE001
            pass
        self._apply_titlebar()

    def _apply_font(self) -> None:
        """한글 폰트를 맑은 고딕으로 통일하고 리스트 행 높이를 넉넉히 한다."""
        try:
            import tkinter.font as tkfont

            for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                          "TkHeadingFont", "TkFixedFont"):
                try:
                    tkfont.nametofont(fname).configure(family="Malgun Gothic")
                except Exception:  # noqa: BLE001
                    pass
            fnt = ("Malgun Gothic", 10)
            style = ttk.Style()
            style.configure(".", font=fnt)
            # sv-ttk가 위젯 스타일별로 자체 폰트를 지정하므로 각 스타일에 직접 적용.
            for st in ("TLabel", "TButton", "Accent.TButton", "TEntry",
                       "TMenubutton", "TCheckbutton", "TRadiobutton",
                       "Toolbutton", "TLabelframe.Label", "TNotebook.Tab",
                       "Treeview", "Treeview.Heading"):
                try:
                    style.configure(st, font=fnt)
                except Exception:  # noqa: BLE001
                    pass
            style.configure("Treeview", rowheight=28)
        except Exception:  # noqa: BLE001
            pass

    def _apply_titlebar(self) -> None:
        """Windows 11에서 제목 표시줄 색을 테마에 맞춘다(기본 강조색/주황 제거)."""
        if os.name != "nt":
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dark = self.theme == "dark"
            flag = ctypes.c_int(1 if dark else 0)
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(flag), ctypes.sizeof(flag)
            )
            # DWMWA_CAPTION_COLOR = 35 (Win11 22000+), COLORREF 0x00BBGGRR.
            # 다크는 Primer 캔버스(#0d1117 → BGR 0x171B0D).
            color = ctypes.c_int(0x00171B0D if dark else 0x00FAFAFA)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 35, ctypes.byref(color), ctypes.sizeof(color)
            )
        except Exception:  # noqa: BLE001 — 구버전 Windows 등은 무시
            pass

    def _set_theme(self, name: str) -> None:
        self.theme = name
        self._apply_theme()

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
        # 경로/계정 바 (설정 진입은 '파일' 메뉴로 이동, 설정 파일명은 창 제목에 표시)
        pframe = ttk.Frame(self.body, padding=(10, 8))
        pframe.pack(fill="x")
        self.login_btn = ttk.Button(pframe, text=t["login"], command=self._login)
        self.login_btn.pack(side="right")
        self.logout_btn = ttk.Button(pframe, text=t["logout"], command=self._logout)
        self.logout_btn.pack(side="right", padx=(0, 6))
        ttk.Button(pframe, text=t["up"], command=self._up).pack(side="left")
        self.path_var = tk.StringVar(value=self.vpath)
        entry = ttk.Entry(pframe, textvariable=self.path_var)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda _e: self._go())
        ttk.Button(pframe, text=t["go"], command=self._go).pack(side="left")
        ttk.Button(pframe, text=t["refresh"], command=self.refresh).pack(side="left", padx=(6, 10))
        self._update_title()

        # 파일 목록: 아이콘을 이름 앞에 붙여 Windows 11 탐색기 같은 경험(소유자 컬럼 제거)
        cols = ("name", "size", "backup")
        self.tree = ttk.Treeview(self.body, columns=cols, show="headings", selectmode="extended")
        for c, head, w, anchor in (
            ("name", t["col_name"], 440, "w"),
            ("size", t["col_size"], 110, "e"),
            ("backup", t["col_backup"], 140, "w"),
        ):
            self.tree.heading(c, text=head)
            self.tree.column(c, width=w, anchor=anchor)
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", self._on_double)

        # 하단 상태바(VSCode 풍): ● daemon · 스토리지 · 디바이스 · (전송 상태) · 백업
        statusbar = ttk.Frame(self.body, padding=(10, 4))
        statusbar.pack(fill="x", side="bottom")
        self.daemon_label = ttk.Label(statusbar, text=t["daemon_unknown"])
        self.daemon_label.pack(side="left")
        ttk.Separator(statusbar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.storage_label = ttk.Label(statusbar, text="")
        self.storage_label.pack(side="left")
        ttk.Separator(statusbar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.device_label = ttk.Label(statusbar, text="")
        self.device_label.pack(side="left")
        self.backup_status = ttk.Label(statusbar, text="", anchor="e")
        self.backup_status.pack(side="right")
        self.status = ttk.Label(statusbar, text="", anchor="w")
        self.status.pack(side="right", padx=10)
        ttk.Separator(self.body, orient="horizontal").pack(fill="x", side="bottom")

        # 액션 툴바: 전송 | 파일작업 | 백업 그룹(구분선), 주요 동작은 Accent 강조
        tb = ttk.Frame(self.body, padding=(8, 6))
        tb.pack(fill="x", side="bottom")

        def _btn(text, cmd, accent=False):
            style = "Accent.TButton" if (accent and sv_ttk is not None) else "TButton"
            return ttk.Button(tb, text=text, command=cmd, style=style)

        _btn(t["upload"], self._upload, accent=True).pack(side="left")
        _btn(t["download"], self._download).pack(side="left", padx=(6, 0))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=10)
        for text, cmd in ((t["mkdir"], self._mkdir), (t["delete"], self._delete),
                          (t["move"], self._move), (t["copy"], self._copy)):
            _btn(text, cmd).pack(side="left", padx=(0, 6))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=10)
        _btn(t["backup_now"], self._backup_selected, accent=True).pack(side="left")
        _btn(t["heal_now"], self._heal_selected).pack(side="left", padx=(6, 0))

        self._refresh_login_state()

    # --- 워커 브리지 ---

    def _tick(self) -> None:
        self.worker.poll()
        self.root.after(80, self._tick)

    # --- 메타데이터 변경 감지 → 자동 새로고침 ---

    def _mark_meta_seen(self) -> None:
        """현재 메타데이터 mtime을 '본 것'으로 기록한다(자동 새로고침 기준점)."""
        if self.config_path:
            try:
                self._last_meta_mtime = actions.metadata_mtime(self.config_path)
            except Exception:  # noqa: BLE001
                pass

    def _poll_meta(self) -> None:
        """daemon이 메타데이터를 갱신(동기화 등)하면 목록을 자동 새로고침한다.

        목록만 가볍게 갱신(counts=False)해 삭제/추가가 수동 새로고침 없이 반영된다.
        백업 수(온라인 조회)는 수동 새로고침에서만 갱신한다.
        """
        try:
            if self.config_path and self._logged_in():
                m = actions.metadata_mtime(self.config_path)
                if m > self._last_meta_mtime:
                    self._last_meta_mtime = m
                    self.refresh(counts=False)
        except Exception:  # noqa: BLE001 — 폴링 실패는 무시
            pass
        self.root.after(3000, self._poll_meta)

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

    @staticmethod
    def make_modal(win: tk.Toplevel, parent: tk.Misc) -> None:
        """창을 모달로 만든다 — 닫기 전까지 parent(메인 창) 입력을 차단한다.

        grab_set은 창이 아직 보이지 않으면 TclError(window not viewable)를 낼 수
        있으므로, 보일 때까지 지연 재시도한다. 창이 닫히면 grab은 자동 해제된다.
        """
        win.transient(parent)

        def _grab() -> None:
            try:
                win.grab_set()
            except tk.TclError:
                win.after(50, _grab)

        win.after(0, _grab)

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
            self._update_title()
            self.vpath = "/"
            self.path_var.set("/")
            self._daemon_restart_until = 0.0  # 새 설정 → 즉시 감독 시작
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
            self._update_title()
            self.vpath = "/"
            self.path_var.set("/")
            self._daemon_restart_until = 0.0  # 새 설정 → 즉시 감독 시작
            self.worker.submit(lambda: actions.invalidate(None), lambda *_a: None)
            self.refresh()
            self._refresh_daemon()
            self._refresh_login_state()

    # --- 탐색 ---

    def refresh(self, counts: bool = True) -> None:
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
        self._submit(lambda: actions.browse(cfg, vp),
                     lambda d: self._show_browse(d, counts),
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
            is_file = r["type"] == "file"
            size = _human(r["size"]) if is_file else ""
            backup = self._backup_label(r.get("backup", "")) if is_file else ""
            icon = "📄" if is_file else "📁"
            iid = self.tree.insert(
                "", "end",
                values=(f"{icon}  {r['name']}", size, backup),
            )
            self._rows[iid] = r

    def _show_browse(self, d: dict, counts: bool = True) -> None:
        self._populate(d["rows"])
        # 스토리지 상태: 소스 수 + 사용/총 용량
        self.storage_label.config(text=self.t["storage_status"].format(
            sources=d.get("sources", 0), used=_human(d["used"]),
            total=_human(d["total"]),
        ))
        bs = d.get("backup_summary")
        if bs:
            self.backup_status.config(text=self.t["backup_summary"].format(
                replicated=bs["replicated"], pending=bs["pending"],
                none=bs["none"],
            ))
        # 방금 본 메타데이터 시점을 기록(자동 새로고침이 즉시 재발동하지 않도록).
        self._mark_meta_seen()
        if not counts:
            return
        # 디바이스 상태(온라인/전체)는 로그인 시 백그라운드 조회로 갱신.
        if self._logged_in():
            cfg = self.config_path
            self.worker.submit(
                lambda: actions.devices_summary(cfg),
                lambda ok, s: self._show_device_summary(s) if ok else None,
            )
        # 로컬 상태가 pending/replicated인 파일만 실제 복제본 수를 백그라운드 조회해
        # 병기한다. none(미백업) 뿐이면 서버 조회를 생략한다(불필요한 초기화/호출 방지).
        # (조용한 보강 — worker 콜백은 (ok, payload) 시그니처, 실패 시 상태만 유지)
        names = [
            r["name"] for r in d["rows"]
            if r["type"] == "file" and r.get("backup") in ("pending", "replicated")
        ]
        if names and self._logged_in():
            cfg, vp = self.config_path, self.vpath
            self.worker.submit(
                lambda: actions.replica_counts(cfg, vp, names),
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

    def _show_device_summary(self, s: dict) -> None:
        """디바이스 온라인/전체 요약을 상태바에 표시한다."""
        if not s:
            return
        self.device_label.config(text=self.t["device_status"].format(
            online=s.get("online", 0), total=s.get("total", 0),
        ))

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _selected_rows(self) -> list[dict]:
        """다중 선택된 행 목록(없으면 빈 리스트)."""
        return [self._rows[i] for i in self.tree.selection() if i in self._rows]

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
        if not self.config_path:
            messagebox.showwarning(self.t["app_title"], self.t["need_config"])
            return
        from stardustlib.gui.upload_dialog import UploadDialog

        UploadDialog(self)

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

    def _backup_selected(self) -> None:
        files = [r for r in self._selected_rows() if r["type"] == "file"]
        if not files:
            messagebox.showinfo(self.t["app_title"], self.t["backup_pick"])
            return
        cfg = self.config_path
        paths = [self._join(r["name"]) for r in files]
        self._submit(
            lambda: actions.backup_paths(cfg, paths),
            self._show_backup_result, self.t["backup_busy"],
        )

    def _heal_selected(self) -> None:
        files = [r for r in self._selected_rows() if r["type"] == "file"]
        if not files:
            messagebox.showinfo(self.t["app_title"], self.t["backup_pick"])
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
        self._set_status(self.t["backup_done"].format(ok=ok, pending=pending))
        self.refresh()  # 상태 컬럼·요약 갱신

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
        if self._focus_existing("_sources_win"):
            return
        t = self.t
        cfg = self.config_path
        win = tk.Toplevel(self.root)
        self._sources_win = win
        self.make_modal(win, self.root)
        win.title(t["sources_title"])
        win.geometry("640x360")
        win.minsize(520, 280)
        # 버튼 바를 먼저 하단에 고정(트리 expand에 가려지지 않도록)
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x", side="bottom")
        cols = ("scope", "name", "status", "cap", "path")
        heads = (t["col_scope"], t["col_src_name"], t["col_status"],
                 t["col_capacity"], t["col_path"])
        widths = (70, 150, 80, 130, 240)
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True)
        row_meta: dict[str, dict] = {}  # iid → {scope, id|device_id}

        def _cap(d: dict) -> str:
            total = d.get("total")
            if total:
                avail = d.get("available")
                used = total - avail if avail is not None else None
                if used is None:
                    return _human(total)
                return f"{_human(used)} / {_human(total)}"
            used = d.get("used")  # 리모트: 사용 바이트만 알 수 있음
            if used is not None:
                return _human(used)
            return "-"

        def reload():
            def done(ok, ov):
                tree.delete(*tree.get_children())
                row_meta.clear()
                remove_btn.config(state="disabled")  # 선택 해제 → 제거 불가
                if not ok:
                    return
                for s in ov.get("local", []):
                    iid = tree.insert("", "end", values=(
                        t["scope_local"], s.get("id"), "-",
                        _cap(s), s.get("path", "")))
                    row_meta[iid] = {"scope": "local", "id": s.get("id")}
                for d in ov.get("remote", []):
                    status = (t["status_online"] if d.get("online")
                              else t["status_offline"])
                    iid = tree.insert("", "end", values=(
                        t["scope_remote"], d.get("name"), status,
                        _cap(d), d.get("device", "")))
                    row_meta[iid] = {
                        "scope": "remote", "device_id": d.get("device_id")}

            self.worker.submit(lambda: actions.storage_overview(cfg), done)

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
            self._after_write()  # 캐시 세션 무효화 → 새 소스 마운트·상태바 반영
            reload()             # 재빌드된 세션 기준으로 목록 갱신
            self._reload_daemon()  # daemon이 새 소스를 remount·재신고하도록 리로드

        def remove():
            sel = tree.selection()
            if not sel:
                return
            meta = row_meta.get(sel[0], {})
            if meta.get("scope") != "local":
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
                    reload()
                    self.refresh()
                    # daemon이 분리된 소스를 더는 mount·재신고하지 않도록 리로드
                    self._reload_daemon()
                else:
                    messagebox.showwarning(t["src_remove"], t["src_detach_blocked"].format(
                        unmoved=len(report.get("unmoved", []))))
                    reload()
                    self.refresh()

            self._set_status(t["src_detach_busy"])
            self.worker.submit(lambda: actions.detach_source(cfg, sid), done)

        ttk.Button(bar, text=t["src_add_loop"], command=add_loop).pack(side="left")
        remove_btn = ttk.Button(bar, text=t["src_remove"], command=remove,
                                state="disabled")
        remove_btn.pack(side="left", padx=4)
        ttk.Button(bar, text=t["close"], command=win.destroy).pack(side="right")

        def _on_sel(_e=None):
            # 로컬 소스를 선택했을 때만 제거(detach) 가능. 리모트는 불가.
            sel = tree.selection()
            local = bool(sel) and row_meta.get(sel[0], {}).get("scope") == "local"
            remove_btn.config(state="normal" if local else "disabled")

        tree.bind("<<TreeviewSelect>>", _on_sel)
        reload()

    # --- 디바이스 ---

    def _focus_existing(self, attr: str) -> bool:
        """단일 인스턴스 창: 이미 열려 있으면 앞으로 가져오고 True를 반환한다."""
        win = getattr(self, attr, None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return True
        return False

    def _devices(self) -> None:
        if self._focus_existing("_devices_win"):
            return
        cfg = self.config_path
        self._submit(lambda: actions.devices_list(cfg), self._show_devices,
                     self.t["devices_busy"])

    def _show_devices(self, devs: list[dict]) -> None:
        t = self.t
        win = tk.Toplevel(self.root)
        self._devices_win = win
        self.make_modal(win, self.root)
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
            # 디바이스 온라인 카운트를 주기적으로 갱신(유휴 중 staleness 방지).
            # 경량 GET /devices라 폴링 부담이 작다.
            if self._logged_in():
                self.worker.submit(
                    lambda: actions.devices_summary(cfg),
                    lambda ok, s: self._show_device_summary(s) if ok else None,
                )
        self.root.after(5000, self._refresh_daemon)

    def _daemon_dot(self, text: str, color: str) -> None:
        self.daemon_label.config(text="● " + text, foreground=color)

    def _on_daemon(self, ok, payload) -> None:
        grey, green, orange = "#9aa0a6", "#2da44e", "#d29922"
        if not ok:
            self._daemon_dot(self.t["daemon_unknown"], grey)
            return
        if payload.get("running"):
            self._daemon_dot(
                self.t["daemon_running"].format(pid=payload.get("pid")), green
            )
            return
        # running이 아니면(정지 또는 stale=중단/행) 항상 온라인 유지를 위해 재시작.
        if payload.get("stale"):
            self._daemon_dot(self.t["daemon_stale"], orange)
        else:
            self._daemon_dot(self.t["daemon_stopped"], grey)
        self._ensure_daemon()

    def _reload_daemon(self) -> None:
        """실행 중인 daemon에 config 리로드 신호를 보낸다(무중단 remount).

        소스 추가/분리 후 호출한다 — daemon은 시작 시 config로 소스를 mount하므로,
        리로드해야 변경된 로컬 소스를 다시 읽어 remount하고 서버 레지스트리에 즉시
        재신고한다. 전체 재시작과 달리 P2P/동기화를 중단하지 않는다.
        """
        cfg = self.config_path
        if not cfg:
            return
        self._set_status(self.t["daemon_restart"])
        self.worker.submit(
            lambda: actions.daemon_signal_reload(cfg), lambda *_a: None
        )

    def _ensure_daemon(self) -> None:
        """daemon이 떠 있지 않으면 재시작한다(쿨다운으로 재시작 폭주 방지).

        시작 후 heartbeat가 기록되기까지 시간이 필요하므로, 한 번 시작하면
        쿨다운 동안에는 추가 재시작을 시도하지 않는다.
        """
        if not self.config_path:
            return
        now = time.time()
        if now < self._daemon_restart_until:
            return  # 직전 시작이 진행/안정화 중
        self._daemon_restart_until = now + _DAEMON_RESTART_COOLDOWN
        self._set_status(self.t["daemon_starting"])
        self._daemon_start()

    def _daemon_start(self) -> None:
        cfg = self.config_path
        # 시작 후 pid를 상태바에 쓰지 않는다(daemon 상태는 좌측 점이 폴링으로 표시).
        # 시작 직후 1회 상태 점검으로 점을 갱신한다(별도 폴링 루프 추가 없이).
        self._submit(
            lambda: actions.daemon_start(cfg),
            lambda _pid: self.worker.submit(
                lambda: actions.daemon_status(cfg), self._on_daemon
            ),
            self.t["daemon_start_busy"],
        )


def _hide_console_if_frozen() -> None:
    """프로즌(PyInstaller) Windows exe로 GUI를 띄울 때 콘솔 창을 숨긴다.

    콘솔 빌드라 CLI 출력은 유지하되, GUI 더블클릭 시 함께 뜨는 cmd 창만 감춘다.
    소스 실행(개발)에서는 사용자의 터미널을 숨기지 않도록 frozen일 때만 동작.
    """
    import sys

    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:  # noqa: BLE001 — 콘솔 숨김 실패는 무시
        pass


def run_gui(config_path: str | None) -> None:
    """GUI를 실행한다 (블로킹)."""
    _hide_console_if_frozen()
    root = tk.Tk()
    StardustApp(root, config_path)
    root.mainloop()
