#!/usr/bin/env python3
"""StardustFS - WebDAV 기반 암호화 가상 파일시스템."""

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

from stardustlib.initializer import initialize_system


def main() -> None:
    """메인 엔트리포인트. --config 인자를 처리하고 서버를 시작한다."""
    parser = argparse.ArgumentParser(description="StardustFS WebDAV Server")
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="JSON 설정 파일 경로",
    )
    args = parser.parse_args()

    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 불필요한 로그 억제
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("wsgidav").setLevel(logging.WARNING)

    # 설정 로드 (Phase 1)
    from stardustlib.config_loader import ConfigLoader

    try:
        loader = ConfigLoader(args.config)
        config = loader.load()
    except (FileNotFoundError, Exception) as e:
        logging.error("설정 로드 실패: %s", e)
        sys.exit(1)

    version = config.get("version")  # type: ignore[attr-defined]

    if version == 2:
        asyncio.run(startup_v2(config, args.config))
    else:
        # v1: 기존 초기화 흐름
        _startup_v1(args.config, config)


def _startup_v1(config_path: str, config: dict) -> None:
    """v1 설정 기반 기존 초기화 흐름 (cheroot WebDAV 서버)."""
    app, config = initialize_system(config_path)

    from cheroot.wsgi import Server as WSGIServer

    host = config["webdav"]["host"]
    port = config["webdav"]["port"]

    server = WSGIServer((host, port), app)
    logging.info("WebDAV 서버 시작: http://%s:%d/", host, port)

    try:
        server.start()
    except KeyboardInterrupt:
        logging.info("서버 종료 중...")
    finally:
        server.stop()


async def startup_v2(config: dict, config_path: str) -> None:
    """v2 설정 기반 MVP2 초기화 흐름.

    순서: (1) 설정 로드 → (2) 인증 → (3) key_file 복원 (필요 시)
    → (4) 로컬 스토리지 초기화 → (5) 디바이스 등록
    → (6) 메타데이터 동기화 → (7) P2P 서버 시작 → (8) WebDAV 서버 시작

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
            app, jbod_manager, metadata_store, encryption_engine, db_key = (
                _initialize_local_storage(config)
            )
        except SystemExit:
            raise
        except Exception as e:
            logger.error("로컬 스토리지 초기화 실패: %s", e)
            sys.exit(1)
        _start_webdav(config, app)
        return

    # (2) 인증
    from stardustlib.auth_client import AuthClient

    email = os.environ.get("STARDUST_EMAIL", "")
    password = os.environ.get("STARDUST_PASSWORD", "")

    auth_client = AuthClient(server_url)
    offline_mode = False

    try:
        await auth_client.login(email, password)
    except AuthenticationError as e:
        logger.warning("인증 실패, 오프라인 모드로 전환: %s", e)
        offline_mode = True
    except Exception as e:
        logger.warning("인증 중 예외 발생, 오프라인 모드로 전환: %s", e)
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
        app, jbod_manager, metadata_store, encryption_engine, db_key = (
            _initialize_local_storage(config)
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

        try:
            _start_webdav(config, app)
        finally:
            await recovery_mgr.stop()
            await sync_client.stop()
            await device_mgr.stop()
            if p2p_server is not None:
                await p2p_server.stop()
            await auth_client.close()
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
        await auth_client.close()
        _start_webdav(config, app)
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
            app, jbod_manager, metadata_store, encryption_engine, db_key = (
                _initialize_local_storage(config)
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

    # (7) WebDAV 서버 시작 (별도 스레드에서 블로킹 실행)
    import threading

    webdav_thread = threading.Thread(
        target=_start_webdav, args=(config, app), daemon=True
    )
    webdav_thread.start()

    # asyncio 루프 유지 (Ctrl+C로 종료)
    try:
        while webdav_thread.is_alive():
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # 정리
        await sync_client.stop()
        await device_mgr.stop()
        if relay_worker is not None:
            await relay_worker.stop()
        if p2p_server is not None:
            await p2p_server.stop()
        await auth_client.close()


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


def _initialize_local_storage(config: dict) -> tuple:
    """로컬 스토리지를 초기화하고 WebDAV 앱과 핵심 컴포넌트를 반환한다.

    기존 initializer.py의 Phase 3-6 로직을 재사용한다.

    Returns:
        (app, jbod_manager, metadata_store, encryption_engine) 튜플.
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
    from stardustlib.webdav_provider import create_webdav_app

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
    app = create_webdav_app(config, jbod_manager, encryption_engine)

    logger.info("로컬 스토리지 초기화 완료")
    return app, jbod_manager, metadata_store, encryption_engine, db_key


def _start_webdav(config: dict, app) -> None:
    """cheroot WebDAV 서버를 시작한다 (블로킹)."""
    from cheroot.wsgi import Server as WSGIServer

    host = config["webdav"]["host"]
    port = config["webdav"]["port"]

    server = WSGIServer((host, port), app)
    logging.info("WebDAV 서버 시작: http://%s:%d/", host, port)

    try:
        server.start()
    except KeyboardInterrupt:
        logging.info("서버 종료 중...")
    finally:
        server.stop()


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

    key_password = os.environ.get("STARDUST_KEY_PASSWORD", "")
    if not key_password:
        raise RuntimeError(
            "key_file이 존재하지 않고 STARDUST_KEY_PASSWORD 환경변수가 "
            "설정되지 않았습니다. 새 디바이스에서는 key 복원을 위해 "
            "STARDUST_KEY_PASSWORD를 설정해야 합니다."
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

    key_password = os.environ.get("STARDUST_KEY_PASSWORD", "")
    if not key_password:
        logger.warning(
            "STARDUST_KEY_PASSWORD 미설정, key 백업 건너뜀. "
            "다른 디바이스에서 key를 복원하려면 백업이 필요합니다."
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
