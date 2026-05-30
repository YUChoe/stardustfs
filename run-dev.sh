#!/bin/bash
# StardustFS 개발용 실행 스크립트
# 10MB 루프백 파일 3개를 사용하는 WebDAV 서버를 시작한다.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STORAGE_DIR="$SCRIPT_DIR/.dev-storage"
KEY_FILE="$STORAGE_DIR/master.key"

# 스토리지 디렉토리 생성
mkdir -p "$STORAGE_DIR"

# 키 파일이 없으면 생성 (오프라인 전용 모드일 때만)
# v2 설정에서 server.url이 있으면 서버에서 key를 복원하므로 여기서 생성하지 않음
if [ ! -f "$KEY_FILE" ]; then
    # dev-config.json에서 server.url 확인
    SERVER_URL=$(python -c "
import json
try:
    with open('dev-config.json') as f:
        cfg = json.load(f)
    url = cfg.get('server', {}).get('url')
    print(url or '')
except:
    print('')
" 2>/dev/null)

    if [ -z "$SERVER_URL" ]; then
        echo "암호화 키 생성 중..."
        python -c "import os, sys; sys.stdout.buffer.write(os.urandom(32))" > "$KEY_FILE"
        echo "키 생성 완료: $KEY_FILE"
    else
        echo "서버 모드: key_file은 서버에서 복원됩니다."
    fi
fi

# Windows WebClient BasicAuthLevel 확인 (HTTP Basic Auth 허용 필요)
BASIC_AUTH_LEVEL=$(powershell -Command "(Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters' -Name BasicAuthLevel -ErrorAction SilentlyContinue).BasicAuthLevel" 2>/dev/null)

if [ "$BASIC_AUTH_LEVEL" != "2" ]; then
    echo "[경고] Windows WebClient BasicAuthLevel=$BASIC_AUTH_LEVEL (HTTP Basic Auth 차단됨)"
    echo "  네트워크 드라이브 연결을 위해 BasicAuthLevel=2로 변경합니다..."
    echo "  (관리자 권한이 필요합니다)"
    powershell -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-Command', 'Set-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters -Name BasicAuthLevel -Value 2; Restart-Service WebClient -Force; Write-Host Done'"
    echo "  BasicAuthLevel=2 설정 완료, WebClient 서비스 재시작됨"
fi

# 가상환경 활성화
source "$SCRIPT_DIR/.venv/Scripts/activate"

echo "StardustFS 시작 (http://127.0.0.1:8080/)"
echo "  사용자: admin / 비밀번호: stardust"
echo "  스토리지: 10MB x 3 루프백"
echo "  종료: Ctrl+C"
echo ""

python "$SCRIPT_DIR/stardustfs.py" --config "$SCRIPT_DIR/dev-config.json"
