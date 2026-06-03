"""CLI 명령 구현.

각 명령은 (session, args) -> int(종료 코드) 시그니처를 따른다.
df/ls/status는 오프라인(로컬 코어)으로 동작하고, devices는 온라인 세션이 필요하다.
전송 계열(put/get) 등은 후속 Phase.
"""

from __future__ import annotations

import getpass
import os
import sys

from stardustlib.cli.format import echo, print_json, print_table
from stardustlib.exceptions import InsufficientStorageError


def _vpath(path: str | None) -> str:
    """CLI 입력 가상 경로를 정규화한다.

    선행 슬래시가 없으면 붙인다 → 사용자는 상대 경로(`foo/bar`)를 쓸 수 있고, 이는
    Git Bash의 MSYS 경로 변환(`/foo` → Windows 경로)을 회피한다. 백슬래시는
    슬래시로, 중복 슬래시는 단일로 정리한다. 빈 값/`.`은 루트(`/`).
    """
    if not path:
        return "/"
    s = path.replace("\\", "/").strip()
    if s in ("", ".", "./"):
        return "/"
    if not s.startswith("/"):
        s = "/" + s
    while "//" in s:
        s = s.replace("//", "/")
    if len(s) > 1:
        s = s.rstrip("/")
    return s


def _join(base: str, name: str) -> str:
    """디렉토리 경로와 엔트리 이름을 가상 경로로 결합한다."""
    return base.rstrip("/") + "/" + name


def _short(device_id: str | None) -> str:
    """device_id를 표시용으로 축약한다 (앞 8자)."""
    if not device_id:
        return "-"
    return device_id[:8]


def cmd_df(session, args) -> int:
    """총/사용/가용 용량을 출력한다 (로컬 소스 합산)."""
    total = session.jbod.get_total_space()
    available = session.jbod.get_available_space()
    used = total - available

    if args.json:
        print_json({"total": total, "used": used, "available": available})
    else:
        print_table(
            [[total, used, available]], ["total", "used", "available"]
        )
    return 0


def cmd_ls(session, args) -> int:
    """가상 경로의 디렉토리 목록을 출력한다.

    파일은 소유 device_id를 함께 표시한다(로컬 메타데이터 기준). 온라인 세션이면
    self device는 'this'로 표시한다.
    """
    base = _vpath(args.path)
    entries = session.jbod.list_directory(base)
    self_id = session.self_device_id

    def owner_of(name: str) -> str | None:
        meta = session.metadata.lookup(_join(base, name))
        return meta.device_id if meta is not None else None

    if args.json:
        out = []
        for e in entries:
            item = {
                "name": e.name,
                "is_directory": e.is_directory,
                "file_size": e.file_size,
                "modified_at": e.modified_at,
            }
            if not e.is_directory:
                item["device_id"] = owner_of(e.name)
            out.append(item)
        print_json(out)
        return 0

    rows = []
    for e in entries:
        if e.is_directory:
            owner = ""
        else:
            did = owner_of(e.name)
            owner = "this" if self_id and did == self_id else _short(did)
        rows.append(
            ["d" if e.is_directory else "-", e.file_size, owner, e.name]
        )
    print_table(rows, ["type", "size", "owner", "name"])
    return 0


def cmd_status(session, args) -> int:
    """동기화 상태를 출력한다 (보류 변경 수 등, 로컬 기준)."""
    pending = session.metadata.get_pending_files()
    total = len(session.jbod.list_directory("/"))

    if args.json:
        print_json(
            {
                "pending": len(pending),
                "root_entries": total,
                "self_device_id": session.self_device_id,
                "online": session.online,
            }
        )
    else:
        print_table(
            [[len(pending), total, session.online]],
            ["pending", "root_entries", "online"],
        )
    return 0


def cmd_devices(session, args) -> int:
    """내 계정에 등록된 device 목록과 online 여부를 출력한다 (온라인 필요)."""
    if not session.online or session.my_devices is None:
        # graceful skip 금지 — 규격 오류로 반환
        print_table([], ["id", "name", "online"])
        return 1

    self_id = session.self_device_id

    if args.json:
        print_json(
            [
                {
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "is_online": d.get("is_online"),
                    "self": d.get("id") == self_id,
                }
                for d in session.my_devices
            ]
        )
        return 0

    rows = [
        [
            _short(d.get("id")),
            d.get("name"),
            "online" if d.get("is_online") else "offline",
            "this" if d.get("id") == self_id else "",
        ]
        for d in session.my_devices
    ]
    print_table(rows, ["id", "name", "online", "self"])
    return 0


