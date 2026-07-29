"""StardustFS Tkinter GUI — 메인 윈도우 셸.

파일 탐색기 + 디바이스·스토리지 관리 + daemon 제어 + 로그인을 한 창에 모은다.
이 모듈은 창·테마·메뉴·트레이와 워커 브리지만 담고, 화면 영역별 동작은 믹스인으로
나눠 두었다:

- panel_files / file_ops: 파일 목록 위젯과 파일 동작
- panel_mgmt: 하단 스토리지·디바이스 패널
- statusbar: 하단 상태바, 진행 표시, 메타데이터 폴링
- daemon_control: daemon 감독
- session: 설정 진입과 로그인

i18n(ko/en), 시스템 트레이 최소화(선택 의존 pystray) 지원. 창 닫기(X)는 트레이로
숨기고, 트레이 '종료'로만 실제 종료한다.

네트워크/파일 작업은 워커 스레드에서 수행하고 결과를 메인 스레드로 전달한다.
"""

from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from stardustlib.gui import i18n, prefs, theme, tray
from stardustlib.gui.daemon_control import DaemonControlMixin
from stardustlib.gui.file_ops import FileOpsMixin
from stardustlib.gui.panel_files import FilesPanelMixin
from stardustlib.gui.panel_mgmt import MgmtPanelMixin
from stardustlib.gui.session import SessionMixin
from stardustlib.gui.statusbar import StatusBarMixin
from stardustlib.gui.window_theme import WindowThemeMixin
from stardustlib.gui.worker import Worker

logger = logging.getLogger(__name__)

# 창 기본 크기·최소 크기(px).
_WIN_SIZE = (900, 620)
_WIN_MIN = (760, 480)

# 세로 분할에서 파일 목록이 차지하는 초기 비율(나머지는 스토리지·디바이스 패널).
_FILE_PANE_RATIO = 0.68


