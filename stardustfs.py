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

        run_gui(getattr(args, "config", None))
        return

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

    # 중복 실행 방지 (제어 파일 기준)
    metadata_db = config.get("metadata_db")  # type: ignore[attr-defined]
    if metadata_db and daemon.read_status(metadata_db).get("running"):
        pid = daemon.read_status(metadata_db).get("pid")
        logging.error("daemon이 이미 실행 중입니다 (pid=%s)", pid)
        sys.exit(1)

    version = config.get("version")  # type: ignore[attr-defined]

    if version != 2:
        logging.error(
            "지원하지 않는 설정 버전입니다: %s (v2 필요). "
            "WebDAV 기반 v1 흐름은 제거되었습니다.", version,
        )
        sys.exit(1)

    asyncio.run(startup_v2(config, config_path))


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
            jbod_manager, metadata_store, encryption_engine, db_key = (
                _build_core(config)
            )
        except SystemExit:
            raise
        except Exception as e:
            logger.error("로컬 스토리지 초기화 실패: %s", e)
            sys.exit(1)

        async def _cleanup() -> None:
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
        jbod_manager, metadata_store, encryption_engine, db_key = (
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
            jbod_manager=jbod_manager,
        )

        # P2P 서버 인스턴스 (복구 시 시작됨)
        from stardustlib.p2p_server import P2PServer

        p2p_server = None
        p2p_enabled = p2p_config.get("enabled", False)
        if p2p_enabled:
            p2p_server = P2PServer(
                jbod_manager, auth_client, p2p_port, server_url
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
            metadata_store.close()

        from stardustlib import daemon
        await daemon.serve(config["metadata_db"], _cleanup)
        return

    # 등록된 device_id를 JBOD에 주입 (파일 변경 추적용)
    jbod_manager.device_id = device_mgr.device_id

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
        config, jbod_manager, auth_client, server_url,
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
        jbod_manager=jbod_manager,
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
            metadata_store.close()
            jbod_manager, metadata_store, encryption_engine, db_key = (
                _build_core(config)
            )
            # device_id, remote 소스 재주입 (jbod_manager가 교체되었으므로)
            jbod_manager.device_id = device_mgr.device_id
            _mount_remote_sources(
                config, jbod_manager, auth_client, server_url,
                my_devices=my_devices, self_device_id=device_mgr.device_id,
            )
            # SyncClient 재생성
            conflict_resolver = ConflictResolver(metadata_store, device_name)
            sync_client = SyncClient(
                auth_client, server_url, metadata_store,
                conflict_resolver, interval_seconds,
                encryption_key=db_key,
                jbod_manager=jbod_manager,
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
    p2p_enabled = p2p_config.get("enabled", False)

    if p2p_enabled:
        p2p_server = P2PServer(jbod_manager, auth_client, p2p_port, server_url)
        await p2p_server.start()
        await device_mgr.setup_upnp()

        # 릴레이 워커 시작 (직접 연결 불가 환경의 fallback 수신).
        # device_id가 확보된 경우에만 시작한다.
        device_id = device_mgr.device_id
        if p2p_config.get("relay_enabled", True) and device_id:
            from stardustlib.relay_worker import RelayWorker

            relay_worker = RelayWorker(
                p2p_server, auth_client, server_url, device_id
            )
            await relay_worker.start()

    await device_mgr.start_heartbeat()
    await sync_client.start_periodic_sync()

    # (7) 상주 루프 (정지 신호까지 — Ctrl+C 또는 'daemon stop')
    async def _cleanup() -> None:
        await sync_client.stop()
        await device_mgr.stop()
        if relay_worker is not None:
            await relay_worker.stop()
        if p2p_server is not None:
            await p2p_server.stop()
        await auth_client.close()
        metadata_store.close()

    from stardustlib import daemon
    await daemon.serve(config["metadata_db"], _cleanup)


def _mount_remote_sources(
    config: dict, jbod_manager, auth_client, server_url: str,
    my_devices: list | None = None, self_device_id: str | None = None,
) -> None:
    """remote 소스를 RemoteSource로 생성해 JBOD에 마운트한다.

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
            jbod_manager.add_source(source)
            # device_id로도 등록 → read_file의 크로스 디바이스 라우팅에 사용
            jbod_manager.register_remote_device(device_id, source)
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


def _build_core(config: dict) -> tuple:
    """로컬 스토리지 핵심 컴포넌트를 조립해 반환한다 (WebDAV 비의존).

    기존 initializer.py의 Phase 3-6 로직을 재사용한다. WebDAV 앱 생성은 포함하지
    않으므로 daemon(WebDAV)과 CLI가 공통으로 호출할 수 있다.

    Returns:
        (jbod_manager, metadata_store, encryption_engine, db_key) 튜플.
        실패 시 sys.exit(1) 호출.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from stardustlib.config_loader import ConfigLoader
    from stardustlib.encryption_engine import EncryptionEngine
    from stardustlib.exceptions import InvalidKeyError, KeyNotFoundError
    from stardustlib.jbod_manager import JBODManager
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
    sources: list[StorageSource] = []
    for cfg in config.get("sources", []):  # type: ignore[attr-defined]
        source_type = cfg["type"]
        if source_type == "directory":
            source = DirectorySource(cfg["id"], cfg["path"])
        elif source_type == "loopback":
            source = LoopbackSource(cfg["id"], cfg["path"], cfg["size"])
        else:
            # remote 타입은 여기서 건너뜀
            continue
        source.initialize()
        sources.append(source)

    assert metadata_store is not None
    jbod_manager = JBODManager(sources, metadata_store, encryption_engine)

    logger.info("로컬 스토리지 초기화 완료")
    return jbod_manager, metadata_store, encryption_engine, db_key


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
    """
    from pathlib import Path

    import httpx

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
    token = await auth_client.get_valid_token()
    server_url = auth_client._server_url

    async with httpx.AsyncClient(timeout=10.0) as client:
        check_resp = await client.get(
            f"{server_url}/sync/key",
            headers={"Authorization": f"Bearer {token}"},
        )
        if check_resp.status_code == 200:
            logger.info("서버에 key 백업이 이미 존재, 덮어쓰지 않음")
            return

    # 서버에 key가 없으면 업로드
    master_key = Path(key_file_path).read_bytes()
    engine = KeyBackupEngine()
    encrypted_blob = engine.encrypt_for_backup(master_key, key_password)

    token = await auth_client.get_valid_token()

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.put(
            f"{server_url}/sync/key",
            headers={"Authorization": f"Bearer {token}"},
            content=encrypted_blob,
        )

    if response.status_code < 400:
        logger.info("key 백업 업로드 완료")
    else:
        logger.error("key 백업 업로드 실패: HTTP %d", response.status_code)


if __name__ == "__main__":
    main()
