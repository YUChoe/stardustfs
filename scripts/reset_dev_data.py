"""개발 데이터 초기화 (런칭 전 레거시 blob 폐기용).

로컬 메타데이터·스토리지 이미지·패리티 보관소를 지우고, 선택적으로 서버의 이 계정
메타데이터를 빈 DB로 덮어쓰고 이 device 등록을 삭제한다(hosting/replicas는 서버
스키마의 ON DELETE CASCADE로 함께 정리된다).

기본은 dry-run이다. 실제로 지우려면 --yes를 준다. 지우기 전에 metadata.db를
`{metadata_db}.reset-YYYYmmdd-HHMMSS.bak`으로 복사해 롤백 경로를 남긴다.

master.key는 기본 보존한다(--reset-key로 삭제). 키를 지우면 서버의 키 백업 복원
흐름을 다시 타야 하므로 특별한 이유가 없으면 보존한다.

사용 예:
    python scripts/reset_dev_data.py --config dev-config.json            # 미리보기
    python scripts/reset_dev_data.py --config dev-config.json --yes
    python scripts/reset_dev_data.py --config dev-config.json --yes --server

주의: daemon을 먼저 정지해야 한다(`python stardustfs.py daemon stop --config ...`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _local_targets(config: dict) -> tuple[list[str], list[str]]:
    """(삭제할 파일 목록, 삭제할 디렉토리 목록)."""
    db = config["metadata_db"]
    files = [
        db, db + "-wal", db + "-shm",
        db + ".syncstate.json",
        db + ".daemon.json", db + ".daemon.ctl.json",
    ]
    dirs = [db + ".parity"]
    for source in config.get("sources", []):
        path = source.get("path")
        if not path:
            continue
        if source.get("type") == "loopback":
            files.append(path)          # FAT 이미지 (재기동 시 재생성·포맷)
            dirs.append(path + ".d")    # 구버전 컴패니언 디렉토리
        else:
            dirs.append(path)           # 디렉토리 소스 내용
    return files, dirs


def _backup(db_path: str, apply: bool) -> str | None:
    if not os.path.exists(db_path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{db_path}.reset-{stamp}.bak"
    if apply:
        shutil.copy2(db_path, dest)
    return dest


def _reset_local(config: dict, apply: bool) -> None:
    files, dirs = _local_targets(config)
    backup = _backup(config["metadata_db"], apply)
    if backup:
        print(f"{'백업' if apply else '백업 예정'}: {backup}")
    for path in files:
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        print(f"{'삭제' if apply else '삭제 예정'}: {path} ({size:,} bytes)")
        if apply:
            os.remove(path)
    for path in dirs:
        if not os.path.isdir(path):
            continue
        n = sum(len(fs) for _r, _d, fs in os.walk(path))
        print(f"{'삭제' if apply else '삭제 예정'}: {path}/ (파일 {n}개)")
        if apply:
            shutil.rmtree(path, ignore_errors=True)


def _empty_metadata_blob(config: dict) -> bytes:
    """빈 메타데이터 DB를 만들어 master.key로 암호화한 blob을 돌려준다.

    sync_client._encrypt_blob과 같은 형식(iv 12B + tag 16B + ciphertext)이다.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from stardustlib.metadata_store import MetadataStore

    key_file = config.get("key_file")
    key = b""
    if key_file and os.path.exists(key_file):
        with open(key_file, "rb") as f:
            key = f.read()

    tmp = config["metadata_db"] + ".reset-empty.tmp"
    for leftover in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
    store = MetadataStore(tmp, key)
    try:
        conn = store._get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()
    with open(tmp, "rb") as f:
        plain = f.read()
    for leftover in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)

    if not key:
        print("경고: key_file이 없어 평문으로 업로드합니다")
        return plain
    iv = os.urandom(12)
    ct_with_tag = AESGCM(key).encrypt(iv, plain, None)
    return iv + ct_with_tag[-16:] + ct_with_tag[:-16]


def _valid_token(config: dict, server_url: str) -> str | None:
    """저장된 자격증명으로 유효한 access token을 얻는다(만료 시 refresh).

    저장된 access_token을 그대로 쓰면 만료된 경우 401이 나므로, AuthClient의
    갱신 경로를 그대로 탄다.
    """
    import asyncio

    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore

    store = CredentialStore(config["metadata_db"])
    auth = AuthClient(server_url, credential_store=store)
    if not auth.load_from_store():
        print("자격증명을 불러오지 못했습니다")
        return None

    async def run() -> str | None:
        try:
            return await auth.get_valid_token()
        except Exception as e:  # noqa: BLE001 — 만료·네트워크 실패
            print(f"토큰 갱신 실패: {e}")
            return None
        finally:
            await auth.close()

    return asyncio.run(run())


