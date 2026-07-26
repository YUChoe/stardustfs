"""리플리케이션 배치/쿼터 진단.

서버 /replication/policy와 /replication/placement를 실제 토큰으로 호출해
- 어떤 홀더가 후보로 뽑히는지
- 서버 회계 기준 홀더의 잔여 용량(size 이진 탐색)
를 확인한다. 클라이언트 로컬 파일의 청크 목록·복제 등록 현황도 함께 출력한다.

사용: python scripts/diag_placement.py [virtual_path]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys

import httpx

CRED = ".dev-storage/metadata.db.credentials.json"
DAEMON = ".dev-storage/metadata.db.daemon.json"
CHUNK = 4 * 1024 * 1024


def _load() -> tuple[str, str, str]:
    with open(CRED, encoding="utf-8") as f:
        cred = json.load(f)
    return cred["server_url"].rstrip("/"), cred["access_token"], cred["user_id"]


def _self_device() -> str | None:
    """자기 device_id. 인자 --self=<id>가 있으면 그것을, 없으면 daemon.json에서."""
    for arg in sys.argv[1:]:
        if arg.startswith("--self="):
            return arg.split("=", 1)[1]
    try:
        with open(DAEMON, encoding="utf-8") as f:
            return json.load(f).get("device_id")
    except (OSError, ValueError):
        return None


async def main() -> int:
    url, token, user_id = _load()
    self_dev = _self_device()
    headers = {"Authorization": f"Bearer {token}"}
    exclude = [self_dev] if self_dev else []
    print(f"server={url} user={user_id[:8]} self_device={self_dev}")

    async with httpx.AsyncClient(timeout=20.0) as cli:
        resp = await cli.get(f"{url}/replication/policy", headers=headers)
        print(f"\n[policy] HTTP {resp.status_code}: {resp.text}")

        # 청크 1개 크기로 후보 조회
        resp = await cli.post(
            f"{url}/replication/placement",
            json={"size": CHUNK, "count": 5, "exclude": exclude},
            headers=headers,
        )
        print(f"\n[placement size=4MiB count=5] HTTP {resp.status_code}: {resp.text}")

        # 서버 회계 기준 최대 잔여량 이진 탐색 (후보가 나오는 최대 size)
        lo, hi = 0, 64 * 1024 * 1024 * 1024
        found_any = False
        for _ in range(40):
            mid = (lo + hi) // 2
            r = await cli.post(
                f"{url}/replication/placement",
                json={"size": mid, "count": 1, "exclude": exclude},
                headers=headers,
            )
            holders = r.json().get("holders", []) if r.status_code == 200 else []
            if holders:
                found_any = True
                lo = mid
            else:
                hi = mid
            if hi - lo <= 1:
                break
        if found_any:
            print(f"\n[서버 회계] 최대 후보 배치 가능 크기 = {lo} bytes "
                  f"({lo / 1024 / 1024:.1f} MiB)")
        else:
            print("\n[서버 회계] 어떤 크기로도 후보 홀더가 없습니다")

        # 대상 파일의 청크 등록/복제 현황
        vpath = next(
            (a for a in sys.argv[1:] if not a.startswith("--")), None
        )
        if vpath:
            file_ref = hashlib.sha256(
                f"{user_id}:{vpath}".encode("utf-8")
            ).hexdigest()
            r = await cli.get(
                f"{url}/replication/chunks/{file_ref}", headers=headers
            )
            chunks = r.json() if r.status_code == 200 else []
            print(f"\n[chunks] {vpath} file_ref={file_ref[:16]} "
                  f"count={len(chunks)} total={sum(c['size'] for c in chunks)}")
            placed = 0
            for c in chunks[:2000]:
                rr = await cli.get(
                    f"{url}/replication/replicas/{c['chunk_id']}",
                    headers=headers,
                )
                if rr.status_code == 200 and rr.json():
                    placed += 1
            print(f"[replicas] 복제본이 등록된 청크 수: {placed}/{len(chunks)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
