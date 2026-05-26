# StardustFS 설정 가이드

StardustFS는 JSON 설정 파일과 별도의 암호화 키를 사용하여 구성한다.

## 1. 의존성 설치

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt
```

주요 의존성:
- `wsgidav` / `cheroot` - WebDAV 서버
- `cryptography` - AES-256-GCM 암호화
- `pysqlcipher3` - 메타데이터 DB 암호화 (선택, 없으면 표준 sqlite3 폴백)

## 2. 암호화 키 생성

StardustFS는 정확히 32바이트(256비트)의 마스터 키를 요구한다.

```bash
# 랜덤 키 파일 생성
python -c "import os; open('master.key', 'wb').write(os.urandom(32))"
```

또는 환경변수로 제공:

```bash
export STARDUST_KEY=$(python -c "import os; import sys; sys.stdout.buffer.write(os.urandom(32))" | cat)
```

키 로드 우선순위:
1. 설정 파일의 `key_file` 경로에 있는 파일
2. 환경변수 `STARDUST_KEY`

둘 다 없으면 시작이 중단된다.

## 3. 설정 파일 작성

`stardustfs.json` 예시:

```json
{
  "version": 1,
  "webdav": {
    "host": "127.0.0.1",
    "port": 8080,
    "username": "admin",
    "password": "your_secure_password"
  },
  "sources": [
    {
      "type": "directory",
      "id": "disk1",
      "path": "/home/user/storage/disk1"
    },
    {
      "type": "directory",
      "id": "disk2",
      "path": "/home/user/storage/disk2"
    },
    {
      "type": "loopback",
      "id": "vault1",
      "path": "/home/user/storage/vault.img",
      "size": 1073741824
    }
  ],
  "metadata_db": "/home/user/.stardustfs/metadata.db",
  "key_file": "/home/user/.stardustfs/master.key"
}
```

## 4. 설정 필드 설명

### `version`

| 필드 | 값 |
|------|-----|
| 타입 | 정수 |
| 필수 | 예 |
| 현재 지원 | `1` |

### `webdav`

| 필드 | 타입 | 설명 | 기본값 |
|------|------|------|--------|
| `host` | string | 바인드 주소. 보안상 항상 `127.0.0.1`로 강제됨 | - |
| `port` | int | 바인드 포트 (1~65535) | 8080 |
| `username` | string | HTTP Basic Auth 사용자명 | - |
| `password` | string | HTTP Basic Auth 비밀번호 | - |

`host`는 설정 파일에 어떤 값을 넣어도 `127.0.0.1`로 고정된다. 외부 접근이 필요하면 리버스 프록시를 사용할 것.

### `sources`

최소 1개 이상의 스토리지 소스가 필요하다.

#### Directory Source

| 필드 | 타입 | 설명 |
|------|------|------|
| `type` | `"directory"` | 소스 유형 |
| `id` | string | 소스 고유 ID |
| `path` | string | 절대 경로 (존재하는 디렉토리) |

#### Loopback Source

| 필드 | 타입 | 설명 |
|------|------|------|
| `type` | `"loopback"` | 소스 유형 |
| `id` | string | 소스 고유 ID |
| `path` | string | 루프백 파일 절대 경로 |
| `size` | int | 파일 크기 (바이트, 10MB ~ 2TB) |

Loopback 소스는 지정된 크기의 파일을 생성하고, 동반 디렉토리(`path.d/`)에 실제 데이터를 저장한다. 파일이 이미 존재하면 덮어쓰지 않고 기존 파일을 활성화한다.

### `metadata_db`

| 필드 | 타입 | 설명 |
|------|------|------|
| `metadata_db` | string | SQLite DB 파일 경로 |

파일이 없으면 자동 생성된다. pysqlcipher3가 설치되어 있으면 AES-256으로 암호화된다.

### `key_file`

| 필드 | 타입 | 설명 |
|------|------|------|
| `key_file` | string \| null | 암호화 키 파일 경로 (선택) |

`null`이면 환경변수 `STARDUST_KEY`에서 키를 로드한다.

## 5. 서버 시작

```bash
python stardustfs.py --config stardustfs.json
```

성공 시 로그:
```
StardustFS 준비 완료
WebDAV 서버 시작: http://127.0.0.1:8080/
```

## 6. 클라이언트 연결

### Windows

```cmd
net use Z: http://localhost:8080/ /user:admin your_secure_password
```

### Linux

```bash
sudo mount -t davfs http://localhost:8080/ /mnt/stardust
```

### macOS

Finder → 이동 → 서버에 연결 → `http://localhost:8080/`

## 7. 검증 규칙 요약

시작 시 다음 항목을 순차적으로 검증한다. 하나라도 실패하면 모든 에러를 로그에 기록하고 종료한다.

1. 설정 파일 존재 및 JSON 파싱
2. `version` = 1
3. `sources` 최소 1개
4. Directory Source 경로 존재 + 읽기/쓰기 권한
5. Loopback Source 경로가 절대 경로 + 크기 범위 (10MB~2TB)
6. `webdav.port` 범위 (1~65535)
7. 암호화 키 로드 가능 + 정확히 32바이트
8. 메타데이터 DB 연결 (10초 타임아웃)

## 8. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `KeyNotFoundError` | 키 파일/환경변수 없음 | `key_file` 경로 확인 또는 `STARDUST_KEY` 설정 |
| `InvalidKeyError` | 키가 32바이트가 아님 | `python -c "import os; open('master.key','wb').write(os.urandom(32))"` |
| `sources: 최소 1개` | 소스 미설정 | `sources` 배열에 최소 1개 추가 |
| `절대 경로여야 합니다` | 상대 경로 사용 | 절대 경로로 변경 |
| `존재하는 디렉토리가 아닙니다` | 경로 미존재 | 디렉토리 생성 후 재시작 |
| 포트 충돌 | 이미 사용 중인 포트 | `webdav.port` 변경 |
