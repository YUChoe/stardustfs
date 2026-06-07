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
from pathlib import Path

from stardustlib.config_loader import ConfigLoader

logger = logging.getLogger(__name__)


def _identify_self(my_devices: list[dict], device_name: str) -> str | None:
    """device 목록에서 (name, os)로 자기 device의 id를 찾는다.

    서버는 (user_id, name, os)로 device를 식별하므로 같은 기준으로 매칭한다.
    찾지 못하면 None (이 device가 아직 등록되지 않음 — daemon 미실행).
    """
    from stardustlib.device_manager import _get_os_info

    os_info = _get_os_info()
    for device in my_devices:
        if device.get("name") == device_name and device.get("os") == os_info:
            return device.get("id")
    return None


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
        self.sync_client = None
        self.server_url: str | None = None
        self.my_devices: list[dict] | None = None
        self.self_device_id: str | None = None

    def make_replication_manager(self):
        """리플리케이션 매니저를 생성한다(온라인 세션 전용).

        호출자가 close()로 정리한다.
        """
        if not self.online or self.auth is None or not self.server_url:
            raise RuntimeError("리플리케이션은 온라인 세션이 필요합니다")
        from stardustlib.replication_manager import ReplicationManager

        return ReplicationManager(
            self.auth, self.server_url, self.metadata, self.jbod
        )

    @classmethod
    def open(cls, config_path: str, *, read_only: bool = False) -> "CLISession":
        """설정을 로드하고 로컬 코어만 조립한다 (서버 접속 없음).

        read_only=True이면 루프백 FAT 이미지를 읽기 전용으로 연다(조회·용량 표시용).
        쓰기는 데몬 단독이므로, GUI/CLI 조회 세션은 read_only로 열어 같은 FAT 이미지를
        데몬과 동시에 rw로 여는 충돌을 피한다. 실패 시 _build_core가 sys.exit(1) 호출.
        """
        # 순환 import 방지를 위해 함수 내부에서 import (stardustfs가 cli를 호출)
        from stardustfs import _build_core

        config = ConfigLoader(config_path).load()
        jbod_manager, metadata_store, _enc, _db_key = _build_core(
            config, read_only=read_only
        )
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
        from stardustlib.credential_store import CredentialStore
        from stardustlib.device_manager import DeviceManager
        from stardustlib.exceptions import AuthenticationError

        config = ConfigLoader(config_path).load()
        server = config.get("server")
        server_url = server.get("url") if isinstance(server, dict) else None

        if not server_url:
            logger.info("server.url 미설정, 오프라인 세션으로 시작")
            jbod, metadata, _enc, _db = _build_core(config)
            return cls(jbod, metadata)

        # 저장된 토큰으로 인증 (비밀번호는 사용하지 않음). 토큰 없거나 무효면
        # 오프라인 세션으로 강등 → 온라인 명령은 dispatcher에서 'login 필요' 처리.
        store = CredentialStore(config["metadata_db"])
        auth = AuthClient(server_url, credential_store=store)
        if not auth.load_from_store():
            logger.warning(
                "저장된 자격증명이 없습니다. 'stardustfs login'을 먼저 실행하세요."
            )
            await auth.close()
            jbod, metadata, _enc, _db = _build_core(config)
            return cls(jbod, metadata)
        try:
            await auth.get_valid_token()
        except AuthenticationError as e:
            logger.warning(
                "토큰이 만료/무효합니다 (%s). 'stardustfs login'을 다시 실행하세요.",
                e,
            )
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

        # CLI는 register()하지 않는다 — daemon이 보정해 둔 connection_address(공인
        # IP)를 CLI의 LAN 주소로 덮어쓰지 않기 위함. 대신 device 목록에서 (name,
        # os)로 자기 device를 식별해 device_id만 얻는다. 등록·주소 보정은 daemon이
        # 소유한다.
        my_devices = await device_mgr.list_devices()
        self_device_id = _identify_self(my_devices, device_name)
        if self_device_id is not None:
            jbod.device_id = self_device_id
            device_mgr._device_id = self_device_id
        else:
            logger.warning(
                "이 device가 서버에 등록돼 있지 않습니다 (name=%s). daemon을 먼저 "
                "실행해 등록하세요. 원격 라우팅 없이 진행합니다.",
                device_name,
            )
        _mount_remote_sources(
            config, jbod, auth, server_url,
            my_devices=my_devices, self_device_id=self_device_id,
        )

        sync_client = None
        if sync:
            sync_client = await cls._make_sync_client(
                auth, server_url, metadata, db_key, jbod, device_name, config
            )

        session = cls(jbod, metadata)
        session.online = True
        session.auth = auth
        session.device_mgr = device_mgr
        session.sync_client = sync_client
        session.server_url = server_url
        session.my_devices = my_devices
        session.self_device_id = self_device_id
        return session

    @staticmethod
    async def _make_sync_client(
        auth, server_url, metadata, db_key, jbod, device_name, config
    ):
        """SyncClient를 만들고 1회 초기 동기화한다. 실패해도 클라이언트는 반환한다
        (이후 upload_metadata로 전파 시도 가능). 동기화 실패 시 로컬 DB로 진행."""
        from stardustlib.conflict_resolver import ConflictResolver
        from stardustlib.sync_client import SyncClient

        interval = config.get("sync", {}).get("interval_seconds", 30)
        resolver = ConflictResolver(metadata, device_name)
        sync_client = SyncClient(
            auth, server_url, metadata, resolver, interval,
            encryption_key=db_key, jbod_manager=jbod,
        )
        try:
            await sync_client.initial_sync()
        except Exception as e:  # noqa: BLE001 — 로컬 DB 폴백
            logger.warning("초기 동기화 실패, 로컬 DB 사용: %s", e)
        return sync_client

    def close(self) -> None:
        """동기 리소스를 정리한다 (오프라인 세션용)."""
        self._close_metadata()

    async def upload_if_online(self) -> None:
        """온라인 세션이면 pending 변경을 서버로 전파한다 (없으면 no-op)."""
        if self.sync_client is not None:
            await self.sync_client.upload_metadata()

    async def aclose(self) -> None:
        """비동기 리소스까지 정리한다 (온라인 세션용)."""
        if self.sync_client is not None:
            try:
                await self.sync_client.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("sync_client 종료 중 예외: %s", e)
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
            self.jbod.close_local_sources()
        except Exception as e:  # noqa: BLE001 — 종료 경로, 로깅만
            logger.debug("로컬 소스 종료 중 예외: %s", e)
        try:
            self.metadata.close()
        except Exception as e:  # noqa: BLE001 — 종료 경로, 로깅만
            logger.debug("metadata_store 종료 중 예외: %s", e)
