"""설정 파일 선택·생성과 계정 로그인/로그아웃.

설정이 없을 때 새로 만드는 경로(닭-달걀 해소)와, 로그인 상태에 따른 버튼 활성화를
다룬다. StardustApp에 믹스인으로 결합한다.
"""

from __future__ import annotations

import logging
from tkinter import filedialog, messagebox, simpledialog

from stardustlib.gui import actions

logger = logging.getLogger(__name__)


class SessionMixin:
    """설정 진입 + 인증."""

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
            self._adopt_config(payload)
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
            self._adopt_config(path)

    def _adopt_config(self, path: str) -> None:
        """설정 파일을 현재 설정으로 채택하고 화면을 처음부터 다시 읽는다."""
        self.config_path = path
        self._update_title()
        self.vpath = "/"
        self.path_var.set("/")
        self._daemon_restart_until = 0.0  # 새 설정 → 즉시 감독 시작
        self.worker.submit(lambda: actions.invalidate(None), lambda *_a: None)
        self.refresh()
        self._refresh_daemon()
        self._refresh_login_state()

    # --- 인증 ---

    def _logged_in(self) -> bool:
        if not self.config_path:
            return False
        try:
            return actions.is_logged_in(self.config_path)
        except Exception:  # noqa: BLE001
            return False

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
