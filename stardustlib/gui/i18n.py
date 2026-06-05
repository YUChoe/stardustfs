"""GUI 국제화(i18n) — 한국어/영어.

get_text(lang)으로 현재 언어의 문자열 dict를 얻는다. 동적 값은 {name} 형식의
str.format 자리표시자를 사용한다. 사용자 대상 영어는 영국 영어 철자를 따른다.
"""

from __future__ import annotations

_KO: dict[str, str] = {
    "app_title": "StardustFS",
    "ready": "준비됨",
    "err": "오류",
    "need_config": "먼저 설정 파일을 선택하세요.",
    "select_config_hint": "설정 파일을 선택하세요 (새 설정... 또는 설정...).",
    "login_required": "로그인 후 파일을 볼 수 있습니다.",
    # 상단
    "new_config": "새 설정...",
    "choose_config": "설정...",
    "login": "로그인",
    "logout": "로그아웃",
    # daemon
    "daemon_unknown": "daemon: ?",
    "daemon_running": "daemon: 실행 중 (pid={pid})",
    "daemon_stale": "daemon: stale(비정상 종료?)",
    "daemon_stopped": "daemon: 미실행",
    "daemon_start": "daemon 시작",
    "daemon_stop": "daemon 정지",
    "daemon_starting": "daemon 자동 시작 중...",
    "daemon_started": "daemon 시작(pid={pid})",
    "daemon_stop_req": "daemon 정지 요청",
    "daemon_start_busy": "daemon 시작 중...",
    "daemon_stop_busy": "daemon 정지 중...",
    "devices": "디바이스",
    "storage": "스토리지",
    # 경로
    "up": "↑ 상위",
    "go": "이동",
    "refresh": "새로고침",
    # 컬럼
    "col_type": "종류",
    "col_name": "이름",
    "col_size": "크기",
    "col_owner": "소유자",
    "col_backup": "백업",
    "bk_done": "완료",
    "bk_pending": "대기",
    "bk_none": "—",
    # 툴바
    "upload": "업로드",
    "download": "다운로드",
    "mkdir": "새 폴더",
    "delete": "삭제",
    "move": "이동/이름변경",
    "copy": "복사",
    "backup_now": "백업",
    "heal_now": "복제 점검",
    # 상태
    "busy_browse": "조회 중...",
    "cap": "용량: 사용 {used} / 총 {total} (가용 {avail}) · 보류 {pending}",
    "backup_summary": "백업: 완료 {replicated} · 대기 {pending} · 미백업 {none}",
    "storage_status": "스토리지: 소스 {sources} · {used}/{total}",
    "device_status": "디바이스: {online}/{total} 온라인",
    "backup_busy": "백업 중...",
    "heal_busy": "복제 점검 중...",
    "backup_pick": "백업할 파일을 선택하세요.",
    "backup_done": "백업 완료: {ok}개 / 미완료(pending) {pending}개",
    "err_status": "오류: {msg}",
    # 업로드/다운로드
    "upload_pick": "업로드할 파일",
    "uploading": "업로드 중: {name}",
    "download_pick": "다운로드할 파일을 선택하세요.",
    "save_to": "저장 위치",
    "downloading": "다운로드 중: {name}",
    "download_done": "다운로드 완료: {path} ({size})",
    # mkdir/delete/move/copy
    "mkdir_prompt": "폴더 이름:",
    "mkdir_busy": "폴더 생성 중...",
    "delete_confirm": "'{name}'을(를) 삭제할까요?",
    "delete_confirm_many": "선택한 {count}개 항목을 삭제할까요?",
    "delete_busy": "삭제 중...",
    "move_prompt": "대상 가상 경로:",
    "move_busy": "이동 중...",
    "copy_pick": "복사할 파일을 선택하세요.",
    "copy_prompt": "대상 가상 경로:",
    "copy_busy": "복사 중...",
    # 디바이스 창
    "devices_title": "내 디바이스",
    "devices_busy": "디바이스 조회 중...",
    "online": "온라인",
    "offline": "오프라인",
    "this_device": "현재",
    # 스토리지 창
    "sources_title": "스토리지 소스",
    "src_add_dir": "디렉토리 추가",
    "src_add_loop": "루프백 추가",
    "src_remove": "제거",
    "close": "닫기",
    "src_pick_dir": "디렉토리 소스로 추가할 폴더",
    "src_loop_path": "루프백 이미지 경로",
    "src_loop_size_prompt": "크기(MB):",
    "src_remove_confirm": (
        "소스 '{id}'를 설정에서 제거할까요?\n물리 데이터는 삭제되지 않으나 해당 "
        "소스의 파일은 접근 불가가 되며, 실행 중인 daemon은 재시작해야 반영됩니다."
    ),
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
    "nc_key_title": "암호화 키",
    "nc_key_q": (
        "이 디바이스에서 새 암호화 키를 생성할까요?\n\n"
        "예 = 첫 디바이스(새 키 생성)\n아니오 = 기존 계정(로그인 후 서버 백업에서 복원)"
    ),
    "nc_busy": "설정 생성 중...",
    "nc_done_new": "설정과 새 키를 생성했습니다. 로그인 후 사용하세요.",
    "nc_done_restore": (
        "설정을 생성했습니다. '로그인'에서 키 백업 암호까지 입력하면 서버 백업에서 "
        "키가 복원됩니다."
    ),
    # 메뉴 / 트레이
    "menu_language": "언어",
    "lang_ko": "한국어",
    "lang_en": "English",
    "menu_theme": "테마",
    "theme_light": "라이트",
    "theme_dark": "다크",
    "menu_manage": "관리",
    "menu_file": "파일",
    "tray_open": "열기",
    "tray_quit": "종료",
    "tray_minimised": "트레이로 최소화되었습니다.",
    "tray_disabled_hint": "트레이 비활성(pystray 미설치): 창 닫기 = 종료. "
                          "설치: pip install -r requirements.txt",
}

