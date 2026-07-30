"""GUI 백엔드 동작 (Tk 비의존) — 공개 표면.

구현은 도메인별 모듈에 있고 이 모듈은 GUI가 쓰는 이름만 모아 노출한다. GUI 코드는
`from stardustlib.gui import actions` 후 `actions.browse(...)` 형태로 호출한다.

- act_core: 세션 개방/캐시, 온라인 실행 래퍼, 목록 조회
- act_auth: 로그인/로그아웃
- act_storage: 설정 생성, 스토리지 소스 추가/포맷/분리
- act_inventory: 디바이스·소스 인벤토리
- act_replication: 백업/복구/heal, 진행 조회
- act_files: 전송과 파일 조작
- act_daemon: daemon 라이프사이클

모든 동작은 워커 스레드에서 호출된다. sqlite 연결은 스레드별이므로 코어 작업은 모두
같은 워커 스레드에서 수행되어야 한다.
"""

from __future__ import annotations

from stardustlib.gui.act_auth import account_email, is_logged_in, login, logout
from stardustlib.gui.act_core import (
    RemotePathExists,
    browse,
    invalidate,
    metadata_mtime,
)
from stardustlib.gui.act_daemon import (
    daemon_signal_reload,
    daemon_signal_stop,
    daemon_start,
    daemon_status,
)
from stardustlib.gui.act_files import (
    copy,
    get_file,
    mkdir,
    move,
    put_file,
    remove_many,
)
from stardustlib.gui.act_inventory import storage_and_devices, storage_overview
from stardustlib.gui.act_replication import (
    announce_paths,
    backup_paths,
    heal_paths,
    replica_counts,
    replication_progress,
    restore_paths,
)
from stardustlib.gui.act_storage import (
    add_source,
    create_config,
    create_storage_image,
    delete_storage_image,
    detach_source,
    list_sources,
    remove_source,
    storage_initializing,
)

__all__ = [
    "RemotePathExists",
    "account_email",
    "add_source",
    "announce_paths",
    "backup_paths",
    "browse",
    "copy",
    "create_config",
    "create_storage_image",
    "daemon_signal_reload",
    "daemon_signal_stop",
    "daemon_start",
    "daemon_status",
    "delete_storage_image",
    "detach_source",
    "get_file",
    "heal_paths",
    "invalidate",
    "is_logged_in",
    "list_sources",
    "login",
    "logout",
    "metadata_mtime",
    "mkdir",
    "move",
    "put_file",
    "remove_many",
    "remove_source",
    "replica_counts",
    "replication_progress",
    "restore_paths",
    "storage_and_devices",
    "storage_initializing",
    "storage_overview",
]
