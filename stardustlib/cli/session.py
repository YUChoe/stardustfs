"""CLI 세션: 설정으로 코어 컴포넌트를 조립하고 명령에 노출한다.

두 가지 진입을 제공한다.
- open(): 오프라인(로컬 코어)만 조립. df/ls/status 등 서버 불필요 명령용. 동기.
- open_online(): 로그인 → device 등록/조회 → remote 마운트 → (선택) 1회 메타데이터
  동기화까지 수행. devices 및 향후 get/put 등 서버 필요 명령용. 단일 asyncio.run
  안에서 setup→op→teardown 을 처리한다 (auth_client의 httpx 루프 바인딩 때문).

read_file/write_file의 원격 경로는 remote_source의 전용 이벤트 루프로 자가
브리지되므로, 명령 본체(동기 JBOD 호출)는 이벤트 루프를 소유하지 않는다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from stardustlib.config_loader import ConfigLoader

logger = logging.getLogger(__name__)


class CLISession:
    """단발 CLI 명령이 사용하는 코어 컴포넌트 묶음.

    online 속성이 True면 서버 연동(device 목록 등)이 가능하다.
    """

    def __init__(self, jbod_manager, metadata_store) -> None:
        self.jbod = jbod_manager
        self.metadata = metadata_store
        # 온라인 setup에서만 채워진다.
        self.online: bool = False
        self.auth = None
        self.device_mgr = None
        self.my_devices: list[dict] | None = None
        self.self_device_id: str | None = None

    @classmethod
    def open(cls, config_path: str) -> "CLISession":
        """설정을 로드하고 로컬 코어만 조립한다 (서버 접속 없음).

        실패 시 _build_core가 sys.exit(1)을 호출한다.
        """
        # 순환 import 방지를 위해 함수 내부에서 import (stardustfs가 cli를 호출)
        from stardustfs import _build_core

        config = ConfigLoader(config_path).load()
        jbod_manager, metadata_store, _enc, _db_key = _build_core(config)
        return cls(jbod_manager, metadata_store)

    @classmethod
    async def open_online(
        cls, config_path: str, *, sync: bool = True
    ) -> "CLISession":
        """로그인·device 등록·remote 마운트(·동기화)까지 수행한다.

        server.url 미설정 또는 인증 실패 시 오프라인 세션으로 강등한다
        (online=False). sync=True면 1회 메타데이터 동기화를 수행한다.
        """
        from stardustfs import (
            _build_core,
            _mount_remote_sources,
            _restore_key_from_server,
        )
        from stardustlib.auth_client import AuthClient
        from stardustlib.device_manager import DeviceManager

        config = ConfigLoader(config_path).load()
        server = config.get("server")
        server_url = server.get("url") if isinstance(server, dict) else None

        if not server_url:
            logger.info("server.url 미설정, 오프라인 세션으로 시작")
            jbod, metadata, _enc, _db = _build_core(config)
            return cls(jbod, metadata)

        auth = AuthClient(server_url)
        email = os.environ.get("STARDUST_EMAIL", "")
        password = os.environ.get("STARDUST_PASSWORD", "")

        try:
            await auth.login(email, password)
        except Exception as e:  # noqa: BLE001 — 오프라인 강등
            logger.warning("인증 실패, 오프라인 세션으로 강등: %s", e)
            await auth.close()
            jbod, metadata, _enc, _db = _build_core(config)
            return cls(jbod, metadata)

        key_file = config.get("key_file")
        if key_file and not Path(key_file).exists():
            await _restore_key_from_server(auth, key_file, logger)

        jbod, metadata, _enc, db_key = _build_core(config)

        device_name = server.get("device_name", "unknown")
        p2p = config.get("p2p", {})
        p2p_port = p2p.get("port", 9090)
        device_mgr = DeviceManager(auth, server_url, device_name, p2p_port)

        my_devices: list[dict] = []
        await device_mgr.register()
        jbod.device_id = device_mgr.device_id
        my_devices = await device_mgr.list_devices()
        _mount_remote_sources(
            config, jbod, auth, server_url,
            my_devices=my_devices, self_device_id=device_mgr.device_id,
        )

        if sync:
            await cls._sync_once(
                auth, server_url, metadata, db_key, jbod, device_name, config
            )

        session = cls(jbod, metadata)
        session.online = True
        session.auth = auth
        session.device_mgr = device_mgr
        session.my_devices = my_devices
        session.self_device_id = device_mgr.device_id
        return session

    @staticmethod
    async def _sync_once(
        auth, server_url, metadata, db_key, jbod, device_name, config
    ) -> None:
        """1회 메타데이터 동기화. 실패해도 로컬 DB로 진행한다."""
        from stardustlib.conflict_resolver import ConflictResolver
        from stardustlib.sync_client import SyncClient

        interval = config.get("sync", {}).get("interval_seconds", 30)
        try:
            resolver = ConflictResolver(metadata, device_name)
            sync_client = SyncClient(
                auth, server_url, metadata, resolver, interval,
                encryption_key=db_key, jbod_manager=jbod,
            )
            await sync_client.initial_sync()
        except Exception as e:  # noqa: BLE001 — 로컬 DB 폴백
            logger.warning("메타데이터 동기화 실패, 로컬 DB 사용: %s", e)

    def close(self) -> None:
        """동기 리소스를 정리한다 (오프라인 세션용)."""
        self._close_metadata()

    async def aclose(self) -> None:
        """비동기 리소스까지 정리한다 (온라인 세션용)."""
        if self.device_mgr is not None:
            try:
                await self.device_mgr.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("device_mgr 종료 중 예외: %s", e)
        if self.auth is not None:
            try:
                await self.auth.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("auth_client 종료 중 예외: %s", e)
        self._close_metadata()

    def _close_metadata(self) -> None:
        try:
            self.metadata.close()
        except Exception as e:  # noqa: BLE001 — 종료 경로, 로깅만
            logger.debug("metadata_store 종료 중 예외: %s", e)