_EN: dict[str, str] = {
    "app_title": "StardustFS",
    "ready": "Ready",
    "err": "Error",
    "need_config": "Select a config file first.",
    "select_config_hint": "Select a config file (New Config... or Open Config...).",
    "login_required": "Sign in to view your files.",
    "new_config": "New Config...",
    "choose_config": "Open Config...",
    "login": "Sign In",
    "logout": "Sign Out",
    "daemon_unknown": "daemon: ?",
    "daemon_running": "daemon: running (pid={pid})",
    "daemon_stale": "daemon: stale (crashed?)",
    "daemon_stopped": "daemon: stopped",
    "daemon_start": "Start daemon",
    "daemon_stop": "Stop daemon",
    "daemon_starting": "Auto-starting daemon...",
    "daemon_started": "daemon started (pid={pid})",
    "daemon_stop_req": "daemon stop requested",
    "daemon_start_busy": "Starting daemon...",
    "daemon_stop_busy": "Stopping daemon...",
    "devices": "Devices",
    "storage": "Storage",
    "up": "↑ Up",
    "go": "Go",
    "refresh": "Refresh",
    "col_type": "type",
    "col_name": "name",
    "col_size": "size",
    "col_owner": "owner",
    "col_backup": "backup",
    "bk_done": "backed up",
    "bk_pending": "pending",
    "bk_none": "—",
    "upload": "Upload",
    "download": "Download",
    "mkdir": "New Folder",
    "delete": "Delete",
    "move": "Move/Rename",
    "copy": "Copy",
    "backup_now": "Back up",
    "heal_now": "Check copies",
    "busy_browse": "Loading...",
    "cap": "Storage: {used} used / {total} total ({avail} free) · pending {pending}",
    "backup_summary": "Backup: done {replicated} · pending {pending} · none {none}",
    "storage_status": "Storage: {sources} sources · {used}/{total}",
    "device_status": "Devices: {online}/{total} online",
    "backup_busy": "Backing up...",
    "heal_busy": "Checking copies...",
    "backup_pick": "Select files to back up.",
    "backup_done": "Backed up: {ok} done / {pending} pending",
    "err_status": "Error: {msg}",
    "upload_pick": "File to upload",
    "uploading": "Uploading: {name}",
    "download_pick": "Select a file to download.",
    "save_to": "Save as",
    "downloading": "Downloading: {name}",
    "download_done": "Downloaded: {path} ({size})",
    "mkdir_prompt": "Folder name:",
    "mkdir_busy": "Creating folder...",
    "delete_confirm": "Delete '{name}'?",
    "delete_confirm_many": "Delete {count} selected items?",
    "delete_busy": "Deleting...",
    "move_prompt": "Destination virtual path:",
    "move_busy": "Moving...",
    "copy_pick": "Select a file to copy.",
    "copy_prompt": "Destination virtual path:",
    "copy_busy": "Copying...",
    "devices_title": "My Devices",
    "devices_busy": "Loading devices...",
    "online": "online",
    "offline": "offline",
    "this_device": "this",
    "sources_title": "Storage Sources",
    "src_add_dir": "Add Directory",
    "src_add_loop": "Add Loopback",
    "src_remove": "Remove",
    "close": "Close",
    "src_pick_dir": "Folder to add as a directory source",
    "src_loop_path": "Loopback image path",
    "src_loop_size_prompt": "Size (MB):",
    "src_remove_confirm": (
        "Remove source '{id}' from the config?\nThe physical data is not deleted, "
        "but its files become inaccessible, and a running daemon must be restarted."
    ),
    "login_email": "Email:",
    "login_password": "Password:",
    "login_keypw": "Master-key backup password (optional, leave blank to skip):",
    "login_busy": "Signing in...",
    "login_ok": "Signed in: {email}",
    "logout_busy": "Signing out...",
    "logout_ok": "Signed out",
    "nc_pick_dir": "Choose a config/storage folder (an empty folder is recommended)",
    "nc_server": "Server URL (leave blank for offline-only):",
    "nc_device": "Device name:",
    "nc_key_title": "Encryption key",
    "nc_key_q": (
        "Generate a new encryption key on this device?\n\n"
        "Yes = first device (create new key)\n"
        "No = existing account (restore from server backup after sign-in)"
    ),
    "nc_busy": "Creating config...",
    "nc_done_new": "Created the config and a new key. Sign in to continue.",
    "nc_done_restore": (
        "Config created. Sign in (including the key backup password) and the key "
        "will be restored from the server backup."
    ),
    "menu_language": "Language",
    "lang_ko": "한국어",
    "lang_en": "English",
    "menu_theme": "Theme",
    "theme_light": "Light",
    "theme_dark": "Dark",
    "menu_manage": "Manage",
    "menu_file": "File",
    "tray_open": "Open",
    "tray_quit": "Quit",
    "tray_minimised": "Minimised to tray.",
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