class StardustApp(
    FilesPanelMixin,
    FileOpsMixin,
    MgmtPanelMixin,
    StatusBarMixin,
    DaemonControlMixin,
    SessionMixin,
    WindowThemeMixin,
):
    """메인 윈도우."""

    def __init__(
        self, root: tk.Tk, config_path: str | None, instance_lock=None
    ) -> None:
        self.root = root
        self.config_path = config_path
        # 단일 인스턴스 락(선택). 메인루프에서 heartbeat를 갱신하고, 두 번째 실행이
        # 남긴 포커스 요청을 소비해 창을 앞으로 올린다.
        self._instance_lock = instance_lock
        self.vpath = "/"
        self.worker = Worker()
        self._rows: dict[str, dict] = {}
        # daemon은 항상 온라인으로 감독(supervise)한다. 다음 재시작 허용 시각(쿨다운).
        self._daemon_restart_until = 0.0
        # 직전 폴링에서 본 daemon 생존 여부. 정지→실행 전이를 감지해 조회 세션을
        # 다시 연다(daemon이 FAT 이미지를 만들기 전에 열린 세션은 소스가 비활성이다).
        self._daemon_was_running: bool | None = None
        # 상태바에 복제 진행을 표시 중인지(끝나면 기본 문구로 되돌리기 위함).
        self._showing_progress = False
        self._last_meta_mtime = 0.0
        # 표시 설정은 사용자 단위로 저장한다. 없으면 언어는 로케일 추정,
        # 테마는 디자인 시스템 기본(Primer 다크).
        self.lang = prefs.lang(i18n.detect_lang())
        self.t = i18n.get_text(self.lang)
        self.theme = prefs.theme("dark")
        self._icon_photo = None  # 브랜드 마크 아이콘 참조(GC 방지)

        root.title(self.t["app_title"])
        # 주 모니터 중앙에 배치(위치 미지정 시 터미널/커서 기준으로 화면 밖·보조
        # 모니터에 열려 "안 보임"으로 오인되는 것을 막는다).
        _w, _h = _WIN_SIZE
        _sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
        _x, _y = max(0, (_sw - _w) // 2), max(0, (_sh - _h) // 2)
        root.geometry(f"{_w}x{_h}+{_x}+{_y}")
        root.minsize(*_WIN_MIN)
        self._icon_photo = theme.set_window_icon(root)
        self._apply_theme()
        self._build_menu()
        self.body = ttk.Frame(root)
        self.body.pack(fill="both", expand=True)
        self._build_body()
        self._setup_tray()

        self.root.after(80, self._tick)
        self.root.after(150, self._apply_titlebar)  # 매핑 후 제목표시줄 색 재적용
        # 터미널에서 실행 시 창이 터미널 뒤(비-포그라운드)에 열려 "안 보임"으로
        # 오인되는 것을 막는다. 매핑 후 한 번 앞으로 끌어올린다(항상 위는 아님).
        self.root.after(180, self._bring_to_front)
        self.root.after(200, self._refresh_daemon)
        self.root.after(250, self._mgmt_poll)  # 하단 스토리지·디바이스 패널 갱신 루프
        self.root.after(3000, self._poll_meta)
        if self._instance_lock is not None:
            self.root.after(300, self._instance_poll)
        if self.config_path:
            self.refresh()
        else:
            self._set_status(self.t["select_config_hint"])

    # --- 창 표시 / 단일 인스턴스 ---

    def _instance_poll(self) -> None:
        """단일 인스턴스 heartbeat 갱신 + 두 번째 실행의 포커스 요청 처리.

        갱신이 멈추면(창이 얼어붙으면) 락이 stale이 되어 새 인스턴스가 뜬다 —
        응답하지 않는 창을 붙들고 사용자를 막지 않기 위한 의도된 동작이다.
        """
        from stardustlib.single_instance import (
            BEAT_INTERVAL_SECONDS,
            consume_focus_request,
        )

        lock = self._instance_lock
        if lock is None:
            return
        lock.beat()
        if consume_focus_request(lock.path):
            logger.info("두 번째 실행 요청 — 기존 창을 앞으로 올립니다")
            self._show_window()
        self.root.after(int(BEAT_INTERVAL_SECONDS * 1000), self._instance_poll)

    def _show_window(self) -> None:
        """트레이로 숨었거나 최소화된 창을 다시 보여주고 앞으로 올린다."""
        try:
            self.root.deiconify()
        except Exception:  # noqa: BLE001 — 이미 표시 중이면 무시
            pass
        self._bring_to_front()

    def _bring_to_front(self) -> None:
        """시작 시 창을 화면 앞으로 올려 포커스한다(일시적 topmost 후 해제)."""
        try:
            self.root.deiconify()
            self.root.update_idletasks()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
            # 진단: 창이 안 보인다는 신고 시 위치/화면을 로그로 확인한다.
            logger.info(
                "GUI 창 표시: geometry=%s screen=%dx%d state=%s",
                self.root.winfo_geometry(),
                self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
                self.root.state(),
            )
        except Exception as e:  # noqa: BLE001 — 플랫폼별 실패는 무시
            logger.warning("창 포그라운드 실패: %s", e)

    # --- 메뉴 / 트레이 ---

    def _build_menu(self) -> None:
        """ttk Menubutton 기반 메뉴바를 만든다.

        네이티브 tk 메뉴바는 sv_ttk 테마를 받지 않고 Windows에서 strip 색을 제어할
        수 없어 다크 모드에서 밝게 보인다. ttk 바 + 다크색 드롭다운으로 대체한다.
        언어/테마 변경 시 다시 호출되므로 기존 바를 파괴하고 새로 만든다(body 위로 고정).
        """
        t = self.t
        dark = self.theme == "dark"
        old = getattr(self, "_menubar", None)
        if old is not None:
            old.destroy()
        bar = ttk.Frame(self.root)
        if getattr(self, "body", None) is not None:
            bar.pack(side="top", fill="x", before=self.body)
        else:
            bar.pack(side="top", fill="x")
        self._menubar = bar

        def _dropdown(items):
            m = tk.Menu(bar, tearoff=0)
            theme.style_menu(m, dark=dark)
            for label, cmd in items:
                m.add_command(label=label, command=cmd)
            return m

        file_menu = _dropdown([
            (t["new_config"], self._new_config),
            (t["choose_config"], self._choose_config),
        ])
        lang_menu = _dropdown([
            (t["lang_ko"], lambda: self._set_language("ko")),
            (t["lang_en"], lambda: self._set_language("en")),
        ])
        theme_menu = _dropdown([
            (t["theme_light"], lambda: self._set_theme("light")),
            (t["theme_dark"], lambda: self._set_theme("dark")),
        ])
        # 스토리지·디바이스 관리는 메인 창 하단 패널로 이동(관리 메뉴 제거).
        for label, menu in (
            (t["menu_file"], file_menu),
            (t["menu_language"], lang_menu),
            (t["menu_theme"], theme_menu),
        ):
            ttk.Menubutton(
                bar, text=label, menu=menu, direction="below",
                style="Toolbutton",
            ).pack(side="left", padx=(2, 0), pady=2)

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

    def _quit(self) -> None:
        # 종료 시 daemon도 정지(센티넬만 즉시 생성, 대기 없음 — daemon이 ~1초 내 종료).
        if self.config_path:
            try:
                from stardustlib.gui import actions

                actions.daemon_signal_stop(self.config_path)
            except Exception:  # noqa: BLE001
                pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    # --- 제목 / 언어 ---

    def _update_title(self) -> None:
        """창 제목에 앱 이름 + 설정 파일명을 표시한다."""
        if self.config_path:
            name = os.path.basename(self.config_path)
            self.root.title(f"{self.t['app_title']} — {name}")
        else:
            self.root.title(self.t["app_title"])

    def _set_language(self, lang: str) -> None:
        if lang == self.lang:
            return
        self.lang = lang
        prefs.save(lang=lang)  # 다음 실행에도 유지
        self.t = i18n.get_text(lang)
        self.root.title(self.t["app_title"])
        self._build_menu()
        self._rebuild_body()

    # --- 위젯 구성 ---

    def _build_body(self) -> None:
        """경로 바 → 하단 고정 바 → 파일 목록/관리 패널 분할.

        하단 고정 바(상태바·액션 툴바)를 파일 목록보다 먼저 pack해 공간을 선점한다.
        pack은 선언 순서로 공간을 떼어 가므로, expand=True인 목록을 먼저 배치하면
        창이 작거나 DPI 배율이 높을 때 마지막에 배치된 툴바가 화면 밖으로 밀린다
        (업로드 버튼이 사라지는 증상). 목록이 줄어드는 쪽이 옳다.
        """
        self._build_path_bar(self.body)
        self._update_title()
        self._build_statusbar(self.body)
        self._build_action_toolbar(self.body)

        # 파일 목록 + 하단 스토리지·디바이스 패널을 세로 분할(구분선 드래그로 조절).
        paned = ttk.PanedWindow(self.body, orient="vertical")
        paned.pack(fill="both", expand=True)

        file_frame = ttk.Frame(paned)
        self._build_file_tree(file_frame)
        paned.add(file_frame, weight=3)

        mgmt_frame = ttk.Frame(paned)
        self._build_mgmt_panel(mgmt_frame)
        paned.add(mgmt_frame, weight=1)
        self._place_sash(paned)

        self._refresh_login_state()

    def _place_sash(self, paned: ttk.PanedWindow) -> None:
        """분할선 초기 위치를 파일 목록 쪽으로 둔다.

        weight는 창 크기가 바뀔 때의 분배 비율일 뿐 초기 위치가 아니다. 그대로 두면
        Tk가 자식의 요청 크기로 나누어, 비어 있는 관리 패널이 창 절반을 차지하고 파일
        목록에 서너 줄만 남는다.
        """
        def _apply() -> None:
            try:
                height = paned.winfo_height()
                if height < 100:  # 아직 배치 전 — 다음 프레임에 다시 시도
                    paned.after(50, _apply)
                    return
                paned.sashpos(0, int(height * _FILE_PANE_RATIO))
            except tk.TclError:  # 창이 이미 닫혔으면 무시
                pass

        paned.after_idle(_apply)

    def _rebuild_body(self) -> None:
        """본문 위젯을 전부 다시 만든다(언어 전환 등 라벨 일괄 변경 시)."""
        self.body.destroy()
        self.body = ttk.Frame(self.root)
        self.body.pack(fill="both", expand=True)
        self._build_body()
        if self.config_path:
            self.refresh()
        else:
            self._set_status(self.t["select_config_hint"])

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


def run_gui(config_path: str | None) -> int:
    """GUI를 실행한다 (블로킹). 이미 실행 중이면 그 창을 띄우고 1을 반환한다.

    GUI가 여러 개 뜨면 같은 스토리지를 다투고 각자 daemon을 감독하려 들어 서로의
    판단을 무너뜨린다.
    """
    from stardustlib.single_instance import InstanceLock, gui_lock_path, request_focus

    lock = InstanceLock(gui_lock_path())
    if not lock.acquire():
        pid = lock.holder().get("pid")
        logger.info("StardustFS GUI가 이미 실행 중입니다 (pid=%s)", pid)
        request_focus(lock.path)  # 먼저 뜬 창을 앞으로 끌어올린다
        return 1

    _hide_console_if_frozen()
    root = tk.Tk()
    try:
        StardustApp(root, config_path, instance_lock=lock)
        root.mainloop()
    finally:
        lock.release()
    return 0
