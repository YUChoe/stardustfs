"""GUI 백엔드 — daemon 라이프사이클(상태 조회, 정지·리로드 신호, 기동).

신호 계열은 대기하지 않는다(GUI 멈춤 방지). daemon은 GUI와 별개 프로세스이므로 자체
코어 초기화를 수행하고, 로그는 GUI 콘솔이 아니라 파일로 보낸다.
"""

from __future__ import annotations

import os
import subprocess
import sys

from stardustlib import daemon
from stardustlib.config_loader import ConfigLoader

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def daemon_status(config_path: str) -> dict:
    config = ConfigLoader(config_path).load()
    return daemon.read_status(config["metadata_db"])


def daemon_signal_stop(config_path: str) -> dict:
    """정지 신호만 보내고 대기하지 않는다(GUI 종료 시 UI 멈춤 방지)."""
    config = ConfigLoader(config_path).load()
    return daemon.signal_stop(config["metadata_db"])


def daemon_signal_reload(config_path: str) -> dict:
    """config 리로드 신호를 보낸다(daemon이 로컬 소스를 다시 mount). 대기 없음."""
    config = ConfigLoader(config_path).load()
    return daemon.signal_reload(config["metadata_db"])


def daemon_start(config_path: str) -> int:
    """daemon을 백그라운드 프로세스로 시작하고 pid를 반환한다.

    daemon의 stdout/stderr는 {metadata_db}.daemon.log로 보낸다 — GUI 콘솔에
    daemon 초기화 로그가 섞여 '초기화 반복'처럼 보이지 않도록 한다(daemon은 GUI와
    별개 프로세스라 자체 코어 초기화를 수행한다).
    """
    config = ConfigLoader(config_path).load()
    log_path = config["metadata_db"] + ".daemon.log"
    log_file = open(log_path, "a", encoding="utf-8")
    # 자식 프로세스가 로그를 UTF-8로 쓰도록 강제(Windows 기본 cp949 → 파일 mojibake 방지).
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # 프로즌(PyInstaller) exe에는 stardustfs.py 소스가 없으므로 exe 자신의 daemon
    # 서브커맨드를 직접 호출한다. 소스 실행 시에는 python으로 stardustfs.py를 호출.
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "daemon", "--config", config_path]
        cwd = None
    else:
        cmd = [sys.executable, "stardustfs.py", "daemon", "--config", config_path]
        cwd = _REPO_ROOT
    # Windows: 자식(daemon) 콘솔 창이 뜨지 않도록 CREATE_NO_WINDOW.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT, env=env,
            creationflags=creationflags,
        )
    finally:
        log_file.close()  # 자식이 자체 핸들을 보유하므로 부모 핸들은 닫는다
    return proc.pid
