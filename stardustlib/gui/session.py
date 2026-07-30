"""설정 파일 선택·생성과 계정 로그인/로그아웃.

설정이 없을 때 새로 만드는 경로(닭-달걀 해소)와, 로그인 상태에 따른 계정 버튼
표시를 다룬다. 입력은 공용 폼 다이얼로그로 한 창에서 받는다 — 입력 창을 연달아
띄우면 중간에 취소했을 때 처음부터 다시 해야 하고, 실패 사유를 입력값과 함께 보여줄
자리가 없다. StardustApp에 믹스인으로 결합한다.
"""

from __future__ import annotations

import logging
import socket
from tkinter import filedialog

from stardustlib.gui import actions
from stardustlib.gui.widgets.form_dialog import (
    BOOL,
    DIR,
    PASSWORD,
    Field,
    FormDialog,
)

logger = logging.getLogger(__name__)


class SessionMixin:
    """설정 진입 + 인증."""

    # --- 설정 ---

    def _new_config(self) -> None:
        """폴더·서버·디바이스 이름·키 생성 여부를 한 창에서 받아 설정을 만든다."""
        t = self.t
        fields = (
            Field("base", t["nc_pick_dir"], DIR, required=True,
                  pick_title=t["nc_pick_dir"]),
            Field("server", t["nc_server"],
                  initial="https://stardustfs.noizze.net"),
            Field("device", t["nc_device"], initial=socket.gethostname(),
                  required=True),
            Field("generate_key", t["nc_key_q_short"], BOOL, initial="1"),
        )

        def submit(values, dlg) -> None:
            generate_key = values["generate_key"]

            def make():
                return actions.create_config(
                    values["base"], values["server"] or None, values["device"],
                    generate_key=generate_key,
                )

            def done(ok, payload):
                if not ok:
                    dlg.error(str(payload))
                    return
                dlg.close()
                self._adopt_config(payload)
                self._set_status(
                    t["nc_done_new"] if generate_key else t["nc_done_restore"]
                )

            self._set_status(t["nc_busy"])
            self.worker.submit(make, done)

        FormDialog(self, t["new_config"], fields, submit)

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
        self._set_vpath("/")
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
        """로그인 여부에 따라 계정 버튼의 라벨·동작을 바꾼다.

        비로그인이면 '로그인', 로그인 상태면 계정 이메일과 '로그아웃'을 보여준다.
        상시 비활성 버튼을 두지 않는다.
        """
        btn = getattr(self, "account_btn", None)
        if btn is None:
            return
        logged = self._logged_in()
        self._enable(btn, bool(self.config_path))
        btn.config(text=self.t["logout"] if logged else self.t["login"],
                   command=self._logout if logged else self._login)
        email = actions.account_email(self.config_path) if logged else ""
        self.account_label.config(text=email or "")

    def _login(self) -> None:
        t = self.t
        if not self.config_path:
            self._show_banner(t["need_config"], level="warning")
            return
        fields = (
            Field("email", t["login_email"], required=True),
            Field("password", t["login_password"], PASSWORD, required=True),
            Field("key_pw", t["login_keypw"], PASSWORD),
        )

        def submit(values, dlg) -> None:
            cfg = self.config_path
            email = values["email"]

            def done(ok, payload):
                if not ok:
                    dlg.error(str(payload))
                    return
                dlg.close()
                self._after_login(t["login_ok"].format(email=email))

            self._set_status(t["login_busy"])
            self.worker.submit(
                lambda: actions.login(
                    cfg, email, values["password"], values["key_pw"] or None
                ),
                done,
            )

        FormDialog(self, t["login"], fields, submit, ok_label=t["login"])

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
