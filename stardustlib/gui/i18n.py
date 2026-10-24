"""GUI 국제화(i18n) — 한국어/영어.

get_text(lang)으로 현재 언어의 문자열 dict를 얻는다. 동적 값은 {name} 형식의
str.format 자리표시자를 사용한다. 사용자 대상 영어는 영국 영어 철자를 따르고,
버튼·메뉴·상태값은 문장형 대소문자(sentence case)로 통일한다.
"""

from __future__ import annotations

_KO: dict[str, str] = {
    "app_title": "StardustFS",
    "ready": "준비됨",
    "need_config": "먼저 설정 파일을 선택하세요.",
    "select_config_hint": "설정 파일을 선택하세요 (새 설정... 또는 설정...).",
    "login_required": "로그인 후 파일을 볼 수 있습니다.",
    "empty_folder": "이 폴더는 비어 있습니다.",
    "empty_folder_drop": "이 폴더는 비어 있습니다.\n파일을 끌어다 놓으면 업로드합니다.",
    # 상단
    "new_config": "새 설정...",
    "choose_config": "설정...",
    "login": "로그인",
    "logout": "로그아웃",
    # 폼 다이얼로그
    "form_ok": "확인",
    "form_cancel": "취소",
    "form_browse": "찾기",
    "form_required": "'{label}'을(를) 입력하세요.",
    "form_int": "'{label}'은(는) 숫자여야 합니다.",
    "form_min": "'{label}'은(는) {minimum} 이상이어야 합니다.",
    # 폴더 선택
    "pick_folder": "폴더 선택",
    "pick_here": "여기로",
    # 배너
    "banner_repeat": " ({count}회)",
    # 데몬
    "daemon_unknown": "데몬: ?",
    "daemon_running_short": "데몬: 실행 중",
    "daemon_running": "데몬: 실행 중 (pid={pid})",
    "daemon_stale": "데몬: 응답 없음(비정상 종료?)",
    "daemon_stopped": "데몬: 미실행",
    "daemon_starting": "데몬 자동 시작 중...",
    "daemon_restart": "소스 변경 반영 — 데몬 리로드 중...",
    "daemon_start_busy": "데몬 시작 중...",
    # 경로
    "up": "↑ 상위",
    "refresh": "새로고침",
    # 컬럼
    "col_name": "이름",
    "col_size": "크기",
    "col_backup": "백업",
    "bk_done": "✓ 완료",
    "bk_pending": "○ 대기",
    "bk_none": "—",
    # 툴바
    "upload": "업로드",
    "download": "다운로드",
    "mkdir": "새 폴더",
    "delete": "삭제",
    "rename": "이름 변경",
    "move": "이동",
    "copy": "복사",
    "backup_now": "백업",
    "heal_now": "복제본 보충",
    "restore_now": "복원",
    "upload_cancel": "업로드 취소",
    "upload_clear": "완료 항목 정리",
    "toolbar_more": "더보기",
    # 툴팁(동작 설명)
    "tip_upload": "파일을 골라 현재 폴더에 올립니다. 목록에 끌어다 놓아도 됩니다.",
    "tip_download": "선택한 파일을 이 기기에 내려받습니다.",
    "tip_mkdir": "현재 폴더 안에 새 폴더를 만듭니다.",
    "tip_rename": "현재 폴더에서 이름만 바꿉니다.",
    "tip_move": "선택 항목을 다른 폴더로 옮깁니다.",
    "tip_copy": "선택 파일을 다른 폴더로 복사합니다.",
    "tip_delete": "선택 항목을 삭제합니다(되돌릴 수 없습니다).",
    "tip_backup_now": "선택 파일의 청크를 다른 기기에 복제해 둡니다.",
    "tip_heal_now": "복제본이 모자란 청크를 다른 기기에 더 만듭니다.",
    "tip_restore_now": "다른 기기의 복제본에서 파일을 되살립니다.",
    "tip_ctx_announce": "데몬에 지금 백업하라고 알립니다(주기를 기다리지 않음).",
    "tip_upload_cancel": "아직 시작하지 않은 업로드를 취소합니다.",
    "tip_upload_clear": "실패했거나 건너뛴 업로드 행을 목록에서 지웁니다.",
    # 툴팁(비활성 사유)
    "need_any": "항목을 하나 이상 선택하세요.",
    "need_one": "항목을 하나만 선택하세요.",
    "need_one_file": "파일을 하나만 선택하세요.",
    "need_files": "파일을 하나 이상 선택하세요.",
    # 상태
    "busy_browse": "조회 중...",
    "backup_summary": "백업 ✓{replicated} ○{pending} —{none}",
    "backup_summary_tip": "백업: 완료 {replicated} · 대기 {pending} · 미백업 {none}",
    "storage_status": "스토리지: {used}/{total}",
    "storage_status_tip": "로컬 소스 {sources}개 · 사용 {used} / 전체 {total}",
    "device_status": "디바이스: {online}/{total} 온라인",
    "backup_busy": "백업 중...",
    "heal_busy": "복제본 보충 중...",
    "restore_busy": "복원 중...",
    "backup_done": "백업 완료: {ok}개 / 미완료 {pending}개",
    "backup_delegated": " · 다른 기기에 위임 {count}건",
    "backup_progress": "백업 중: {name} {done}/{total} 청크",
    "backup_progress_reading": "백업 준비 중: {name} {done}/{total} 청크 읽는 중",
    "backup_delegate_offline": " · 위임 실패(오프라인): {devices}",
    "restore_done": "복원 완료: {ok}개 / 실패 {failed}개",
    # 즉시 백업 요청(announce)
    "ctx_announce": "지금 백업 요청",
    "announce_busy": "백업 요청 중...",
    "announce_done": "백업 요청 완료: {count}개 (데몬이 곧 처리)",
    "announce_no_daemon": "데몬이 실행 중이 아닙니다. 백업 요청을 보낼 수 없습니다.",
    "err_status": "오류: {msg}",
    # 오류(사용자용 문구)
    "err_login": "로그인이 필요합니다. 먼저 로그인하세요.",
    "err_auth": "로그인하지 못했습니다. 이메일과 비밀번호를 확인하세요.",
    "err_network": "서버에 연결하지 못했습니다. 네트워크를 확인하세요.",
    "err_server": "서버 오류가 발생했습니다. 잠시 후 다시 시도하세요.",
    "err_no_server": "설정에 서버 URL이 없습니다.",
    "err_not_found": "찾을 수 없습니다: {msg}",
    "err_exists": "이미 있습니다: {msg}",
    "err_permission": "권한이 없습니다: {msg}",
    "err_no_space": "저장 공간이 부족합니다.",
    "err_timeout": "응답 시간이 초과되었습니다.",
    "err_unexpected": "예기치 않은 오류: {msg}",
    # 업로드/다운로드
    "upload_pick": "업로드할 파일",
    "save_to": "저장 위치",
    "downloading": "다운로드 중: {name}",
    "download_done": "다운로드 완료: {path} ({size})",
    # 업로드 진행(파일 목록 행 + 상태바)
    "upload_status": "업로드 중 {i}/{n}: {name}",
    "upload_all_done": "업로드 완료: 성공 {ok} / 실패 {fail} / 건너뜀 {skip}",
    "upload_cancelled": "업로드 취소: 남은 {n}개 건너뜀",
    "upload_fail_banner": "업로드 실패 {count}개 — 마지막: {name}: {msg}",
    "upload_drop_dirs": "폴더는 업로드할 수 없습니다. 파일만 끌어다 놓으세요.",
    "up_state_queued": "대기 중",
    "up_state_running": "업로드 중",
    "up_state_failed": "실패",
    "up_state_exists": "이미 있음",
    # 같은 이름 충돌
    "conflict_title": "같은 이름의 파일",
    "conflict_msg": "{count}개 파일이 대상 폴더에 이미 있습니다. 어떻게 할까요?",
    "conflict_overwrite": "덮어쓰기",
    "conflict_rename": "둘 다 유지",
    "conflict_skip": "건너뛰기",
    # mkdir/delete/rename/move/copy
    "mkdir_prompt": "폴더 이름:",
    "mkdir_busy": "폴더 생성 중...",
    "delete_confirm": "'{name}'을(를) 삭제할까요?",
    "delete_confirm_many": "선택한 {count}개 항목을 삭제할까요?",
    "delete_busy": "삭제 중...",
    "rename_prompt": "새 이름:",
    "rename_busy": "이름 변경 중...",
    "move_busy": "이동 중...",
    "copy_busy": "복사 중...",
    "copy_prefix": "복사본-",
    # 스토리지·디바이스 패널
    "this_device": "이 기기",
    "src_add_loop": "루프백 추가",
    "src_detach": "분리",
    "src_loop_path": "루프백 이미지 경로",
    "src_loop_size_prompt": "크기(MB):",
    "src_detach_confirm": (
        "소스 '{id}'를 분리할까요?\n그 소스의 파일을 남은 로컬 소스로 모두 이동한 "
        "뒤에야 분리됩니다(.img는 삭제되지 않음). 데몬은 정지 상태를 권장합니다."
    ),
    "src_detach_busy": "분리 준비(파일 이동) 중...",
    "src_detach_done": "분리 완료: {moved}개 파일 이동",
    "src_detach_blocked": (
        "분리 보류: {unmoved}개 파일을 옮길 곳이 없습니다(로컬 용량 부족). "
        "용량 확보 후 다시 시도하세요."
    ),
    "src_remote_no_detach": (
        "리모트(다른 디바이스) 스토리지는 여기서 분리할 수 없습니다. "
        "해당 디바이스에서 관리하세요."
    ),
    "col_device": "디바이스",
    "mgmt_panel_title": "스토리지 · 디바이스",
    "mgmt_empty_config": "설정 파일을 선택하면 스토리지가 표시됩니다.",
    "mgmt_empty_none": "표시할 스토리지가 없습니다. '루프백 추가'로 만드세요.",
    "mgmt_empty_failed": "스토리지 정보를 불러오지 못했습니다.",
    "col_src_name": "이름",
    "col_status": "상태",
    "col_capacity": "용량",
    "status_online": "온라인",
    "status_offline": "오프라인",
    "src_ready": "준비됨",
    "src_initializing": "초기화 중",
    "src_kind_loopback": "루프백",
    "src_kind_directory": "디렉토리",
    "src_init_busy": "스토리지 초기화 중...",
    "transfer_init_block": "스토리지 초기화 중입니다. 잠시 후 다시 시도하세요.",
    # 로그인
    "login_email": "이메일:",
    "login_password": "비밀번호:",
    "login_keypw": "마스터키 백업 암호(선택, 없으면 비움):",
    "login_busy": "로그인 중...",
    "login_ok": "로그인 성공: {email}",
    "logout_busy": "로그아웃 중...",
    "logout_ok": "로그아웃 완료",
    # 새 설정
    "nc_pick_dir": "설정/저장 폴더 선택 (비어 있는 폴더 권장)",
    "nc_server": "서버 URL (비우면 오프라인 전용):",
    "nc_device": "디바이스 이름:",
    "nc_key_q_short": "이 기기에서 새 암호화 키 생성(첫 기기면 체크, "
                      "기존 계정이면 해제)",
    "nc_busy": "설정 생성 중...",
    "nc_done_new": "설정과 새 키를 생성했습니다. 로그인 후 사용하세요.",
    "nc_done_restore": (
        "설정을 생성했습니다. '로그인'에서 키 백업 암호까지 입력하면 서버 백업에서 "
        "키가 복원됩니다."
    ),
    "config_filetype": "설정 파일 (JSON)",
    "all_files": "모든 파일",
    # 메뉴 / 트레이
    "menu_language": "언어",
    "lang_ko": "한국어",
    "lang_en": "English",
    "menu_theme": "테마",
    "theme_light": "라이트",
    "theme_dark": "다크",
    "menu_file": "파일",
    "tray_open": "열기",
    "tray_quit": "종료",
    "tray_minimised": "트레이로 최소화되었습니다.",
    "tray_hint": "창을 닫아도 종료되지 않고 트레이에서 계속 실행됩니다. "
                 "완전히 끄려면 트레이 아이콘에서 '종료'를 고르세요.",
    "tray_disabled_hint": "트레이 비활성(pystray 미설치): 창 닫기 = 종료. "
                          "설치: pip install -r requirements.txt",
}

