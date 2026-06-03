"""로컬 서버 대상 CLI E2E 테스트.

전제: 로컬 서버가 http://127.0.0.1:8000 에서 실행 중(격리 테스트 DB 권장).

흐름:
1. 테스트 계정 등록(POST /auth/register, 이미 있으면 통과)
2. device 등록(daemon 역할을 대신 1회 수행 — CLI는 비등록 모델이므로)
3. CLI 서브프로세스로 status/devices/put/ls/get/rm 검증 (인자 리스트 호출이라
   Git Bash 경로 변환 영향 없음)

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
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    line = f"[{mark}] {name}"
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
        "webdav": {
            "host": "127.0.0.1", "port": 8098,
            "username": "admin", "password": "x",
        },
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
    """daemon 역할: 로그인 후 device를 1회 등록한다 (CLI가 식별할 수 있도록)."""
    from stardustlib.auth_client import AuthClient
    from stardustlib.device_manager import DeviceManager

    server = config["server"]
    auth = AuthClient(server["url"])
    await auth.login(EMAIL, PASSWORD)
    dm = DeviceManager(auth, server["url"], server["device_name"],
                       config["p2p"]["port"])
    device_id = await dm.register()
    await dm.stop()
    await auth.close()
    return device_id


def _cli(cfg_path: str, args: list, env: dict):
    cmd = [sys.executable, "stardustfs.py", "--config", cfg_path] + args
    # CLI는 UTF-8로 출력하므로 cp949 기본 디코딩을 피한다.
    return subprocess.run(
        cmd, cwd=CLIENT_DIR, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def main() -> int:
    config = _make_config()
    cfg_path = os.path.join(BASE, "e2e-config.json")

    # 1. 계정 등록 (이미 있으면 통과)
    r = httpx.post(
        f"{SERVER_URL}/auth/register",
        json={"email": EMAIL, "password": PASSWORD}, timeout=10.0,
    )
    _check("account register", r.status_code in (200, 201, 400, 409),
           f"HTTP {r.status_code}")

    # 2. device 등록 (daemon 역할)
    os.environ["STARDUST_EMAIL"] = EMAIL
    os.environ["STARDUST_PASSWORD"] = PASSWORD
    device_id = asyncio.run(_register_device(config))
    _check("device register", bool(device_id), f"device_id={device_id[:8]}")

    # CLI 서브프로세스 환경
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["STARDUST_EMAIL"] = EMAIL
    env["STARDUST_PASSWORD"] = PASSWORD

    # 3. status
    res = _cli(cfg_path, ["status"], env)
    _check("status exit0", res.returncode == 0, res.stderr.strip()[-200:])

    # 4. devices — 우리 device가 보여야 함
    res = _cli(cfg_path, ["devices"], env)
    _check("devices exit0", res.returncode == 0, res.stderr.strip()[-200:])
    _check("devices lists e2e-cli", "e2e-cli" in res.stdout, res.stdout.strip())

    # 5. put
    sample = os.path.join(BASE, "sample.txt")
    payload = "stardustfs e2e payload 한글 — em-dash\n"
    with open(sample, "w", encoding="utf-8") as f:
        f.write(payload)
    res = _cli(cfg_path, ["put", sample, "/e2e.txt"], env)
    _check("put exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])

    # 6. ls — e2e.txt 존재
    res = _cli(cfg_path, ["ls", "/"], env)
    _check("ls shows e2e.txt", "e2e.txt" in res.stdout, res.stdout.strip())

    # 7. get — 바이트 일치
    out = os.path.join(BASE, "out.txt")
    res = _cli(cfg_path, ["get", "/e2e.txt", out], env)
    _check("get exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])
    got = ""
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            got = f.read()
    _check("get bytes identical", got == payload, repr(got)[:120])

    # 8. rm + ls
    res = _cli(cfg_path, ["rm", "/e2e.txt"], env)
    _check("rm exit0", res.returncode == 0,
           (res.stdout + res.stderr).strip()[-200:])
    res = _cli(cfg_path, ["ls", "/"], env)
    _check("ls after rm empty of e2e.txt", "e2e.txt" not in res.stdout,
           res.stdout.strip())

    sys.stdout.buffer.write(
        f"\n결과: {_passed} passed, {_failed} failed\n".encode("utf-8")
    )
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
