"""메타데이터 DB 스키마 덤프 (files/chunks의 device 관련 컬럼 확인용).

사용: python scripts/dump_schema.py [db_path]
"""

from __future__ import annotations

import sqlite3
import sys

DEFAULT_DB = ".dev-storage/metadata.db"
TABLES = ("files", "chunks")


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    for table in TABLES:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            print(f"[{table}] 없음")
            continue
        cols = ", ".join(f"{r['name']}:{r['type']}" for r in rows)
        print(f"[{table}] {cols}")
    # device_id 분포 (누가 무엇을 들고 있는지)
    try:
        rows = conn.execute(
            "SELECT device_id, COUNT(*) n FROM files WHERE deleted = 0 "
            "GROUP BY device_id"
        ).fetchall()
        print("\nfiles.device_id 분포:",
              {(r["device_id"] or "NULL"): r["n"] for r in rows})
    except sqlite3.OperationalError as e:
        print("files 집계 불가:", e)
    try:
        rows = conn.execute(
            "SELECT device_id, COUNT(*) n FROM chunks GROUP BY device_id"
        ).fetchall()
        print("chunks.device_id 분포:",
              {(r["device_id"] or "NULL"): r["n"] for r in rows})
    except sqlite3.OperationalError as e:
        print("chunks 집계 불가:", e)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
