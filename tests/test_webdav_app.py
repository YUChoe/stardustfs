"""create_webdav_app() 단위 테스트."""

import base64
from io import BytesIO
from unittest.mock import MagicMock

from stardustlib.webdav_provider import create_webdav_app
from stardustlib.models import StardustConfig, WebDAVConfig


def _make_config(username: str = "admin", password: str = "secret") -> StardustConfig:
    """테스트용 설정 생성."""
    return StardustConfig(
        version=1,
        webdav=WebDAVConfig(
            host="127.0.0.1",
            port=8080,
            username=username,
            password=password,
        ),
        sources=[],
        metadata_db=":memory:",
        key_file=None,
    )


def _make_environ(
    method: str = "GET",
    path: str = "/",
    auth_header: str | None = None,
) -> dict:
    """최소 WSGI 환경 생성."""
    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_HOST": "localhost:8080",
        "wsgi.input": BytesIO(b""),
        "wsgi.errors": BytesIO(),
        "wsgi.url_scheme": "http",
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": "0",
    }
    if auth_header is not None:
        environ["HTTP_AUTHORIZATION"] = auth_header
    return environ


def _call_app(app, environ) -> str:
    """WSGI 앱을 호출하고 응답 상태를 반환한다."""
    response_started = []

    def start_response(status, headers, exc_info=None):
        response_started.append(status)

    try:
        list(app(environ, start_response))
    except Exception:
        pass

    return response_started[0] if response_started else ""


class TestCreateWebdavApp:
    """create_webdav_app 함수 테스트."""

    def test_returns_wsgi_app(self):
        """WSGI 앱 인스턴스를 반환한다."""
        config = _make_config()
        jbod = MagicMock()
        engine = MagicMock()

        app = create_webdav_app(config, jbod, engine)

        assert app is not None
        assert callable(app)

    def test_basic_auth_rejects_no_credentials(self):
        """인증 정보 없이 접근 시 401을 반환한다."""
        config = _make_config(username="user1", password="pass1")
        jbod = MagicMock()
        engine = MagicMock()

        app = create_webdav_app(config, jbod, engine)
        environ = _make_environ()

        status = _call_app(app, environ)
        assert "401" in status

    def test_basic_auth_rejects_wrong_credentials(self):
        """잘못된 인증 정보로 접근 시 401을 반환한다."""
        config = _make_config(username="admin", password="correct_pass")
        jbod = MagicMock()
        engine = MagicMock()

        app = create_webdav_app(config, jbod, engine)

        wrong_creds = base64.b64encode(b"admin:wrong_pass").decode()
        environ = _make_environ(auth_header=f"Basic {wrong_creds}")

        status = _call_app(app, environ)
        assert "401" in status

    def test_basic_auth_accepts_correct_credentials(self):
        """올바른 인증 정보로 접근 시 401이 아닌 응답을 반환한다."""
        config = _make_config(username="admin", password="correct_pass")
        jbod = MagicMock()
        engine = MagicMock()

        app = create_webdav_app(config, jbod, engine)

        correct_creds = base64.b64encode(b"admin:correct_pass").decode()
        environ = _make_environ(auth_header=f"Basic {correct_creds}")

        status = _call_app(app, environ)
        # 인증 성공 시 401이 아닌 다른 응답 (provider stub이므로 404 예상)
        assert "401" not in status
