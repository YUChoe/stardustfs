"""호스팅 사용량 보고 + 리플리케이션 정책 조회.

호스팅 상한은 서버가 정한다(프로비저닝). 클라이언트는 상한을 신고하지 않고 실제
사용량(보관 중인 타 사용자 청크 바이트 / 소스 총 용량)만 보고해 서버 회계가 실제와
어긋나는 것을 바로잡는다. 서버는 상한 − 사용량으로 배치 가용량을 구한다.
서버 미배포/도달 불가 시 조용히 실패하지 않고 False를 반환해 호출자가 경고하게 한다.
"""

from __future__ import annotations

import logging

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


async def report_usage(
    auth_client: AuthClient,
    server_url: str,
    device_id: str,
    hosted_bytes: int,
    total_bytes: int,
    *,
    timeout: float = 10.0,
) -> bool:
    """POST /replication/hosting로 이 기기의 실제 호스팅 사용량을 보고한다.

    hosted_bytes는 보관 중인 타 사용자 청크의 합계(ParityStore 집계), total_bytes는
    로컬 소스 총 용량이다. 서버는 hosted_bytes로 회계를 정렬해 배치 가용량
    (상한 − 사용량)을 실제와 맞춘다.

    성공 시 True. 인증/네트워크/HTTP 오류 시 False(경고는 호출자 책임).
    """
    try:
        token = await auth_client.get_valid_token()
    except AuthenticationError as e:
        logger.warning("호스팅 사용량 보고 인증 실패: %s", e)
        return False

    url = f"{server_url.rstrip('/')}/replication/hosting"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json={
                    "device_id": device_id,
                    "hosted_bytes": hosted_bytes,
                    "total_bytes": total_bytes,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning("호스팅 사용량 보고 실패(서버 도달 불가): %s", e)
        return False

    if resp.status_code != 200:
        logger.warning("호스팅 사용량 보고 실패: HTTP %d", resp.status_code)
        return False
    return True


async def fetch_policy(
    auth_client: AuthClient, server_url: str, *,
    device_id: str | None = None, timeout: float = 10.0,
) -> dict | None:
    """GET /replication/policy로 리플리케이션·전송 정책을 내려받는다(프로비저닝).

    device_id를 주면 그 기기의 호스팅 할당량(`hosting_quota_bytes`)을 받는다. 없으면
    서버 기본 할당량이 온다.

    {"p2p_enabled": bool, "hosting_enabled": bool, "target_copies": int,
    "hosting_quota_bytes": int | None}
    또는 실패 시 None(인증/네트워크/미배포 — 호출자가 직전 값/기본값 사용).

    구버전 서버는 새 필드를 반환하지 않으므로 기본값으로 채운다. 할당량 기본값은
    None으로 두어 "서버가 알려주지 않았다"와 "0(호스팅 금지)"를 구분한다.
    폐기 필드(reciprocity_fraction·min_replicas)는 읽지 않는다.
    """
    try:
        token = await auth_client.get_valid_token()
    except AuthenticationError:
        return None

    url = f"{server_url.rstrip('/')}/replication/policy"
    params = {"device_id": device_id} if device_id else None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning("정책 조회 실패(서버 도달 불가): %s", e)
        return None

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        quota = data.get("hosting_quota_bytes")
        return {
            # 구버전 서버는 미반환 → 허용(기본값)으로 간주
            "p2p_enabled": bool(data.get("p2p_enabled", True)),
            "hosting_enabled": bool(data.get("hosting_enabled", True)),
            "target_copies": int(data.get("target_copies", 3)),
            "hosting_quota_bytes": None if quota is None else int(quota),
        }
    except (ValueError, KeyError, TypeError):
        return None
