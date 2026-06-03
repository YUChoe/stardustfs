"""로컬 서버 대상 CLI E2E 테스트 (토큰 인증 흐름).

전제: 로컬 서버가 http://127.0.0.1:8000 에서 실행 중(격리 테스트 DB 권장).

흐름:
1. 테스트 계정 등록(POST /auth/register, 이미 있으면 통과)
2. CLI `login`(--email/--password) → 자격증명 저장소(credentials.json) 생성
3. daemon 역할: 저장된 토큰으로 device 등록(CLI는 비등록 모델이므로)
4. CLI devices/put/ls/get 검증 — STARDUST_EMAIL/PASSWORD 없이 저장소 토큰만 사용
5. CLI `logout` → 서버 토큰 취소 + 저장소 삭제
6. logout 후 devices → "login 필요"(비0 종료)

실사용 계정 오염 방지를 위해 e2e-test@example.com 사용(핸드오버 7절).

사용: PYTHONPATH=. python scripts/e2e_cli_local.py
"""

import asyncio
import json
import os
import subprocess
import sys

import httpx

CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_URL = "http://127.0.0.1:8000"
EMAIL = "e2e-test@example.com"
PASSWORD = "e2e-pass-123"
BASE = os.path.join(CLIENT_DIR, ".tmp-e2e")

_passed = 0
_failed = 0


def _check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
    else:
        _failed += 1
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" — {detail}"
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _make_config() -> dict:
    os.makedirs(BASE, exist_ok=True)
    key_path = os.path.join(BASE, "master.key")
    if not os.path.exists(key_path):
        with open(key_path, "wb") as f:
            f.write(os.urandom(32))
    config = {
        "version": 2,
        "sources": [{
            "type": "loopback", "id": "e2e-loop",
            "path": os.path.join(BASE, "vol.img"), "size": 10485760,
        }],
        "metadata_db": os.path.join(BASE, "meta.db"),
        "key_file": key_path,
        "server": {"url": SERVER_URL, "device_name": "e2e-cli"},
        "sync": {"interval_seconds": 30, "conflict_strategy": "copy"},
        "p2p": {"port": 9098, "enabled": False},
    }
    with open(os.path.join(BASE, "e2e-config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return config


async def _register_device(config: dict) -> str:
    """daemon 역할: 저장된 토큰으로 device를 등록한다(CLI가 식별할 수 있도록)."""
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore
    from stardustlib.device_manager import DeviceManager

    server = config["server"]
    store = CredentialStore(config["metadata_db"])
    auth = AuthClient(server["url"], credential_store=store)
    auth.load_from_store()
    dm = DeviceManager(auth, server["url"], server["device_name"],
                       config["p2p"]["port"])
    device_id = await dm.register()
    await dm.stop()
    await auth.close()
    return device_id


def _env_without_credentials() -> dict:
    """STARDUST_EMAIL/PASSWORD 없이 환경을 구성한다(저장소 토큰만 사용 검증)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("STARDUST_EMAIL", "STARDUST_PASSWORD")}
    env["PYTHONPATH"] = "."
    return env


def _cli(cfg_path: str, args: list, env: dict):
    cmd = [sys.executable, "stardustfs.py", "--config", cfg_path] + args
    return subprocess.run(
        cmd, cwd=CLIENT_DIR, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _store_path(config: dict) -> str:
    return config["metadata_db"] + ".credentials.json"


def main() -> int:
    config = _make_config()
    cfg = os.path.join(BASE, "e2e-config.json")
    env = _env_without_credentials()

    # 1. 계정 등록
    r = httpx.post(
        f"{SERVER_URL}/auth/register",
        json={"email": EMAIL, "password": PASSWORD}, timeout=10.0,
    )
    _check("account register", r.status_code in (200, 201, 400, 409),
           f"HTTP {r.status_code}")

    # 2. login → 자격증명 저장소 생성 (env에 비밀번호 없이 플래그로 입력)
    res = _cli(cfg, ["login", "--email", EMAIL, "--password", PASSWORD], env)
    _check("login exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])
    _check("credentials.json 생성", os.path.exists(_store_path(config)))

    # 3. daemon 역할: 저장된 토큰으로 device 등록
    os.environ["STARDUST_EMAIL"] = ""  # 토큰만 쓰는지 확인용(영향 없음)
    device_id = asyncio.run(_register_device(config))
    _check("device register (stored token)", bool(device_id),
           f"device_id={device_id[:8]}")

    # 4. devices — 저장소 토큰만으로(비밀번호 env 없이) 동작
    res = _cli(cfg, ["devices"], env)
    _check("devices exit0 (token only)", res.returncode == 0,
           res.stderr.strip()[-200:])
    _check("devices lists e2e-cli", "e2e-cli" in res.stdout,
           res.stdout.strip())

    # 5. put → ls → get 바이트 일치
    sample = os.path.join(BASE, "sample.txt")
    payload = "token-flow e2e 한글 — em-dash\n"
    with open(sample, "w", encoding="utf-8") as f:
        f.write(payload)
    res = _cli(cfg, ["put", sample, "/e2e.txt"], env)
    _check("put exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])
    res = _cli(cfg, ["ls", "/"], env)
    _check("ls shows e2e.txt", "e2e.txt" in res.stdout, res.stdout.strip())
    out = os.path.join(BASE, "out.txt")
    res = _cli(cfg, ["get", "/e2e.txt", out], env)
    _check("get exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])
    got = ""
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            got = f.read()
    _check("get bytes identical", got == payload, repr(got)[:120])

    # 6. logout → 서버 취소 + 저장소 삭제
    res = _cli(cfg, ["logout"], env)
    _check("logout exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])
    _check("credentials.json 삭제", not os.path.exists(_store_path(config)))

    # 7. logout 후 devices → login 필요(비0)
    res = _cli(cfg, ["devices"], env)
    _check("devices after logout fails", res.returncode != 0,
           f"exit={res.returncode}")

    sys.stdout.buffer.write(
        f"\n결과: {_passed} passed, {_failed} failed\n".encode("utf-8")
    )
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