# --- 전송/쓰기 계열 (온라인 + 동기화) ---
#
# 종료 코드: 0 성공 / 3 없음·로컬 I/O 실패 / 4 원격 오프라인·도달 불가 / 5 용량 부족.
# "실패 시 graceful 건너뛰기" 금지 — 예외를 규격 종료 코드로 매핑한다.


def _err(message: str) -> None:
    """오류 메시지를 표준오류에 UTF-8로 출력한다."""
    sys.stderr.buffer.write((message + "\n").encode("utf-8"))


# --- 인증 계열 (login/logout) ---
#
# 세션 없이 config만으로 동작한다. login은 토큰이 없어도 실행되며, 토큰을 자격증명
# 저장소에 기록한다. 자격증명 수집 우선순위: 플래그 > 환경변수 > 대화형 입력.


def _server_url_of(config) -> str | None:
    server = config.get("server")
    return server.get("url") if isinstance(server, dict) else None


async def cmd_login(args) -> int:
    """email/password로 로그인하여 토큰을 자격증명 저장소에 저장한다."""
    from stardustlib.auth_client import AuthClient
    from stardustlib.config_loader import ConfigLoader
    from stardustlib.credential_store import CredentialStore
    from stardustlib.exceptions import AuthenticationError

    config = ConfigLoader(args.config).load()
    server_url = _server_url_of(config)
    if not server_url:
        _err("오류: server.url이 설정되어 있지 않습니다(오프라인 전용 설정).")
        return 2

    email = (args.email or os.environ.get("STARDUST_EMAIL")
             or input("Email: ").strip())
    password = (args.password or os.environ.get("STARDUST_PASSWORD")
                or getpass.getpass("Password: "))

    store = CredentialStore(config["metadata_db"])
    auth = AuthClient(server_url, credential_store=store)
    try:
        await auth.login(email, password)
    except AuthenticationError as e:
        _err(f"오류: 로그인 실패: {e}")
        await auth.close()
        return 1

    # 마스터키 백업 암호(선택): 플래그 > 환경변수. 없으면 보관하지 않음.
    key_password = args.key_password or os.environ.get("STARDUST_KEY_PASSWORD")
    if key_password:
        auth.set_key_password(key_password)

    await auth.close()
    echo(f"로그인 성공: {email}")
    echo(f"자격증명 저장: {store.path}")
    return 0


async def cmd_logout(args) -> int:
    """서버에 토큰 취소를 요청(best-effort)하고 자격증명 저장소를 삭제한다."""
    from stardustlib.auth_client import AuthClient
    from stardustlib.config_loader import ConfigLoader
    from stardustlib.credential_store import CredentialStore

    config = ConfigLoader(args.config).load()
    store = CredentialStore(config["metadata_db"])
    if not store.exists():
        echo("로그아웃: 저장된 자격증명이 없습니다.")
        return 0

    server_url = _server_url_of(config) or ""
    auth = AuthClient(server_url, credential_store=store)
    auth.load_from_store()
    if server_url:
        await auth.logout()
    await auth.close()
    store.clear()
    echo("로그아웃 완료: 자격증명을 삭제했습니다.")
    return 0


def _note_propagation(session) -> str:
    """전파 여부 안내 문구. 오프라인이면 daemon 동기화 대기임을 알린다."""
    if session.sync_client is None:
        return " (로컬 저장, 서버 미전파 — daemon 동기화 대기)"
    return ""


async def cmd_put(session, args) -> int:
    """로컬 파일을 가상 경로로 업로드한다 (암호화·소스 저장·메타데이터·전파)."""
    local = args.local
    remote = _vpath(args.remote) if args.remote else "/" + os.path.basename(local)
    try:
        with open(local, "rb") as f:
            data = f.read()
    except OSError as e:
        _err(f"오류: 로컬 파일을 읽을 수 없습니다: {e}")
        return 3

    try:
        session.jbod.write_file(remote, data)
    except InsufficientStorageError as e:
        _err(f"오류: 저장 공간 부족: {e}")
        return 5
    except OSError as e:
        _err(f"오류: 업로드 실패: {e}")
        return 4

    await session.upload_if_online()
    echo(f"업로드 완료: {local} -> {remote} ({len(data)} bytes)"
          f"{_note_propagation(session)}")
    return 0


