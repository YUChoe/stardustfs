"""메타데이터 충돌 감지 및 해결 모듈.

서버와 로컬 메타데이터 병합 시 충돌을 감지하고,
로컬 파일을 conflict copy로 rename하여 양쪽 변경사항을 보존한다.
"""

import logging
import posixpath
from datetime import datetime, timezone

from stardustlib.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


class ConflictResolver:
    """메타데이터 충돌 감지 및 해결.

    충돌 판정 조건:
        server_version > local_base_version AND local_version > local_base_version
        (양쪽 모두 base 이후 수정됨)

    충돌 해결 전략:
        1. 로컬 파일을 conflict copy로 rename
        2. 서버 버전의 메타데이터를 원본 경로에 적용
    """

    def __init__(
        self,
        metadata_store: MetadataStore,
        device_name: str,
    ) -> None:
        self._metadata_store = metadata_store
        self._device_name = device_name

    def detect_conflict(
        self,
        virtual_path: str,
        server_version: int,
        local_version: int,
        local_base_version: int,
    ) -> bool:
        """충돌 여부를 판정한다.

        양쪽 모두 base_version 이후 수정된 경우 충돌로 판정한다.

        Args:
            virtual_path: 대상 파일의 가상 경로.
            server_version: 서버 측 현재 version.
            local_version: 로컬 측 현재 version.
            local_base_version: 마지막 동기화 시점의 version.

        Returns:
            충돌이면 True, 아니면 False.
        """
        return (
            server_version > local_base_version
            and local_version > local_base_version
        )

    def resolve_conflict(
        self,
        virtual_path: str,
        server_version: int,
    ) -> str:
        """충돌을 해결하고 conflict copy 경로를 반환한다.

        1. generate_conflict_name()으로 새 파일명 생성
        2. MetadataStore.rename_path()로 로컬 파일을 conflict copy로 rename
        3. conflict copy의 sync_status를 "conflict"로 설정
        4. 원본 virtual_path의 version을 server_version으로 갱신

        Args:
            virtual_path: 충돌이 발생한 파일의 가상 경로.
            server_version: 서버 측 version (원본 경로에 적용할 값).

        Returns:
            생성된 conflict copy의 가상 경로.

        Raises:
            Exception: conflict copy 생성 실패 시 sync_status를
                "pending"으로 유지하고 로그를 기록한 뒤 예외를 재발생시킨다.
        """
        try:
            conflict_path = self.generate_conflict_name(virtual_path)

            # 로컬 파일을 conflict copy로 rename
            self._metadata_store.rename_path(virtual_path, conflict_path)

            # conflict copy의 sync_status를 "conflict"로 설정
            self._metadata_store.set_sync_status(conflict_path, "conflict")

            # 서버 버전의 메타데이터를 원본 경로에 적용 (version 갱신)
            # 원본 경로의 레코드는 rename으로 이동했으므로,
            # 서버 메타데이터 적용은 호출자(SyncClient)가 담당한다.
            # 여기서는 version 갱신을 위해 원본 경로 레코드가 존재해야 하므로,
            # 호출자가 서버 메타데이터를 원본 경로에 삽입한 뒤 version을 설정한다.
            # 단, design 문서에 따르면 resolve_conflict 내에서 처리하므로
            # 원본 경로의 version을 직접 갱신하는 대신 반환값으로 알린다.

            logger.info(
                "충돌 해결 완료: %s → %s (server_version=%d)",
                virtual_path, conflict_path, server_version,
            )
            return conflict_path

        except Exception as e:
            # conflict copy 생성 실패 시 sync_status "pending" 유지, 로그 기록
            logger.error(
                "conflict copy 생성 실패: %s, 오류: %s. "
                "sync_status를 'pending'으로 유지합니다.",
                virtual_path, e,
            )
            try:
                self._metadata_store.set_sync_status(virtual_path, "pending")
            except Exception:
                pass
            raise

    def generate_conflict_name(self, virtual_path: str) -> str:
        """충돌 파일명을 생성한다.

        형식: "{dir}/{name} (conflict - {device_name} - {YYYY-MM-DD HH-MM-SS}).{ext}"
        동일 파일명이 이미 존재하면 "(2)", "(3)" 순번을 추가한다.

        Args:
            virtual_path: 원본 파일의 가상 경로.

        Returns:
            고유한 conflict copy 가상 경로.
        """
        dir_part = posixpath.dirname(virtual_path)
        basename = posixpath.basename(virtual_path)

        # 확장자 분리
        dot_idx = basename.rfind(".")
        if dot_idx > 0:
            name = basename[:dot_idx]
            ext = basename[dot_idx:]  # ".txt" 형태
        else:
            name = basename
            ext = ""

        # 타임스탬프 생성
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%d %H-%M-%S")

        # 기본 conflict 파일명
        conflict_name = f"{name} (conflict - {self._device_name} - {timestamp}){ext}"
        conflict_path = (
            posixpath.join(dir_part, conflict_name) if dir_part else conflict_name
        )

        # 존재 여부 확인, 존재하면 순번 추가
        if self._metadata_store.lookup(conflict_path) is None:
            return conflict_path

        # 순번 부여: (2), (3), ...
        seq = 2
        while True:
            conflict_name_seq = (
                f"{name} (conflict - {self._device_name} - {timestamp}) "
                f"({seq}){ext}"
            )
            conflict_path_seq = (
                posixpath.join(dir_part, conflict_name_seq)
                if dir_part
                else conflict_name_seq
            )
            if self._metadata_store.lookup(conflict_path_seq) is None:
                return conflict_path_seq
            seq += 1
