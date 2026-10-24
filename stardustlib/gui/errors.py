"""작업 실패를 사용자용 문구로 바꾼다.

워커에서 올라온 예외를 그대로 `str()`로 보여주면 백엔드의 한국어 메시지가 영어 UI에
섞이고, 네트워크 예외처럼 내부 표현이 노출된다. 알려진 예외 유형은 현재 언어의
문구로 바꾸고, 모르는 예외만 원문을 붙여 보여준다(원인을 숨기지 않는다).
"""

from __future__ import annotations

import logging

from stardustlib.exceptions import AuthenticationError, InsufficientStorageError
from stardustlib.gui.act_auth import ServerNotConfigured
from stardustlib.gui.act_core import LoginRequired, RemotePathExists

logger = logging.getLogger(__name__)


def _detail(exc: BaseException) -> str:
    """경로 등 예외가 가리키는 대상(없으면 예외 문자열)."""
    filename = getattr(exc, "filename", None)
    if filename:
        return str(filename)
    if exc.args:
        return str(exc.args[0])
    return str(exc)


def _is_network(exc: BaseException) -> bool:
    """서버에 닿지 못한 실패인지(httpx/aiohttp/소켓)."""
    if isinstance(exc, ConnectionError):
        return True
    module = type(exc).__module__ or ""
    return module.startswith(("httpx", "httpcore", "aiohttp"))


def friendly(exc: BaseException, t: dict) -> str:
    """예외를 현재 언어의 사용자 문구로 바꾼다."""
    if isinstance(exc, LoginRequired):
        return t["err_login"]
    if isinstance(exc, ServerNotConfigured):
        return t["err_no_server"]
    if isinstance(exc, AuthenticationError):
        text = str(exc)
        if "Cannot connect" in text:
            return t["err_network"]
        if "Server error" in text:
            return t["err_server"]
        return t["err_auth"]
    if isinstance(exc, InsufficientStorageError):
        return t["err_no_space"]
    if isinstance(exc, (RemotePathExists, FileExistsError)):
        return t["err_exists"].format(msg=_detail(exc))
    if isinstance(exc, FileNotFoundError):
        return t["err_not_found"].format(msg=_detail(exc))
    if isinstance(exc, PermissionError):
        return t["err_permission"].format(msg=_detail(exc))
    if isinstance(exc, TimeoutError):
        return t["err_timeout"]
    if _is_network(exc):
        return t["err_network"]
    text = str(exc) or type(exc).__name__
    return t["err_unexpected"].format(msg=text)


def describe(payload, t: dict) -> str:
    """워커 실패 payload(보통 예외)를 표시 문구로. 예외가 아니면 문자열 그대로."""
    if isinstance(payload, BaseException):
        return friendly(payload, t)
    return str(payload)
