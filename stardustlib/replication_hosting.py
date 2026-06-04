"""호스팅 용량 신고 (리플리케이션 운영 활성화).

device가 네트워크에 제공하는 용량(provided_bytes)을 중앙 서버에 신고한다. 서버는
이 값을 배치(placement)·호혜 회계의 기준으로 쓴다(가용 = provided*0.5 - hosted).
서버 미배포/도달 불가 시 조용히 실패하지 않고 False를 반환해 호출자가 경고하게 한다.
"""

from __future__ import annotations

import logging

import httpx

from stardustlib.auth_client import AuthClient
from stardustlib.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


async def report_hosting(
    auth_client: AuthClient,
    server_url: str,
    device_id: str,
    provided_bytes: int,
    *,
    timeout: float = 10.0,
) -> bool:
    """POST /replication/hosting로 제공 용량을 신고한다.

    성공 시 True. 인증/네트워크/HTTP 오류 시 False(경고는 호출자 책임).
    """
    try:
        token = await auth_client.get_valid_token()
    except AuthenticationError as e:
        logger.warning("호스팅 신고 인증 실패: %s", e)
        return False

    url = f"{server_url.rstrip('/')}/replication/hosting"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json={"device_id": device_id, "provided_bytes": provided_bytes},
                headers={"Authorization": f"Bearer {token}"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning("호스팅 신고 실패(서버 도달 불가): %s", e)
        return False

    if resp.status_code != 200:
        logger.warning("호스팅 신고 실패: HTTP %d", resp.status_code)
        return False
    return True


async def fetch_policy(
    auth_client: AuthClient, server_url: str, *, timeout: float = 10.0
) -> dict | None:
    """GET /replication/policy로 리플리케이션 정책을 내려받는다.

    {"reciprocity_fraction": float, "min_replicas": int} 또는 실패 시 None
    (인증/네트워크/미배포 — 호출자가 설정/기본값 사용).
    """
    try:
        token = await auth_client.get_valid_token()
    except AuthenticationError:
        return None

    url = f"{server_url.rstrip('/')}/replication/policy"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning("정책 조회 실패(서버 도달 불가): %s", e)
        return None

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        return {
            "reciprocity_fraction": float(data["reciprocity_fraction"]),
            "min_replicas": int(data["min_replicas"]),
        }
    except (ValueError, KeyError, TypeError):
        return None
