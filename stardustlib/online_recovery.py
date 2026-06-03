"""오프라인 → 온라인 복구 매니저.

백그라운드에서 주기적으로 서버 연결을 재시도하고,
연결 복구 시 인증 → 디바이스 등록 → pending 업로드 → 서버 metadata 병합
→ P2P 서버 시작 → heartbeat 시작 순서로 복구를 수행한다.
"""

from __future__ import annotations

import asyncio
import logging

from stardustlib.auth_client import AuthClient
from stardustlib.device_manager import DeviceManager
from stardustlib.exceptions import AuthenticationError, DeviceRegistrationError
from stardustlib.sync_client import SyncClient

logger = logging.getLogger(__name__)

_DEFAULT_CHECK_INTERVAL = 60  # 초


class OnlineRecoveryManager:
    """오프라인 → 온라인 복구를 관리하는 백그라운드 매니저."""

    def __init__(
        self,
        auth_client: AuthClient,
        device_mgr: DeviceManager,
        sync_client: SyncClient,
        p2p_server,
        *,
        check_interval: int = _DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._auth_client = auth_client
        self._device_mgr = device_mgr
        self._sync_client = sync_client
        self._p2p_server = p2p_server
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False
        self._recovered = False

    @property
    def is_recovered(self) -> bool:
        """온라인 복구가 완료되었는지 여부."""
        return self._recovered

    async def start(self) -> None:
        """복구 루프를 백그라운드 태스크로 시작한다."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._recovery_loop())
        logger.info(
            "OnlineRecoveryManager started (interval=%ds)",
            self._check_interval,
        )

    async def stop(self) -> None:
        """복구 루프를 중지한다."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("OnlineRecoveryManager stopped")

    async def _recovery_loop(self) -> None:
        """check_interval마다 복구를 시도하는 루프.

        복구 성공 시 루프를 종료한다.
        """
        while self._running:
            await asyncio.sleep(self._check_interval)
            if not self._running:
                break

            try:
                success = await self._attempt_recovery()
                if success:
                    self._recovered = True
                    self._running = False
                    logger.info("Online recovery completed successfully")
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    "Recovery attempt failed, will retry: %s", e
                )

    async def _attempt_recovery(self) -> bool:
        """단일 복구 시도. 성공 시 True, 실패 시 False를 반환한다.

        복구 순서:
        1. 인증 재시도 (login)
        2. 디바이스 등록
        3. pending 변경사항 업로드
        4. 서버 metadata 다운로드 및 병합
        5. P2P 서버 시작
        6. heartbeat 시작
        """
        # (1) 인증 재시도 — 저장된 토큰을 재로딩하고 유효성 확인(필요 시 갱신).
        #     비밀번호는 사용하지 않는다. 토큰 없거나 무효면 복구 실패(재로그인 필요).
        if not self._auth_client.load_from_store():
            logger.debug("Recovery: 저장된 자격증명 없음 (login 필요)")
            return False
        try:
            await self._auth_client.get_valid_token()
        except AuthenticationError as e:
            logger.debug("Recovery auth failed: %s", e)
            return False
        except Exception as e:
            logger.debug("Recovery auth error: %s", e)
            return False

        # (2) 디바이스 등록
        try:
            await self._device_mgr.register()
        except DeviceRegistrationError as e:
            logger.debug("Recovery device registration failed: %s", e)
            return False

        # (3) pending 변경사항 업로드 (시각순)
        try:
            await self._sync_client.upload_metadata()
        except Exception as e:
            logger.warning("Recovery pending upload failed: %s", e)
            # 업로드 실패해도 다음 주기에 재시도 가능하므로 계속 진행하지 않음
            return False

        # (4) 서버 metadata 다운로드 및 병합
        try:
            await self._sync_client.initial_sync()
        except Exception as e:
            logger.warning("Recovery metadata sync failed: %s", e)
            # 병합 실패는 치명적이지 않으나 복구 미완료로 처리
            return False

        # (5) P2P 서버 시작
        if self._p2p_server is not None:
            try:
                await self._p2p_server.start()
            except Exception as e:
                logger.warning("Recovery P2P server start failed: %s", e)
                # P2P 실패는 치명적이지 않으므로 계속 진행

        # (6) heartbeat 시작
        try:
            await self._device_mgr.start_heartbeat()
        except Exception as e:
            logger.warning("Recovery heartbeat start failed: %s", e)

        # periodic sync 시작
        try:
            await self._sync_client.start_periodic_sync()
        except Exception as e:
            logger.warning("Recovery periodic sync start failed: %s", e)

        return True