async def cmd_get(session, args) -> int:
    """가상 경로의 파일을 로컬로 다운로드한다 (소유 device에서 fetch·복호화)."""
    remote = _vpath(args.remote)
    local = args.local or os.path.basename(remote.rstrip("/"))
    try:
        data = session.jbod.read_file(remote)
    except FileNotFoundError:
        _err(f"오류: 파일을 찾을 수 없습니다: {remote}")
        return 3
    except OSError as e:
        _err(f"오류: 다운로드 실패 (원격 오프라인/도달 불가 가능): {e}")
        return 4

    try:
        with open(local, "wb") as f:
            f.write(data)
    except OSError as e:
        _err(f"오류: 로컬 저장 실패: {e}")
        return 3

    echo(f"다운로드 완료: {remote} -> {local} ({len(data)} bytes)")
    return 0


async def cmd_rm(session, args) -> int:
    """파일 또는 디렉토리(-r)를 삭제한다 (tombstone·전파)."""
    path = _vpath(args.path)
    try:
        if args.recursive:
            session.jbod.delete_directory(path)
        else:
            session.jbod.delete_file(path)
    except FileNotFoundError:
        _err(f"오류: 없는 경로: {path}")
        return 3
    except OSError as e:
        _err(f"오류: 삭제 실패: {e}")
        return 4

    await session.upload_if_online()
    echo(f"삭제 완료: {path}{_note_propagation(session)}")
    return 0


async def cmd_mkdir(session, args) -> int:
    """디렉토리를 생성한다 (전파)."""
    path = _vpath(args.path)
    session.jbod.create_directory(path)
    await session.upload_if_online()
    echo(f"디렉토리 생성: {path}{_note_propagation(session)}")
    return 0


async def cmd_mv(session, args) -> int:
    """파일/디렉토리를 이동(이름변경)한다 (전파)."""
    src, dst = _vpath(args.src), _vpath(args.dst)
    try:
        if session.jbod.file_exists(src):
            session.jbod.move_file(src, dst)
        else:
            session.jbod.move_directory(src, dst)
    except FileNotFoundError:
        _err(f"오류: 없는 경로: {src}")
        return 3
    except OSError as e:
        _err(f"오류: 이동 실패: {e}")
        return 4

    await session.upload_if_online()
    echo(f"이동 완료: {src} -> {dst}{_note_propagation(session)}")
    return 0


async def cmd_cp(session, args) -> int:
    """파일을 복사한다 (전파)."""
    src, dst = _vpath(args.src), _vpath(args.dst)
    try:
        session.jbod.copy_file(src, dst)
    except FileNotFoundError:
        _err(f"오류: 없는 파일: {src}")
        return 3
    except OSError as e:
        _err(f"오류: 복사 실패: {e}")
        return 4

    await session.upload_if_online()
    echo(f"복사 완료: {src} -> {dst}{_note_propagation(session)}")
    return 0


# --- 리플리케이션 계열 (온라인 + 동기화) ---
#
# backup: 암호화 청크를 ≥3 홀더에 복제. restore: 복제본에서 복구.
# 종료 코드: 0 성공(replicated/복구) / 3 없음 / 4 복제 미완료·복구 실패.


async def cmd_backup(session, args) -> int:
    """파일을 암호화 청크로 분할해 ≥3 홀더에 복제한다."""
    from stardustlib.replication_manager import ReplicationError

    path = _vpath(args.path)
    mgr = session.make_replication_manager()
    try:
        result = mgr.replicate(path)
    except FileNotFoundError:
        _err(f"오류: 없는 경로: {path}")
        return 3
    except ReplicationError as e:
        _err(f"오류: 복제 실패: {e}")
        return 4
    finally:
        mgr.close()

    if result.status == "replicated":
        echo(
            f"복제 완료: {path} — 청크 {result.chunk_count}개, "
            f"각 ≥{result.min_replicas} 홀더"
        )
        return 0
    echo(
        f"복제 미완료(pending): {path} — 청크별 복제수 "
        f"{result.replicas_per_chunk} (목표 {result.min_replicas}). "
        "홀더 확보 후 다시 시도하세요."
    )
    return 4


async def cmd_restore(session, args) -> int:
    """복제본에서 파일을 복구해 로컬에 기록한다."""
    from stardustlib.replication_manager import RecoveryError, ReplicationError

    path = _vpath(args.path)
    mgr = session.make_replication_manager()
    try:
        nbytes = mgr.recover(path)
    except RecoveryError as e:
        _err(f"오류: 복구 실패: {e} (누락 청크 {len(e.missing_chunks)}개)")
        return 4
    except ReplicationError as e:
        _err(f"오류: 복구 실패: {e}")
        return 4
    finally:
        mgr.close()

    await session.upload_if_online()
    echo(f"복구 완료: {path} ({nbytes} bytes){_note_propagation(session)}")
    return 0
