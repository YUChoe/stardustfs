#!/usr/bin/env python3
"""StardustFS - 암호화 분산 파일시스템 (CLI + 상주 daemon)."""

import argparse
import asyncio
import logging
import os
import sys

# 개발/디버그 환경에서 .env 파일 자동 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _setup_logging() -> None:
    """표준 로깅 포맷을 설정하고 외부 라이브러리 로그를 억제한다."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """메인 엔트리포인트.

    서브커맨드 없음 또는 `daemon`이면 상주 데몬을 시작한다(기존 동작 호환).
    단발 CLI 명령(ls/df/...)이면 해당 명령을 실행한다. `--config`는 서브커맨드
    앞에 둔다 (예: `stardustfs.py --config dev-config.json ls /`).
    """
    # Windows 콘솔(cp1252 등)에서 한글 출력 시 UnicodeEncodeError 방지.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    from stardustlib.cli.dispatcher import (
        add_subcommands,
        is_cli_command,
        run_cli,
    )

    parser = argparse.ArgumentParser(
        description="StardustFS — 암호화 분산 파일시스템"
    )
    parser.add_argument("--config", "-c", help="JSON 설정 파일 경로")

    # 공통 옵션: 서브커맨드 뒤에서도 --config 를 받도록 한다.
    # default=SUPPRESS로 전역 --config 값을 덮어쓰지 않는다.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", "-c", default=argparse.SUPPRESS, help="JSON 설정 파일 경로"
    )

    subparsers = parser.add_subparsers(dest="command")
    p_daemon = subparsers.add_parser(
        "daemon", parents=[common], help="상주 데몬 (start/status/stop)"
    )
    p_daemon.add_argument(
        "action", nargs="?", choices=["start", "status", "stop"],
        default="start", help="동작 (기본 start)",
    )
    subparsers.add_parser("gui", parents=[common], help="데스크톱 GUI")
    add_subcommands(subparsers, common)

    args = parser.parse_args()

    _setup_logging()

    # 단발 CLI 명령
    if is_cli_command(args.command):
        sys.exit(run_cli(args))

    # 데스크톱 GUI (config 미지정 시 GUI에서 선택)
    if args.command == "gui":
        from stardustlib.gui.app import run_gui

        sys.exit(run_gui(getattr(args, "config", None)))

    # 인자 없이 실행(예: exe 더블클릭) → GUI를 연다(설정은 GUI에서 선택).
    if args.command is None and not args.config:
        from stardustlib.gui.app import run_gui

        sys.exit(run_gui(None))

    # daemon (서브커맨드 없음 또는 'daemon [action]')
    if not args.config:
        parser.error("--config 가 필요합니다 (daemon 모드)")
    action = getattr(args, "action", "start")
    if action == "status":
        sys.exit(_daemon_status(args.config))
    if action == "stop":
        sys.exit(_daemon_stop(args.config))
    _run_daemon(args.config)


def _run_daemon(config_path: str) -> None:
    """설정을 로드하고 버전에 따라 상주 데몬을 시작한다."""
    from stardustlib import daemon
    from stardustlib.config_loader import ConfigLoader

    try:
        config = ConfigLoader(config_path).load()
    except (FileNotFoundError, Exception) as e:
        logging.error("설정 로드 실패: %s", e)
        sys.exit(1)

    version = config.get("version")  # type: ignore[attr-defined]

    if version != 2:
        logging.error(
            "지원하지 않는 설정 버전입니다: %s (v2 필요). "
            "WebDAV 기반 v1 흐름은 제거되었습니다.", version,
        )
        sys.exit(1)

    # 중복 실행 방지 — 제어 파일을 기동 즉시 잡는다. startup(서버 등록 재시도 등)이
    # 길어지는 동안에도 heartbeat가 유지되므로, 감시자(GUI)가 'daemon 없음'으로 보고
    # 새 인스턴스를 띄우는 일이 없다.
    metadata_db = config.get("metadata_db")  # type: ignore[attr-defined]
    if metadata_db and not daemon.claim(metadata_db):
        pid = daemon.read_status(metadata_db).get("pid")
        logging.error("daemon이 이미 실행 중입니다 (pid=%s)", pid)
        sys.exit(1)

    try:
        asyncio.run(startup_v2(config, config_path))
    except BaseException:
        # startup 실패 시 제어 파일을 반납한다 — 남겨 두면 다음 기동이 막힌다.
        if metadata_db:
            daemon.release_claim(metadata_db)
        raise


def _daemon_status(config_path: str) -> int:
    """실행 중인 daemon 상태를 출력한다."""
    from stardustlib import daemon
    from stardustlib.config_loader import ConfigLoader

    config = ConfigLoader(config_path).load()
    status = daemon.read_status(config["metadata_db"])
    if status.get("running"):
        msg = (f"daemon 실행 중: pid={status['pid']} "
               f"(heartbeat {status['heartbeat_age']:.0f}s 전)\n")
        sys.stdout.buffer.write(msg.encode("utf-8"))
        return 0
    if status.get("stale"):
        sys.stdout.buffer.write(
            "daemon 미실행 (stale 제어 파일: 비정상 종료 가능)\n".encode("utf-8")
        )
        return 1
    sys.stdout.buffer.write("daemon 미실행\n".encode("utf-8"))
    return 1


def _daemon_stop(config_path: str) -> int:
    """실행 중인 daemon에 정지를 요청한다."""
    from stardustlib import daemon
    from stardustlib.config_loader import ConfigLoader

    config = ConfigLoader(config_path).load()
    result = daemon.request_stop(config["metadata_db"])
    if result.get("stopped"):
        sys.stdout.buffer.write("daemon 정지됨\n".encode("utf-8"))
        return 0
    if result.get("reason") == "not_running":
        sys.stdout.buffer.write("daemon 미실행\n".encode("utf-8"))
        return 1
    sys.stdout.buffer.write(
        "daemon 정지 요청 전송, 시간 내 종료 확인 실패\n".encode("utf-8")
    )
    return 1


def _build_parity_store(
    config: dict, storage_pool, metadata_store, quota_bytes: int | None = None
):
    """호스트 역할 패리티 스토어를 생성한다(replication.enabled 일 때만, 기본 활성).

    보관 청크는 내 청크와 같은 스토리지 소스에 놓이고 인덱스는 메타데이터 DB의
    hosted_chunks다(별도 `.parity/` 디렉토리는 폐기 — 용량 집계를 하나로 모은다).
    최대 용량은 서버가 정한 호스팅 상한(quota_bytes)이며, None(정책 미수신)이면 0으로
    두어 타 사용자 청크를 받지 않는다. replication.enabled=false면 None.

    구 버전이 남긴 `{metadata_db}.parity/` 청크는 생성 시 한 번 소스로 이관한다.
    """
    repl = config.get("replication", {})
    if not repl.get("enabled", True):  # 기본 활성
        return None
    from stardustlib.parity_store import ParityStore

    logger = logging.getLogger(__name__)
    store = ParityStore(storage_pool, metadata_store, quota_bytes or 0)
    legacy_dir = config["metadata_db"] + ".parity"
    if os.path.isdir(legacy_dir):
        # 이관은 쿼터와 무관하게 이미 맡은 청크를 지키는 일이므로 상한을 잠시 푼다.
        store.set_max_bytes(None)
        try:
            store.migrate_legacy_dir(legacy_dir)
        except Exception as e:  # noqa: BLE001 — 이관 실패가 기동을 막지 않는다
            logger.warning("레거시 보관 청크 이관 실패(다음 기동 재시도): %s", e)
        store.set_max_bytes(quota_bytes or 0)
    return store


async def startup_v2(config: dict, config_path: str) -> None:
    """v2 설정 기반 MVP2 초기화 흐름.

    순서: (1) 설정 로드 → (2) 인증 → (3) key_file 복원 (필요 시)
    → (4) 로컬 스토리지 초기화 → (5) 디바이스 등록
    → (6) 메타데이터 동기화 → (7) P2P 서버 시작 → (8) 상주 루프(daemon.serve)

    인증 실패 시 오프라인 모드 (key_file이 로컬에 있어야 동작 가능).
    메타데이터 동기화 실패 시 로컬 DB만 사용.
    server 섹션 없으면 오프라인 전용 모드.
    설정/스토리지 초기화 실패 시 종료.
    """
    from stardustlib.config_loader import ConfigLoader
    from stardustlib.exceptions import AuthenticationError, KeyMismatchError

    logger = logging.getLogger(__name__)

    # (1) 설정 검증 (이미 로드됨)
    loader = ConfigLoader(config_path)
    errors = loader.validate(config)
    if errors:
        for err in errors:
            logger.error(err)
        sys.exit(1)

    # server 섹션 확인 — 없거나 url이 None이면 오프라인 전용 모드
    server_config = config.get("server")  # type: ignore[attr-defined]
    server_url = None
    if isinstance(server_config, dict):
        server_url = server_config.get("url")

    if not server_url:
        # 오프라인 전용 모드: key_file이 로컬에 있어야 함
        logger.info("server.url 미설정, 오프라인 전용 모드로 시작")
        try:
            storage_pool, metadata_store, encryption_engine, db_key = (
                _build_core(config)
            )
        except SystemExit:
            raise
        except Exception as e:
            logger.error("로컬 스토리지 초기화 실패: %s", e)
            sys.exit(1)

        async def _cleanup() -> None:
            storage_pool.close_local_sources()
            metadata_store.close()

        from stardustlib import daemon
        await daemon.serve(config["metadata_db"], _cleanup)
        return

    # (2) 인증 — 저장된 토큰 사용(비밀번호 미사용). 토큰 없거나 무효면 오프라인 모드.
    from stardustlib.auth_client import AuthClient
    from stardustlib.credential_store import CredentialStore

    credential_store = CredentialStore(config["metadata_db"])  # type: ignore[index]
    auth_client = AuthClient(server_url, credential_store=credential_store)
    offline_mode = False

    if not auth_client.load_from_store():
        logger.warning(
            "저장된 자격증명이 없습니다. 'stardustfs login' 후 daemon을 "
            "실행하세요. 오프라인 모드로 시작합니다."
        )
        offline_mode = True
    else:
        try:
            await auth_client.get_valid_token()
        except AuthenticationError as e:
            logger.warning(
                "토큰 만료/무효(%s), 재로그인 필요. 오프라인 모드로 전환.", e
            )
            offline_mode = True
        except Exception as e:  # noqa: BLE001 — 네트워크 등 → 오프라인
            logger.warning("토큰 확인 중 예외(%s), 오프라인 모드로 전환.", e)
            offline_mode = True

    # (3) key_file 복원 (필요 시)
    key_file_path = config.get("key_file")  # type: ignore[attr-defined]
    if not offline_mode and key_file_path:
        from pathlib import Path
        if not Path(key_file_path).exists():
            await _restore_key_from_server(auth_client, key_file_path, logger)

    # (3-b) 최초 디바이스: key_file 생성 직후 서버에 백업 업로드
    # (로컬 스토리지 초기화 후 수행 — 아래에서 처리)

    # (4) 로컬 스토리지 초기화
    try:
        storage_pool, metadata_store, encryption_engine, db_key = (
            _build_core(config)
        )
    except SystemExit:
        raise
    except Exception as e:
        logger.error("로컬 스토리지 초기화 실패: %s", e)
        sys.exit(1)

    if offline_mode:
        # 오프라인 모드: (4)-(6) 건너뛰기, WebDAV 시작 + 백그라운드 복구
        from stardustlib.conflict_resolver import ConflictResolver
        from stardustlib.device_manager import DeviceManager
        from stardustlib.online_recovery import OnlineRecoveryManager
        from stardustlib.sync_client import SyncClient

        device_name = server_config.get("device_name", "unknown")
        p2p_config = config.get("p2p", {})  # type: ignore[attr-defined]
        p2p_port = p2p_config.get("port", 9090)
        sync_config = config.get("sync", {})  # type: ignore[attr-defined]
        interval_seconds = sync_config.get("interval_seconds", 30)

        device_mgr = DeviceManager(
            auth_client, server_url, device_name, p2p_port
        )
        conflict_resolver = ConflictResolver(metadata_store, device_name)
        sync_client = SyncClient(
            auth_client, server_url, metadata_store,
            conflict_resolver, interval_seconds,
            encryption_key=db_key,
            storage_pool=storage_pool,
        )

        # P2P 서버 인스턴스 (복구 시 시작됨)
        from stardustlib.p2p_server import P2PServer

        p2p_server = None
        # 오프라인 모드에서는 서버 정책을 받을 수 없으므로 로컬 설정만 따른다
        # (기본 활성). 온라인 복구 후 재시작 시 정책이 적용된다.
        p2p_enabled = p2p_config.get("enabled", True)
        if p2p_enabled:
            p2p_server = P2PServer(
                storage_pool, auth_client, p2p_port, server_url,
                parity_store=_build_parity_store(
                    config, storage_pool, metadata_store
                ),
            )

        recovery_mgr = OnlineRecoveryManager(
            auth_client, device_mgr, sync_client, p2p_server,
            check_interval=60,
        )
        await recovery_mgr.start()

        async def _cleanup() -> None:
            await recovery_mgr.stop()
            await sync_client.stop()
            await device_mgr.stop()
            if p2p_server is not None:
                await p2p_server.stop()
            await auth_client.close()
            storage_pool.close_local_sources()
            metadata_store.close()

        from stardustlib import daemon
        await daemon.serve(config["metadata_db"], _cleanup)
        return

    # (4-a) 최초 디바이스: key 백업 업로드 (서버에 백업이 없으면)
    if key_file_path:
        await _backup_key_to_server(auth_client, key_file_path, logger)

    # (5) 디바이스 등록
    from stardustlib.device_manager import DeviceManager
    from stardustlib.exceptions import DeviceRegistrationError

    device_name = server_config.get("device_name", "unknown")
    p2p_config = config.get("p2p", {})  # type: ignore[attr-defined]
    p2p_port = p2p_config.get("port", 9090)

    device_mgr = DeviceManager(auth_client, server_url, device_name, p2p_port)

    try:
        await device_mgr.register()
    except DeviceRegistrationError as e:
        logger.warning("디바이스 등록 실패, 오프라인 모드로 전환: %s", e)

        async def _cleanup() -> None:
            await auth_client.close()
            storage_pool.close_local_sources()
            metadata_store.close()

        from stardustlib import daemon
        await daemon.serve(config["metadata_db"], _cleanup)
        return

    # 등록된 device_id를 스토리지 풀에 주입 (파일 변경 추적용)
    storage_pool.device_id = device_mgr.device_id

    # (5-a) 리플리케이션 정책 다운로드(시작 1회). 호스팅 상한은 서버가 정한다
    # (프로비저닝) — 제공 용량의 비율을 예약하던 방식은 폐기됐다.
    from stardustlib.replication_manager import DEFAULT_TARGET_COPIES

    repl_config = config.get("replication", {})  # type: ignore[attr-defined]
    repl_enabled = repl_config.get("enabled", True)
    repl_target = int(
        repl_config.get("target_copies", DEFAULT_TARGET_COPIES)
    )
    # 서버가 알려준 호스팅 상한(bytes). None이면 아직 받지 못한 상태 → 호스팅 안 함.
    repl_quota: int | None = None
    # 서버 프로비저닝 스위치(기본 허용). 구버전 서버/도달 불가 시 로컬 설정만 적용.
    policy_p2p_allowed = True
    policy_hosting_allowed = True

    from stardustlib.replication_hosting import fetch_policy

    # 정책은 P2P 게이팅에도 쓰이므로 replication.enabled와 무관하게 조회한다.
    policy = await fetch_policy(
        auth_client, server_url, device_id=device_mgr.device_id
    )
    if policy:
        policy_p2p_allowed = policy["p2p_enabled"]
        policy_hosting_allowed = policy["hosting_enabled"]
        if repl_enabled:
            repl_target = policy["target_copies"]
            repl_quota = policy["hosting_quota_bytes"]
        logger.info(
            "서버 정책 수신: 목표 카피 %d, 호스팅 상한 %s, p2p=%s, 호스팅=%s",
            policy["target_copies"],
            "미지정" if repl_quota is None else f"{repl_quota} bytes",
            policy_p2p_allowed, policy_hosting_allowed,
        )
    elif repl_enabled:
        logger.warning(
            "정책 조회 실패 — 호스팅 상한을 받지 못해 타 사용자 청크를 "
            "보관하지 않습니다(내 백업은 그대로 동작)"
        )

    # 로컬 소스 인벤토리를 서버에 신고(리모트 디바이스 GUI의 스토리지 목록용).
    # 실패해도 시작을 막지 않는다.
    if device_mgr.device_id:
        from stardustlib.device_manager import build_local_source_inventory

        inventory = build_local_source_inventory(storage_pool)
        if await device_mgr.report_sources(inventory):
            logger.info("로컬 소스 인벤토리 신고: %d개 소스", len(inventory))

    # 내 계정에 등록된 디바이스 목록 조회 (remote 소스 설정 참고용)
    my_devices = await device_mgr.list_devices()
    if my_devices:
        logger.info("내 계정에 등록된 디바이스 (%d개):", len(my_devices))
        for d in my_devices:
            mark = " (이 디바이스)" if d.get("id") == device_mgr.device_id else ""
            logger.info(
                "  - id=%s name=%s online=%s%s",
                d.get("id"), d.get("name"), d.get("is_online"), mark,
            )

    # (5-b) remote 소스 마운트 (인증 완료 후) — 같은 유저의 다른 디바이스 스토리지 접근
    _mount_remote_sources(
        config, storage_pool, auth_client, server_url,
        my_devices=my_devices, self_device_id=device_mgr.device_id,
    )

    # (5) 메타데이터 동기화
    from stardustlib.conflict_resolver import ConflictResolver
    from stardustlib.sync_client import SyncClient

    sync_config = config.get("sync", {})  # type: ignore[attr-defined]
    interval_seconds = sync_config.get("interval_seconds", 30)

    conflict_resolver = ConflictResolver(metadata_store, device_name)
    sync_client = SyncClient(
        auth_client, server_url, metadata_store,
        conflict_resolver, interval_seconds,
        encryption_key=db_key,
        storage_pool=storage_pool,
    )

    try:
        # 장기 오프라인(stale) 디바이스면 서버 정본으로 재조정.
        # 재조정 시 내부에서 initial_sync까지 수행하므로 중복 호출하지 않는다.
        reconciled = await sync_client.reconcile_if_stale()
        if not reconciled:
            await sync_client.initial_sync()
    except KeyMismatchError as e:
        logger.warning("key 불일치 감지: %s", e)
        logger.info("서버에서 올바른 key를 복원합니다...")
        # key 복원 시도
        try:
            await _restore_key_from_server(auth_client, key_file_path, logger)
            # key_file 교체 후 로컬 스토리지 재초기화
            logger.info("key 복원 완료, 로컬 스토리지 재초기화...")
            storage_pool.close_local_sources()
            metadata_store.close()
            storage_pool, metadata_store, encryption_engine, db_key = (
                _build_core(config)
            )
            # device_id, remote 소스 재주입 (storage_pool가 교체되었으므로)
            storage_pool.device_id = device_mgr.device_id
            _mount_remote_sources(
                config, storage_pool, auth_client, server_url,
                my_devices=my_devices, self_device_id=device_mgr.device_id,
            )
            # SyncClient 재생성
            conflict_resolver = ConflictResolver(metadata_store, device_name)
            sync_client = SyncClient(
                auth_client, server_url, metadata_store,
                conflict_resolver, interval_seconds,
                encryption_key=db_key,
                storage_pool=storage_pool,
            )
            await sync_client.initial_sync()
            logger.info("key 복원 후 메타데이터 동기화 성공")
        except Exception as restore_err:
            logger.error(
                "key 복원 실패, 로컬 DB만 사용: %s", restore_err
            )
    except Exception as e:
        logger.warning(
            "메타데이터 동기화 실패, 로컬 DB만 사용: %s", e
        )

    # (6) P2P 서버 시작
    from stardustlib.p2p_server import P2PServer

    p2p_server = None
    relay_worker = None
    parity_store = None
    # P2P는 기본 활성. 서버 정책이 금지하면(엔터프라이즈 격리 등) 끈다.
    p2p_enabled = p2p_config.get("enabled", True) and policy_p2p_allowed
    if p2p_config.get("enabled", True) and not policy_p2p_allowed:
        logger.info("서버 정책으로 P2P 비활성 — 피어 서빙을 시작하지 않습니다")

    if p2p_enabled:
        # 타 사용자 청크 보관(호스팅)은 정책으로 별도 제어된다. 금지 시 패리티
        # 보관소를 만들지 않아 replica_* op가 503으로 규격 거부된다.
        if policy_hosting_allowed:
            parity_store = _build_parity_store(
                config, storage_pool, metadata_store, repl_quota
            )
        else:
            logger.info("서버 정책으로 호스팅 비활성 — 타 사용자 청크를 보관하지 않습니다")
        p2p_server = P2PServer(
            storage_pool, auth_client, p2p_port, server_url,
            parity_store=parity_store,
        )
        await p2p_server.start()

        # 릴레이 워커 시작 (직접 연결 불가 환경의 fallback 수신).
        # device_id가 확보된 경우에만 시작한다.
        device_id = device_mgr.device_id
        if p2p_config.get("relay_enabled", True) and device_id:
            from stardustlib.relay_worker import RelayWorker

            relay_worker = RelayWorker(
                p2p_server, auth_client, server_url, device_id
            )
            await relay_worker.start()

    # (6-a) 홀펀칭 직접 전송 서비스 — 공유 IO 루프에서 랑데부 등록 + 펀치 + rudp.
    # 직접 UDP(홀펀칭)를 릴레이보다 우선하는 전송 경로. 시작 실패는 비치명(릴레이 fallback).
    holepunch_service = None
    if p2p_enabled and device_mgr.device_id:
        try:
            from urllib.parse import urlparse

            from stardustlib.holepunch_service import HolePunchService
            from stardustlib.remote_source import _EventLoopThread

            rv_host = urlparse(server_url).hostname
            rv_port = int(p2p_config.get("rendezvous_port", 9091))
            if rv_host:
                holepunch_service = HolePunchService(
                    rv_host, rv_port, auth_client.get_valid_token,
                    device_mgr.device_id, p2p_server.dispatch_async,
                )
                _EventLoopThread.get_instance().run_coroutine(
                    holepunch_service.start()
                )
                logger.info(
                    "홀펀칭 서비스 시작 (랑데부 %s:%d, reflexive=%s)",
                    rv_host, rv_port, holepunch_service.reflexive,
                )
                # 마운트된 원격 소스가 직접 TCP 실패 시 홀펀칭 UDP를 쓰도록 주입.
                for _src in storage_pool.sources:
                    if getattr(_src, "is_remote", False) and hasattr(
                        _src, "set_udp_transport"
                    ):
                        _src.set_udp_transport(
                            lambda did, op, pl:
                            holepunch_service.send_op(did, op, pl)
                        )
        except Exception as e:  # noqa: BLE001 — 비치명, 릴레이로 fallback
            logger.warning("홀펀칭 서비스 시작 실패(릴레이 fallback): %s", e)
            holepunch_service = None

    await device_mgr.start_heartbeat()
    # 소스 인벤토리 주기 재신고(리모트 GUI 용량/사용량 최신화). 시작 1회 신고 외에
    # 용량 변동을 반영한다. device_id 없으면 no-op.
    if device_mgr.device_id:
        from stardustlib.device_manager import build_local_source_inventory

        await device_mgr.start_source_report(
            lambda: build_local_source_inventory(storage_pool),
            interval=config.get("p2p", {}).get(
                "source_report_interval_seconds", 60
            ),
        )
    await sync_client.start_periodic_sync()

    # (6-b) 리플리케이션 스케줄러 (자동 백업/heal/정책 갱신) — 기본 활성
    repl_scheduler = None
    repl_progress = None
    if repl_enabled:
        from stardustlib.replication_hosting import fetch_policy
        from stardustlib.replication_manager import ReplicationManager
        from stardustlib.replication_progress import ProgressTracker
        from stardustlib.replication_scheduler import ReplicationScheduler

        # 진행 상태는 제어 채널(/ctl/progress)로 GUI에 노출된다.
        repl_progress = ProgressTracker()
        repl_mgr = ReplicationManager(
            auth_client, server_url, metadata_store, storage_pool,
            target_copies=repl_target, progress=repl_progress,
        )

        # 서버가 알려준 호스팅 상한을 기억한다. 정책 조회가 실패하면 직전 값을
        # 유지해, 일시적 네트워크 문제로 보관을 멈추지 않는다.
        hosting_quota = {"bytes": repl_quota}

        async def _report_hosting_usage() -> None:
            """이 기기의 실제 호스팅 사용량을 서버에 보고한다(회계 정렬).

            제공 용량 신고는 폐기됐다 — 상한은 서버가 정하고 클라이언트는 사용량만
            보고한다. 실패는 비치명(다음 정책 주기에 재시도).
            """
            if parity_store is None or not device_mgr.device_id:
                return
            from stardustlib.replication_hosting import report_usage

            await report_usage(
                auth_client, server_url, device_mgr.device_id,
                parity_store.used_bytes(), storage_pool.get_total_space(),
            )

        async def _apply_policy(policy: dict) -> None:
            """주기적으로 내려받은 정책을 매니저/패리티에 반영한다.

            호스팅 상한은 서버가 정한다(프로비저닝). 금지로 바뀌거나 상한이 0이면
            신규 청크 수용을 멈춘다(이미 보관 중인 청크는 소유자가 회수할 수 있도록
            유지). 상한 필드가 없으면(구버전 서버) 직전 값을 그대로 쓴다.
            """
            repl_mgr.set_target_copies(policy["target_copies"])
            if not policy.get("hosting_enabled", True):
                if parity_store is not None:
                    parity_store.set_max_bytes(0)
                await _report_hosting_usage()
                return
            quota = policy.get("hosting_quota_bytes")
            if quota is not None:
                hosting_quota["bytes"] = int(quota)
            if parity_store is not None:
                parity_store.set_max_bytes(hosting_quota["bytes"] or 0)
            await _report_hosting_usage()

        repl_scheduler = ReplicationScheduler(
            repl_mgr, metadata_store, device_mgr.device_id,
            backup_interval=repl_config.get("backup_interval_seconds", 300),
            heal_interval=repl_config.get("heal_interval_seconds", 3600),
            heal_grace_seconds=repl_config.get("heal_grace_seconds", 86400),
            max_files_per_cycle=repl_config.get("max_files_per_cycle", 20),
            backup_concurrency=repl_config.get("backup_concurrency", 4),
            # device_id를 넘겨 이 기기의 호스팅 상한을 함께 받는다.
            policy_fetcher=lambda: fetch_policy(
                auth_client, server_url, device_id=device_mgr.device_id
            ),
            on_policy=_apply_policy,
            policy_interval=repl_config.get("policy_interval_seconds", 3600),
        )
        await repl_scheduler.start()
        # 다른 device가 보낸 백업 위임(/p2p/backup_announce)을 스케줄러에 연결한다.
        # p2p 핸들러는 홀펀칭 IO 루프에서 실행될 수 있고 스케줄러의 announce는
        # asyncio.Event를 쓰므로, 데몬 루프로 넘겨 호출한다.
        if p2p_server is not None:
            _daemon_loop = asyncio.get_running_loop()

            def _announce_from_peer(vpath: str) -> None:
                _daemon_loop.call_soon_threadsafe(
                    repl_scheduler.announce, vpath
                )

            p2p_server.set_backup_announcer(_announce_from_peer)
        # 축출 파일 읽기 시 복제 홀더에서 복구해 로컬 재구체화하는 콜백 주입.
        storage_pool._recover_fn = repl_mgr.recover
        # 홀더 전송을 직접 TCP→직접 UDP(홀펀칭)→릴레이 순으로. 같은 IO 루프에서 await.
        if holepunch_service is not None:
            repl_mgr.set_udp_transport(
                lambda did, op, pl: holepunch_service.send_op(did, op, pl)
            )

    # (6-c) 콜드 축출: 로컬 공간 부족 시 복제본이 충분한 파일의 로컬 원본 비움.
    # 기본 비활성(eviction.enabled). 안전: 삭제 직전 온라인 복제본 수를 실측.
    evict_task = None
    evict_config = config.get("eviction", {})  # type: ignore[attr-defined]
    if repl_enabled and evict_config.get("enabled", False):
        evict_task = asyncio.create_task(
            _eviction_loop(storage_pool, repl_mgr, evict_config)
        )

    # (6-d) 로컬 전송 위임 채널 — GUI/CLI가 put/get을 데몬에 위임(홀펀칭 활용).
    control_server = None
    try:
        from stardustlib.daemon_control import DaemonControlServer

        control_server = DaemonControlServer(
            storage_pool, sync_client, config["metadata_db"],
            repl_scheduler=repl_scheduler,
            repl_progress=repl_progress,
        )
        await control_server.start()
    except Exception as e:  # noqa: BLE001 — 비치명(위임 없으면 GUI가 직접 수행)
        logger.warning("데몬 제어 채널 시작 실패: %s", e)
        control_server = None

    # (7) 상주 루프 (정지 신호까지 — Ctrl+C 또는 'daemon stop')
    async def _cleanup() -> None:
        if control_server is not None:
            try:
                await control_server.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("제어 채널 종료 중 예외: %s", e)
        if evict_task is not None:
            evict_task.cancel()
            try:
                await evict_task
            except asyncio.CancelledError:
                pass
        if repl_scheduler is not None:
            await repl_scheduler.stop()
        if holepunch_service is not None:
            from stardustlib.remote_source import _EventLoopThread
            try:
                _EventLoopThread.get_instance().run_coroutine(
                    holepunch_service.stop()
                )
            except Exception as e:  # noqa: BLE001 — 종료 경로
                logger.debug("홀펀칭 서비스 종료 중 예외: %s", e)
        await sync_client.stop()
        await device_mgr.stop()
        if relay_worker is not None:
            await relay_worker.stop()
        if p2p_server is not None:
            await p2p_server.stop()
        await auth_client.close()
        storage_pool.close_local_sources()
        metadata_store.close()

    async def _on_reload() -> None:
        """config의 로컬 소스를 다시 읽어 storage_pool에 remount하고 즉시 재신고한다(무중단)."""
        from stardustlib.config_loader import ConfigLoader

        fresh = ConfigLoader(config_path).load()
        new_sources = _build_local_sources(fresh)
        storage_pool.replace_local_sources(new_sources)
        logger.info("로컬 소스 remount: %d개", len(new_sources))
        if device_mgr.device_id:
            from stardustlib.device_manager import build_local_source_inventory

            await device_mgr.report_sources(
                build_local_source_inventory(storage_pool)
            )

    from stardustlib import daemon
    await daemon.serve(config["metadata_db"], _cleanup, on_reload=_on_reload)


def _mount_remote_sources(
    config: dict, storage_pool, auth_client, server_url: str,
    my_devices: list | None = None, self_device_id: str | None = None,
) -> None:
    """remote 소스를 RemoteSource로 생성해 스토리지 풀에 마운트한다.

    두 종류를 마운트한다:
    1. 설정에 명시된 remote 타입 소스 (cfg["device_id"] 지정)
    2. 자동 발견: p2p.auto_mount_devices가 true이면, 내 계정의 다른 디바이스를
       (my_devices에서) 자동으로 remote 소스로 마운트한다. source_id는
       "remote-<device_id>" 규칙. config에 이미 명시된 device_id는 건너뛴다.

    인증 완료 후 호출한다. RemoteSource.initialize()는 routing으로 대상 주소를
    조회하며, 실패 시 비활성(오프라인 placeholder)으로 남는다. 개별 실패는
    전체를 막지 않는다.
    """
    from stardustlib.remote_source import RemoteSource

    logger = logging.getLogger(__name__)
    mounted_device_ids: set[str] = set()

    def _mount(source_id: str, device_id: str) -> None:
        try:
            source = RemoteSource(source_id, device_id, auth_client, server_url)
            source.initialize()
            storage_pool.add_source(source)
            # device_id로도 등록 → read_file의 크로스 디바이스 라우팅에 사용
            storage_pool.register_remote_device(device_id, source)
            mounted_device_ids.add(device_id)
            status = "활성" if source.is_active else "비활성(오프라인)"
            logger.info(
                "RemoteSource 마운트: id=%s device=%s (%s)",
                source_id, device_id, status,
            )
        except Exception as e:
            logger.warning("RemoteSource 마운트 실패 id=%s: %s", source_id, e)

    # 1) 설정에 명시된 remote 소스
    for cfg in config.get("sources", []):  # type: ignore[attr-defined]
        if cfg.get("type") != "remote":
            continue
        _mount(cfg["id"], cfg["device_id"])

    # 2) 자동 발견: 내 다른 디바이스를 remote 소스로 자동 마운트
    p2p_config = config.get("p2p", {})  # type: ignore[attr-defined]
    auto_mount = p2p_config.get("auto_mount_devices", True)
    if auto_mount and my_devices:
        for dev in my_devices:
            dev_id = dev.get("id")
            if not dev_id:
                continue
            if dev_id == self_device_id:
                continue  # 자기 자신 제외
            if dev_id in mounted_device_ids:
                continue  # 설정에 이미 명시된 디바이스 중복 방지
            _mount(f"remote-{dev_id}", dev_id)


def _eviction_safe(repl_mgr, virtual_path: str) -> bool:
    """이 파일의 로컬 청크를 비워도 되는지 판정한다(축출 안전 게이트).

    판정 기준은 총 카피 수가 아니라 **서로 다른 기기의 카피 수**다 — 카피가 모두 이
    기기에 있으면 비우는 순간 0이 되므로 축출 대상이 아니다(Property 4). 건강성 조회가
    실패하면 안전을 확인하지 못한 것이므로 보존한다.
    """
    try:
        health = repl_mgr.replication_health(virtual_path)
    except Exception:  # noqa: BLE001 — 안전 미확인 시 보존
        return False
    return getattr(health, "min_devices", 0) >= repl_mgr.target_copies


async def _eviction_loop(storage_pool, repl_mgr, cfg: dict) -> None:
    """로컬 여유가 low_watermark 미만이면 콜드(replicated) 파일을 축출해 회수한다.

    high_watermark까지 회복하도록 필요분만 축출한다. 안전 판정은 총 카피 수가 아니라
    **서로 다른 기기의 카피 수**로 한다 — 카피가 모두 이 기기에 있으면 비우는 순간 0이
    되므로 축출 대상이 아니다(Property 4). 다른 기기에 목표 수만큼 있어야 지운다.
    """
    logger = logging.getLogger(__name__)
    interval = cfg.get("interval_seconds", 300)
    low = int(cfg.get("low_watermark_bytes", 200 * 1024 * 1024))
    high = int(cfg.get("high_watermark_bytes", 500 * 1024 * 1024))
    def _is_safe(virtual_path: str) -> bool:
        return _eviction_safe(repl_mgr, virtual_path)

    while True:
        await asyncio.sleep(interval)
        try:
            free = storage_pool.get_available_space()
            if free >= low:
                continue
            need = max(0, high - free)
            report = await asyncio.to_thread(
                storage_pool.evict_cold, _is_safe, need
            )
            if report["evicted"]:
                logger.info(
                    "콜드 축출: %d개 파일, %d bytes 회수",
                    len(report["evicted"]), report["freed"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 다음 주기 재시도
            logger.warning("축출 루프 오류: %s", e)


def _build_local_sources(config: dict, *, read_only: bool = False) -> list:
    """config의 로컬 소스(directory/loopback)를 생성·initialize해 반환한다(remote 제외).

    _build_core와 config 리로드(remount)가 공유한다. read_only=True이면 루프백 FAT
    이미지를 읽기 전용으로 연다(GUI/CLI 조회 세션 — 쓰기는 데몬 단독).
    """
    from stardustlib.storage_source import DirectorySource, LoopbackSource

    sources = []
    for cfg in config.get("sources", []):
        source_type = cfg.get("type")
        if source_type == "directory":
            source = DirectorySource(cfg["id"], cfg["path"])
        elif source_type == "loopback":
            source = LoopbackSource(
                cfg["id"], cfg["path"], cfg["size"], read_only=read_only
            )
        else:
            continue  # remote 타입은 건너뜀
        source.initialize()
        sources.append(source)
    return sources


def _build_core(config: dict, *, read_only: bool = False) -> tuple:
    """로컬 스토리지 핵심 컴포넌트를 조립해 반환한다 (WebDAV 비의존).

    기존 initializer.py의 Phase 3-6 로직을 재사용한다. WebDAV 앱 생성은 포함하지
    않으므로 daemon(WebDAV)과 CLI가 공통으로 호출할 수 있다.

    Returns:
        (storage_pool, metadata_store, encryption_engine, db_key) 튜플.
        실패 시 sys.exit(1) 호출.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from stardustlib.config_loader import ConfigLoader
    from stardustlib.encryption_engine import EncryptionEngine
    from stardustlib.exceptions import InvalidKeyError, KeyNotFoundError
    from stardustlib.storage_pool import StoragePool
    from stardustlib.metadata_store import MetadataStore
    from stardustlib.storage_source import (
        DirectorySource,
        LoopbackSource,
        StorageSource,
    )

    logger = logging.getLogger(__name__)
    errors: list[str] = []

    # 암호화 키 로드
    key: bytes | None = None
    try:
        key = ConfigLoader.load_encryption_key(config.get("key_file"))  # type: ignore[attr-defined]
    except (KeyNotFoundError, InvalidKeyError) as e:
        errors.append(f"Encryption_Key 로드 실패: {e}")

    if key is not None and len(key) != 32:
        errors.append(
            f"Encryption_Key가 32바이트가 아님 (현재: {len(key)}바이트)"
        )

    # 스토리지 소스 검증
    for i, source_cfg in enumerate(config.get("sources", [])):  # type: ignore[attr-defined]
        source_type = source_cfg.get("type")
        path = source_cfg.get("path", "")

        if source_type == "directory":
            if not os.path.isdir(path):
                errors.append(
                    f"sources[{i}] Directory Source 경로 미존재: {path}"
                )
            elif not os.access(path, os.R_OK | os.W_OK):
                errors.append(
                    f"sources[{i}] Directory Source 권한 부족: {path}"
                )
        elif source_type == "loopback":
            if not os.path.isfile(path):
                parent = os.path.dirname(path)
                if parent and not os.access(parent, os.W_OK):
                    errors.append(
                        f"sources[{i}] Loopback Source 생성 불가: {path}"
                    )
        # remote 타입은 로컬 스토리지 초기화에서 건너뜀

    # Metadata Store 초기화
    metadata_store: MetadataStore | None = None
    if key is not None and len(key) == 32:
        try:
            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"stardustfs-metadata-db",
                info=b"db-encryption-key",
            )
            db_key = hkdf.derive(key)
            metadata_store = MetadataStore(config["metadata_db"], db_key)
            metadata_store.initialize()
        except Exception as e:
            errors.append(f"Metadata Store 초기화 실패: {e}")

    if errors:
        for err in errors:
            logger.error(err)
        sys.exit(1)

    # 컴포넌트 조립
    assert key is not None
    encryption_engine = EncryptionEngine(key)

    # 로컬 소스만 생성 (remote 제외)
    sources = _build_local_sources(config, read_only=read_only)

    assert metadata_store is not None
    storage_pool = StoragePool(sources, metadata_store, encryption_engine)

    logger.info("로컬 스토리지 초기화 완료")
    return storage_pool, metadata_store, encryption_engine, db_key


