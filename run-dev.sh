#!/bin/bash
# StardustFS 개발용 실행 스크립트
# dev-config.json의 루프백 스토리지로 상주 daemon을 시작한다.

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

# 가상환경 활성화
source "$SCRIPT_DIR/.venv/Scripts/activate"

echo "StardustFS daemon 시작"
echo "  스토리지: dev-config.json의 루프백 소스"
echo "  CLI 예: python stardustfs.py --config dev-config.json ls"
echo "  상태/정지: daemon status / daemon stop"
echo "  종료: Ctrl+C"
echo ""

python "$SCRIPT_DIR/stardustfs.py" daemon --config "$SCRIPT_DIR/dev-config.json"
