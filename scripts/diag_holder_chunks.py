"""홀더에 실제로 보관된 청크 존재 여부 확인 (고아 청크 진단).

릴레이 replica_fetch로 대상 파일의 앞쪽 청크들을 조회한다. 200이면 홀더 디스크에
저장돼 있는데 서버 replicas에는 없는 고아 청크(릴레이 타임아웃으로 실패 처리된 것),
404면 실제로 저장되지 않은 것이다.

사용: python scripts/diag_holder_chunks.py <device_id> <virtual_path> [n]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys

import httpx

CRED = ".dev-storage/metadata.db.credentials.json"
TIMEOUT = 40.0


def _load() -> tuple[str, str, str]:
    with open(CRED, encoding="utf-8") as f:
        cred = json.load(f)
    return cred["server_url"].rstrip("/"), cred["access_token"], cred["user_id"]


async def main() -> int:
    if len(sys.argv) < 3:
        print("사용: python scripts/diag_holder_chunks.py <device_id> <path> [n]")
        return 1
    dev, vpath = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    url, token, user_id = _load()
    file_ref = hashlib.sha256(f"{user_id}:{vpath}".encode("utf-8")).hexdigest()
    headers = {"Authorization": f"Bearer {token}"}
    print(f"file_ref={file_ref[:16]} target={dev} 청크 {n}개 조회")

    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        for idx in range(n):
            chunk_id = hashlib.sha256(
                f"{file_ref}:{idx}".encode("utf-8")
            ).hexdigest()
            resp = await cli.post(
                f"{url}/relay/request",
                json={
                    "target_device_id": dev,
                    "op": "replica_fetch",
                    "payload": {"chunk_id": chunk_id, "auth_token": token},
                },
                headers=headers,
            )
            if resp.status_code != 200:
                print(f"idx={idx} 적재 실패 HTTP {resp.status_code}")
                continue
            req_id = resp.json()["request_id"]
            resp = await cli.get(
                f"{url}/relay/response/{req_id}", headers=headers
            )
            try:
                data = resp.json()
                st = data.get("status")
                res = data.get("result", {})
                size = len(res.get("data", "")) * 3 // 4 if st == 200 else 0
                note = f"보관됨 ~{size}B" if st == 200 else res.get("error", "")
                print(f"idx={idx} chunk={chunk_id[:16]} status={st} {note}")
            except ValueError:
                print(f"idx={idx} 응답 파싱 실패 HTTP {resp.status_code}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
