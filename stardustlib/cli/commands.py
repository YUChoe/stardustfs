"""CLI 명령 구현.

각 명령은 (session, args) -> int(종료 코드) 시그니처를 따른다.
df/ls/status는 오프라인(로컬 코어)으로 동작하고, devices는 온라인 세션이 필요하다.
전송 계열(put/get) 등은 후속 Phase.
"""

from __future__ import annotations

from stardustlib.cli.format import print_json, print_table


def _join(base: str, name: str) -> str:
    """디렉토리 경로와 엔트리 이름을 가상 경로로 결합한다."""
    return base.rstrip("/") + "/" + name


def _short(device_id: str | None) -> str:
    """device_id를 표시용으로 축약한다 (앞 8자)."""
    if not device_id:
        return "-"
    return device_id[:8]


def cmd_df(session, args) -> int:
    """총/사용/가용 용량을 출력한다 (로컬 소스 합산)."""
    total = session.jbod.get_total_space()
    available = session.jbod.get_available_space()
    used = total - available

    if args.json:
        print_json({"total": total, "used": used, "available": available})
    else:
        print_table(
            [[total, used, available]], ["total", "used", "available"]
        )
    return 0


def cmd_ls(session, args) -> int:
    """가상 경로의 디렉토리 목록을 출력한다.

    파일은 소유 device_id를 함께 표시한다(로컬 메타데이터 기준). 온라인 세션이면
    self device는 'this'로 표시한다.
    """
    base = args.path
    entries = session.jbod.list_directory(base)
    self_id = session.self_device_id

    def owner_of(name: str) -> str | None:
        meta = session.metadata.lookup(_join(base, name))
        return meta.device_id if meta is not None else None

    if args.json:
        out = []
        for e in entries:
            item = {
                "name": e.name,
                "is_directory": e.is_directory,
                "file_size": e.file_size,
                "modified_at": e.modified_at,
            }
            if not e.is_directory:
                item["device_id"] = owner_of(e.name)
            out.append(item)
        print_json(out)
        return 0

    rows = []
    for e in entries:
        if e.is_directory:
            owner = ""
        else:
            did = owner_of(e.name)
            owner = "this" if self_id and did == self_id else _short(did)
        rows.append(
            ["d" if e.is_directory else "-", e.file_size, owner, e.name]
        )
    print_table(rows, ["type", "size", "owner", "name"])
    return 0


def cmd_status(session, args) -> int:
    """동기화 상태를 출력한다 (보류 변경 수 등, 로컬 기준)."""
    pending = session.metadata.get_pending_files()
    total = len(session.jbod.list_directory("/"))

    if args.json:
        print_json(
            {
                "pending": len(pending),
                "root_entries": total,
                "self_device_id": session.self_device_id,
                "online": session.online,
            }
        )
    else:
        print_table(
            [[len(pending), total, session.online]],
            ["pending", "root_entries", "online"],
        )
    return 0


def cmd_devices(session, args) -> int:
    """내 계정에 등록된 device 목록과 online 여부를 출력한다 (온라인 필요)."""
    if not session.online or session.my_devices is None:
        # graceful skip 금지 — 규격 오류로 반환
        print_table([], ["id", "name", "online"])
        return 1

    self_id = session.self_device_id

    if args.json:
        print_json(
            [
                {
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "is_online": d.get("is_online"),
                    "self": d.get("id") == self_id,
                }
                for d in session.my_devices
            ]
        )
        return 0

    rows = [
        [
            _short(d.get("id")),
            d.get("name"),
            "online" if d.get("is_online") else "offline",
            "this" if d.get("id") == self_id else "",
        ]
        for d in session.my_devices
    ]
    print_table(rows, ["id", "name", "online", "self"])
    return 0
