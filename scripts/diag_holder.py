"""홀더 상태 릴레이 진단.

대상 device에 릴레이로 op를 보내 실제 상태를 확인한다.
- space: 홀더 스토리지 풀의 available/total (0이면 소스 미초기화 → 제공 용량 0)
- replica_store(1바이트): 패리티 쿼터 수용 여부(507이면 쿼터 초과/한도 0)

사용: python scripts/diag_holder.py <device_id>
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

CRED = ".dev-storage/metadata.db.credentials.json"
TIMEOUT = 40.0


def _load() -> tuple[str, str]:
    with open(CRED, encoding="utf-8") as f:
        cred = json.load(f)
    return cred["server_url"].rstrip("/"), cred["access_token"]


async def relay(url: str, token: str, dev: str, op: str, payload: dict) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        resp = await cli.post(
            f"{url}/relay/request",
            json={"target_device_id": dev, "op": op, "payload": payload},
            headers=headers,
        )
        if resp.status_code != 200:
            print(f"[{op}] 요청 적재 실패 HTTP {resp.status_code}: {resp.text}")
            return
        req_id = resp.json()["request_id"]
        resp = await cli.get(
            f"{url}/relay/response/{req_id}", headers=headers
        )
        body = resp.text
        if len(body) > 400:
            body = body[:400] + "...(생략)"
        print(f"[{op}] HTTP {resp.status_code}: {body}")


async def main() -> int:
    if len(sys.argv) < 2:
        print("사용: python scripts/diag_holder.py <device_id>")
        return 1
    dev = sys.argv[1]
    url, token = _load()
    print(f"server={url} target_device={dev}")
    await relay(url, token, dev, "space", {"auth_token": token})
    # 1바이트 시험 저장 — 쿼터 수용 여부만 확인(chunk_id는 진단 전용)
    await relay(
        url, token, dev, "replica_store",
        {"chunk_id": "diagnostic0000000000000000000001",
         "data": "AA==", "auth_token": token},
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
