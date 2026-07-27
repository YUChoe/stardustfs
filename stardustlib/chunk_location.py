"""청크 카피 위치.

카피(copy)는 청크 암호문 한 벌이고, 위치는 `(device_id, source_id)`로 식별한다.
"원본"과 "복제본"을 구분하지 않는다 — 풀에 올라간 뒤에는 어느 카피가 먼저 쓰였는지가
의미를 갖지 않으므로 카피 간 우열을 두지 않는다.

카피 수와 위치 분포(서로 다른 기기 수)를 따로 센다. 카피 3개가 한 기기에 몰린 상태
(카피 수 3, 기기 수 1)와 3기기에 분산된 상태를 구분해야 heal 이전 대상을 고를 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 카피 종류: 내 청크를 담은 스토리지 소스 / 타 사용자 청크 보관소(ParityStore).
KIND_SOURCE = "source"
KIND_PARITY = "parity"


@dataclass(frozen=True)
class ChunkLocation:
    """청크 카피 한 벌의 위치. 카피 간 우열은 없다."""

    device_id: str          # 보관 기기(빈 문자열=이 기기/레거시)
    source_id: str          # 그 기기 안의 스토리지 소스
    chunk_ref: str | None = None   # 소스 내 물리 경로. ParityStore면 None
    kind: str = KIND_SOURCE        # source | parity

    @property
    def is_local_record(self) -> bool:
        """기기를 특정하지 않은(레거시) 위치인지. 이 기기 보관을 뜻한다."""
        return not self.device_id


def distinct_devices(locations: list[ChunkLocation]) -> int:
    """서로 다른 기기 수. 3카피가 한 기기에 몰린 상태를 구분하는 기준.

    device_id가 빈 문자열인 레거시 위치는 하나의 기기(이 기기)로 센다.
    """
    return len({loc.device_id for loc in locations})


def copies(locations: list[ChunkLocation]) -> int:
    """카피 수 = 서로 다른 위치 수. 원본을 따로 세지 않는다."""
    return len({(loc.device_id, loc.source_id) for loc in locations})


def has_location(
    locations: list[ChunkLocation], device_id: str, source_id: str
) -> bool:
    """그 위치에 이미 카피가 있는지(같은 소스에 두 번 두지 않기 위한 판정)."""
    return any(
        loc.device_id == device_id and loc.source_id == source_id
        for loc in locations
    )
