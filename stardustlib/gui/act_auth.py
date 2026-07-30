"""GUI 백엔드 — 계정 인증(로그인/로그아웃/상태).

토큰은 CredentialStore(메타데이터 DB)에 저장된다. 비밀번호는 로그에 남기지 않는다.
"""

from __future__ import annotations

import asyncio

from stardustlib.config_loader import ConfigLoader


def login(config_path: str, email: str, password: str,
          key_password: str | None = None) -> None:
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore

    async def _do():
        config = ConfigLoader(config_path).load()
        server = config.get("server") or {}
        server_url = server.get("url") if isinstance(server, dict) else None
        if not server_url:
            raise RuntimeError("server.url이 설정되어 있지 않습니다.")
        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        try:
            await auth.login(email, password)
            if key_password:
                auth.set_key_password(key_password)
        finally:
            await auth.close()

    asyncio.run(_do())


def logout(config_path: str) -> None:
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore

    async def _do():
        config = ConfigLoader(config_path).load()
        store = CredentialStore(config["metadata_db"])
        if not store.exists():
            return
        server = config.get("server") or {}
        server_url = (server.get("url") if isinstance(server, dict) else "") or ""
        auth = AuthClient(server_url, credential_store=store)
        auth.load_from_store()
        if server_url:
            await auth.logout()
        await auth.close()
        store.clear()

    asyncio.run(_do())


def is_logged_in(config_path: str) -> bool:
    config = ConfigLoader(config_path).load()
    from stardustlib.credential_store import CredentialStore
    return CredentialStore(config["metadata_db"]).exists()


def account_email(config_path: str | None) -> str:
    """로그인한 계정의 이메일(없거나 읽을 수 없으면 빈 문자열).

    자격증명 저장소는 JSON 파일이라 메인 스레드에서 읽어도 sqlite 스레드 제약을
    타지 않는다. 화면 표시용이므로 실패는 빈 문자열로 흡수한다.
    """
    if not config_path:
        return ""
    from stardustlib.credential_store import CredentialStore

    try:
        config = ConfigLoader(config_path).load()
        data = CredentialStore(config["metadata_db"]).load() or {}
        return str(data.get("email") or "")
    except Exception:  # noqa: BLE001 — 표시용 조회
        return ""