_EN: dict[str, str] = {
    "app_title": "StardustFS",
    "ready": "Ready",
    "need_config": "Select a config file first.",
    "select_config_hint": "Select a config file (New config… or Open config…).",
    "login_required": "Sign in to view your files.",
    "empty_folder": "This folder is empty.",
    "empty_folder_drop": "This folder is empty.\nDrop files here to upload them.",
    "new_config": "New config…",
    "choose_config": "Open config…",
    "login": "Sign in",
    "logout": "Sign out",
    "form_ok": "OK",
    "form_cancel": "Cancel",
    "form_browse": "Browse",
    "form_required": "Please enter '{label}'.",
    "form_int": "'{label}' must be a number.",
    "form_min": "'{label}' must be at least {minimum}.",
    "pick_folder": "Choose folder",
    "pick_here": "Choose",
    "banner_repeat": " ({count}×)",
    "daemon_unknown": "Daemon: ?",
    "daemon_running_short": "Daemon: running",
    "daemon_running": "Daemon: running (pid={pid})",
    "daemon_stale": "Daemon: not responding (crashed?)",
    "daemon_stopped": "Daemon: stopped",
    "daemon_starting": "Starting the daemon…",
    "daemon_restart": "Applying source change — reloading the daemon…",
    "daemon_start_busy": "Starting the daemon…",
    "up": "↑ Up",
    "refresh": "Refresh",
    "col_name": "Name",
    "col_size": "Size",
    "col_backup": "Backup",
    "bk_done": "✓ Backed up",
    "bk_pending": "○ Pending",
    "bk_none": "—",
    "upload": "Upload",
    "download": "Download",
    "mkdir": "New folder",
    "delete": "Delete",
    "rename": "Rename",
    "move": "Move",
    "copy": "Copy",
    "backup_now": "Back up",
    "heal_now": "Top up copies",
    "restore_now": "Restore",
    "upload_cancel": "Cancel uploads",
    "upload_clear": "Clear finished",
    "toolbar_more": "More",
    "tip_upload": "Choose files to upload into the current folder. You can also "
                  "drop them onto the list.",
    "tip_download": "Download the selected file to this device.",
    "tip_mkdir": "Create a folder inside the current folder.",
    "tip_rename": "Rename within the current folder.",
    "tip_move": "Move the selection to another folder.",
    "tip_copy": "Copy the selected file to another folder.",
    "tip_delete": "Delete the selection (this cannot be undone).",
    "tip_backup_now": "Replicate the selected files' chunks to other devices.",
    "tip_heal_now": "Make extra copies of chunks that have too few on other devices.",
    "tip_restore_now": "Rebuild the file from copies held by other devices.",
    "tip_ctx_announce": "Tell the daemon to back up now, without waiting for its "
                        "next cycle.",
    "tip_upload_cancel": "Cancel uploads that have not started yet.",
    "tip_upload_clear": "Remove failed and skipped upload rows from the list.",
    "need_any": "Select at least one item.",
    "need_one": "Select exactly one item.",
    "need_one_file": "Select exactly one file.",
    "need_files": "Select at least one file.",
    "busy_browse": "Loading…",
    "backup_summary": "Backup ✓{replicated} ○{pending} —{none}",
    "backup_summary_tip": "Backup: {replicated} backed up · {pending} pending · "
                          "{none} not backed up",
    "storage_status": "Storage: {used}/{total}",
    "storage_status_tip": "{sources} local source(s) · {used} used of {total}",
    "device_status": "Devices: {online}/{total} online",
    "backup_busy": "Backing up…",
    "heal_busy": "Topping up copies…",
    "restore_busy": "Restoring…",
    "backup_done": "Backed up: {ok} done / {pending} pending",
    "backup_delegated": " · delegated to {count} device(s)",
    "backup_progress": "Backing up: {name} {done}/{total} chunks",
    "backup_progress_reading": "Preparing backup: {name} reading {done}/{total} chunks",
    "backup_delegate_offline": " · delegation failed (offline): {devices}",
    "restore_done": "Restored: {ok} done / {failed} failed",
    "ctx_announce": "Back up now",
    "announce_busy": "Requesting backup…",
    "announce_done": "Backup requested: {count} file(s), the daemon will handle it shortly",
    "announce_no_daemon": "The daemon is not running, so the backup request could not be sent.",
    "err_status": "Error: {msg}",
    "err_login": "You need to sign in first.",
    "err_auth": "Couldn't sign in. Check your email and password.",
    "err_network": "Couldn't reach the server. Check your connection.",
    "err_server": "The server returned an error. Please try again shortly.",
    "err_no_server": "No server URL is set in the config.",
    "err_not_found": "Not found: {msg}",
    "err_exists": "Already exists: {msg}",
    "err_permission": "Permission denied: {msg}",
    "err_no_space": "Not enough storage space.",
    "err_timeout": "The operation timed out.",
    "err_unexpected": "Unexpected error: {msg}",
    "upload_pick": "Files to upload",
    "save_to": "Save as",
    "downloading": "Downloading: {name}",
    "download_done": "Downloaded: {path} ({size})",
    "upload_status": "Uploading {i}/{n}: {name}",
    "upload_all_done": "Upload finished: {ok} succeeded, {fail} failed, "
                       "{skip} skipped",
    "upload_cancelled": "Uploads cancelled: {n} remaining skipped",
    "upload_fail_banner": "{count} upload(s) failed — last: {name}: {msg}",
    "upload_drop_dirs": "Folders can't be uploaded — drop files only.",
    "up_state_queued": "Queued",
    "up_state_running": "Uploading",
    "up_state_failed": "Failed",
    "up_state_exists": "Already exists",
    "conflict_title": "Files already exist",
    "conflict_msg": "{count} file(s) already exist in the destination folder. "
                    "What would you like to do?",
    "conflict_overwrite": "Replace",
    "conflict_rename": "Keep both",
    "conflict_skip": "Skip",
    "mkdir_prompt": "Folder name:",
    "mkdir_busy": "Creating folder…",
    "delete_confirm": "Delete '{name}'?",
    "delete_confirm_many": "Delete {count} selected items?",
    "delete_busy": "Deleting…",
    "rename_prompt": "New name:",
    "rename_busy": "Renaming…",
    "move_busy": "Moving…",
    "copy_busy": "Copying…",
    "copy_prefix": "copy-",
    "this_device": "this device",
    "src_add_loop": "Add loopback",
    "src_detach": "Detach",
    "src_loop_path": "Loopback image path",
    "src_loop_size_prompt": "Size (MB):",
    "src_detach_confirm": (
        "Detach source '{id}'?\nAll its files are first moved to remaining local "
        "sources, then the source is detached (the .img is not deleted). Stopping "
        "the daemon first is recommended."
    ),
    "src_detach_busy": "Preparing to detach (moving files)…",
    "src_detach_done": "Detached: {moved} files moved",
    "src_detach_blocked": (
        "Detach postponed: {unmoved} files have nowhere to go (not enough local "
        "space). Free up some space and try again."
    ),
    "src_remote_no_detach": (
        "Remote (other device) storage cannot be detached here. "
        "Manage it on that device."
    ),
    "col_device": "Device",
    "mgmt_panel_title": "Storage · Devices",
    "mgmt_empty_config": "Select a config file to see your storage.",
    "mgmt_empty_none": "No storage yet — use 'Add loopback' to create some.",
    "mgmt_empty_failed": "Couldn't load storage information.",
    "col_src_name": "Name",
    "col_status": "Status",
    "col_capacity": "Capacity",
    "status_online": "Online",
    "status_offline": "Offline",
    "src_ready": "Ready",
    "src_initializing": "Initialising",
    "src_kind_loopback": "Loopback",
    "src_kind_directory": "Directory",
    "src_init_busy": "Initialising storage…",
    "transfer_init_block": "Storage is initialising. Please try again shortly.",
    "login_email": "Email:",
    "login_password": "Password:",
    "login_keypw": "Master-key backup password (optional, leave blank to skip):",
    "login_busy": "Signing in…",
    "login_ok": "Signed in: {email}",
    "logout_busy": "Signing out…",
    "logout_ok": "Signed out",
    "nc_pick_dir": "Choose a config/storage folder (an empty folder is recommended)",
    "nc_server": "Server URL (leave blank for offline-only):",
    "nc_device": "Device name:",
    "nc_key_q_short": "Generate a new encryption key on this device (tick for "
                      "your first device, untick for an existing account)",
    "nc_busy": "Creating config…",
    "nc_done_new": "Created the config and a new key. Sign in to continue.",
    "nc_done_restore": (
        "Config created. Sign in (including the key backup password) and the key "
        "will be restored from the server backup."
    ),
    "config_filetype": "Config file (JSON)",
    "all_files": "All files",
    "menu_language": "Language",
    "lang_ko": "한국어",
    "lang_en": "English",
    "menu_theme": "Theme",
    "theme_light": "Light",
    "theme_dark": "Dark",
    "menu_file": "File",
    "tray_open": "Open",
    "tray_quit": "Quit",
    "tray_minimised": "Minimised to the tray.",
    "tray_hint": "Closing the window does not quit — StardustFS keeps running in "
                 "the tray. Choose 'Quit' on the tray icon to stop it.",
    "tray_disabled_hint": "Tray disabled (pystray not installed): closing the window "
                          "quits. Install: pip install -r requirements.txt",
}

TRANSLATIONS: dict[str, dict[str, str]] = {"ko": _KO, "en": _EN}
SUPPORTED_LANGS = set(TRANSLATIONS)
DEFAULT_LANG = "ko"


def detect_lang() -> str:
    """시스템 로케일에서 기본 언어를 추정한다(미지원이면 DEFAULT_LANG)."""
    try:
        import locale
        code = (locale.getdefaultlocale()[0] or "").lower()
    except Exception:  # noqa: BLE001
        return DEFAULT_LANG
    if code.startswith("ko"):
        return "ko"
    if code.startswith("en"):
        return "en"
    return DEFAULT_LANG


def get_text(lang: str) -> dict[str, str]:
    """지정 언어의 문자열 dict 반환(미지원이면 DEFAULT_LANG)."""
    return TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