def _reset_server(
    config: dict, apply: bool, device_id: str | None = None
) -> bool:
    """서버의 이 계정 메타데이터를 빈 DB로 덮어쓰고 이 device 등록을 삭제한다.

    성공하면 True. 실패(토큰 만료·네트워크·비-2xx)면 False — 호출자는 로컬 삭제를
    멈춰야 한다. 서버가 옛 메타데이터를 들고 있는 채로 로컬만 지우면 다음 동기화에서
    그대로 되살아난다.
    """
    import httpx

    cred_path = config["metadata_db"] + ".credentials.json"
    if not os.path.exists(cred_path):
        print(f"자격 파일 없음, 서버 초기화 건너뜀: {cred_path}")
        return False
    with open(cred_path, encoding="utf-8") as f:
        cred = json.load(f)
    url = cred["server_url"].rstrip("/")

    token = cred["access_token"]
    if apply:
        fresh = _valid_token(config, url)
        if not fresh:
            print("유효한 토큰을 얻지 못해 서버 초기화를 건너뜁니다 "
                  "(로그인 후 다시 --server로 실행하세요)")
            return False
        token = fresh
    headers = {"Authorization": f"Bearer {token}"}

    ok = True
    blob = _empty_metadata_blob(config)
    print(f"{'업로드' if apply else '업로드 예정'}: 빈 메타데이터 "
          f"{len(blob):,} bytes → PUT {url}/sync/metadata (강제 덮어쓰기)")
    if apply:
        with httpx.Client(timeout=30.0) as client:
            resp = client.put(
                f"{url}/sync/metadata", headers=headers, content=blob,
            )
            print(f"  → HTTP {resp.status_code} {resp.text[:200]}")
            ok = ok and resp.status_code == 200

    # 이 device 등록 삭제 → hosting/replicas가 CASCADE로 정리된다.
    if not device_id:
        daemon_state = config["metadata_db"] + ".daemon.json"
        if os.path.exists(daemon_state):
            with open(daemon_state, encoding="utf-8") as f:
                device_id = json.load(f).get("device_id")
    if not device_id:
        print("device_id를 알 수 없어 device 삭제를 건너뜁니다 "
              "(--device-id로 지정하세요. 서버 chunks 행은 어느 경우에도 남습니다)")
        return ok
    print(f"{'삭제' if apply else '삭제 예정'}: DELETE {url}/devices/{device_id}")
    if apply:
        with httpx.Client(timeout=30.0) as client:
            resp = client.delete(f"{url}/devices/{device_id}", headers=headers)
            print(f"  → HTTP {resp.status_code}")
            # 204=삭제됨, 404=이미 없음(멱등적으로 성공 취급)
            ok = ok and resp.status_code in (200, 204, 404)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="개발 데이터 초기화")
    parser.add_argument("--config", required=True, help="설정 파일 경로")
    parser.add_argument("--yes", action="store_true", help="실제로 삭제한다")
    parser.add_argument("--server", action="store_true",
                        help="서버 메타데이터도 빈 DB로 덮어쓰고 device를 삭제한다")
    parser.add_argument("--device-id", help="서버에서 삭제할 device_id 직접 지정")
    parser.add_argument("--reset-key", action="store_true",
                        help="master.key도 삭제한다(기본 보존)")
    parser.add_argument("--force-local", action="store_true",
                        help="서버 초기화가 실패해도 로컬을 지운다")
    args = parser.parse_args()

    from stardustlib.config_loader import ConfigLoader
    from stardustlib import daemon

    config = ConfigLoader(args.config).load()
    db = config["metadata_db"]

    status = daemon.read_status(db)
    if status.get("running"):
        msg = (f"daemon이 실행 중입니다 (pid={status.get('pid')}). "
               f"정지: python stardustfs.py daemon stop --config {args.config}")
        if args.yes:
            print(msg)
            return 1
        print(f"[주의] {msg}\n")  # 미리보기는 그대로 진행

    if args.server:
        server_ok = _reset_server(config, args.yes, args.device_id)
        if args.yes and not server_ok and not args.force_local:
            print("\n서버 초기화가 실패해 로컬 삭제를 중단했습니다. "
                  "서버가 옛 메타데이터를 들고 있으면 로컬만 지워도 다음 "
                  "동기화에서 되살아납니다. 그래도 지우려면 --force-local.")
            return 1

    _reset_local(config, args.yes)

    if args.reset_key:
        key_file = config.get("key_file")
        if key_file and os.path.exists(key_file):
            print(f"{'삭제' if args.yes else '삭제 예정'}: {key_file}")
            if args.yes:
                os.remove(key_file)

    if not args.yes:
        print("\n미리보기입니다. 실제로 지우려면 --yes를 붙여 다시 실행하세요.")
    else:
        print("\n초기화 완료. daemon을 다시 기동하면 빈 상태로 시작합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