async def _restore_key_from_server(
    auth_client, key_file_path: str, logger
) -> None:
    """서버에서 암호화된 key blob을 다운로드하여 로컬에 key_file을 복원한다.

    환경변수 STARDUST_KEY_PASSWORD로 복호화한다.
    실패 시 예외를 발생시킨다 (graceful skip 하지 않음).
    """
    from pathlib import Path

    import httpx

    from stardustlib.exceptions import KeyNotFoundError
    from stardustlib.key_backup_engine import KeyBackupEngine

    # key_password: 자격증명 저장소(login 시 보관) 우선, 없으면 환경변수(마이그레이션)
    key_password = getattr(auth_client, "key_password", None) or os.environ.get(
        "STARDUST_KEY_PASSWORD", ""
    )
    if not key_password:
        raise RuntimeError(
            "key_file이 존재하지 않고 key 백업 암호도 없습니다. "
            "'stardustfs login --key-password ...'로 저장하거나 "
            "STARDUST_KEY_PASSWORD를 설정한 뒤 다시 실행하세요."
        )

    logger.info("key_file 미존재, 서버에서 key 백업 다운로드 시도...")

    token = await auth_client.get_valid_token()
    server_url = auth_client._server_url

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{server_url}/sync/key",
            headers={"Authorization": f"Bearer {token}"},
        )

    if response.status_code == 404:
        raise KeyNotFoundError(
            "서버에 key 백업이 존재하지 않습니다. "
            "최초 디바이스에서 먼저 key를 백업해야 합니다."
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"서버에서 key 다운로드 실패: HTTP {response.status_code}"
        )

    encrypted_blob = response.content
    engine = KeyBackupEngine()
    master_key = engine.decrypt_from_backup(encrypted_blob, key_password)

    # key_file 저장
    key_path = Path(key_file_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(master_key)
    logger.info("key_file 복원 완료: %s", key_file_path)


async def _backup_key_to_server(
    auth_client, key_file_path: str, logger
) -> None:
    """최초 디바이스에서 key_file을 서버에 백업한다.

    서버에 이미 key가 존재하면 덮어쓰지 않는다.
    환경변수 STARDUST_KEY_PASSWORD로 암호화한다.

    서버 도달 불가·인증 실패는 경고만 남기고 넘어간다(best-effort). key 백업은
    부팅 필수 단계가 아니고 다음 기동에서 재시도되므로, 여기서 예외를 올리면
    서버가 응답하지 않는 동안 daemon이 아예 뜨지 못한다.
    """
    from pathlib import Path

    import httpx

    from stardustlib.exceptions import AuthenticationError
    from stardustlib.key_backup_engine import KeyBackupEngine

    # key_password: 자격증명 저장소 우선, 없으면 환경변수(마이그레이션)
    key_password = getattr(auth_client, "key_password", None) or os.environ.get(
        "STARDUST_KEY_PASSWORD", ""
    )
    if not key_password:
        logger.warning(
            "key 백업 암호 미설정, key 백업 건너뜀. 'stardustfs login "
            "--key-password ...'로 저장하면 다른 디바이스에서 복원할 수 있습니다."
        )
        return

    # 서버에 이미 key가 존재하는지 확인
    try:
        token = await auth_client.get_valid_token()
        server_url = auth_client._server_url

        async with httpx.AsyncClient(timeout=10.0) as client:
            check_resp = await client.get(
                f"{server_url}/sync/key",
                headers={"Authorization": f"Bearer {token}"},
            )
    except (httpx.HTTPError, AuthenticationError) as e:
        logger.warning("key 백업 확인 실패, 건너뜀(다음 기동에 재시도): %s", e)
        return

    if check_resp.status_code == 200:
        logger.info("서버에 key 백업이 이미 존재, 덮어쓰지 않음")
        return

    # 서버에 key가 없으면 업로드
    master_key = Path(key_file_path).read_bytes()
    engine = KeyBackupEngine()
    encrypted_blob = engine.encrypt_for_backup(master_key, key_password)

    try:
        token = await auth_client.get_valid_token()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(
                f"{server_url}/sync/key",
                headers={"Authorization": f"Bearer {token}"},
                content=encrypted_blob,
            )
    except (httpx.HTTPError, AuthenticationError) as e:
        logger.warning("key 백업 업로드 실패, 건너뜀(다음 기동에 재시도): %s", e)
        return

    if response.status_code < 400:
        logger.info("key 백업 업로드 완료")
    else:
        logger.error("key 백업 업로드 실패: HTTP %d", response.status_code)


if __name__ == "__main__":
    main()
