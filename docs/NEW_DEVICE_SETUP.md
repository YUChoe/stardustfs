# 신규 디바이스 설치·설정·실행·동기화 절차

이미 StardustFS 계정을 쓰고 있는 사용자가 새 PC(또는 노트북)를 같은 계정의
디바이스로 추가하는 절차다. 순서는 다음 4단계이며, 순서를 바꾸면 실패한다.

```
설치 → 설정 파일 생성 → 로그인(키 백업 암호 포함) → daemon 실행(키 복원·등록·동기화)
```

핵심 제약: 이 기기에는 계정의 마스터 키가 없다. 마스터 키는 서버에 사용자
비밀번호와 별개인 "마스터키 백업 암호"로 암호화되어 보관돼 있고, 로그인 시 그
암호를 함께 저장해야 daemon이 첫 기동에서 키를 복원한다. 키가 없으면 메타데이터
DB를 열 수 없어 어떤 명령도 동작하지 않는다.

## 0. 사전 준비 (체크리스트)

| 항목 | 값 예시 | 확인 방법 |
|------|---------|-----------|
| 서버 URL | `https://stardustfs.noizze.net` | 기존 기기의 `config.json` → `server.url` |
| 계정 이메일/비밀번호 | `me@example.com` | 서버 웹 로그인으로 확인 |
| 마스터키 백업 암호 | (첫 기기에서 지정한 값) | 아래 "키 백업 존재 확인" 참고 |
| 기존 기기 1대 온라인 | — | 새 기기에서 파일 내용을 받으려면 필요 |
| 저장 폴더 여유 공간 | 제공할 용량만큼 | 새 기기가 스토리지를 제공할 경우 |

계정이 없으면 먼저 서버 웹의 가입 페이지에서 계정을 만든다(`POST /auth/register`와
동일). 계정 생성 후에는 첫 기기에서 키를 생성·백업해야 한다.

### 키 백업 존재 확인 (중요)

서버에 키 백업이 없으면 새 기기에서 복원할 수 없다. 기존 기기에서 한 번
백업 암호를 지정해 재로그인하고 daemon을 재시작한다.

```
python stardustfs.py login --config <기존기기 config.json> --key-password '<백업암호>'
python stardustfs.py daemon stop --config <기존기기 config.json>
python stardustfs.py daemon --config <기존기기 config.json>
```

daemon 로그에 `서버에 key 백업이 이미 존재, 덮어쓰지 않음` 또는 업로드 성공이
찍히면 준비 완료다. `key 백업 암호 미설정, key 백업 건너뜀` 경고가 보이면
백업이 올라가지 않은 상태이므로 새 기기 작업을 진행하지 말 것.

백업 암호를 분실하면 서버의 키 백업을 복호화할 수 없고, 다른 기기의 `master.key`
파일을 직접 복사하는 방법만 남는다. 서버는 암호도 키도 평문으로 갖고 있지 않다.

## 1. 설치

### A. Windows 배포본 (권장)

1. GitHub Releases에서 `stardustfs-windows-x64.zip`을 받는다.
2. 임의 폴더에 압축을 풀면 `stardustfs\stardustfs.exe`가 생긴다.
3. 확인: `stardustfs.exe --help`

Python 설치가 필요 없다. 이후 예시의 `python stardustfs.py`는
`stardustfs.exe`로 바꿔 읽으면 된다.

### B. 소스 실행 (Windows/Linux/macOS)

Python 3.10 이상이 필요하다(개발·검증 환경 3.10, CI 빌드 3.13).

```
git clone <저장소 URL> stardustfs
cd stardustfs
python -m venv .venv
source .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python stardustfs.py --help
```

참고: Windows에서는 `pysqlcipher3`가 없어 표준 `sqlite3`로 폴백한다(정상 동작,
메타데이터 DB 파일 자체는 비암호화). 파일 데이터는 어느 환경에서나 청크 단위로
AES-256-GCM 암호화된다.

### 네트워크

라우터 포트포워딩은 필요 없다. 디바이스 간 전송은 직접 연결 → UDP 홀펀칭 →
릴레이 순으로 시도한다. 로컬 방화벽이 아웃바운드 HTTPS(443)와 P2P 포트(기본 TCP
9090, 랑데부 UDP 9091)를 막지 않으면 된다. Windows 방화벽이 첫 실행 때 묻는
`stardustfs.exe`/`python.exe` 인바운드 허용은 같은 LAN의 기기 간 직접 연결에만
쓰이므로, 거부해도 릴레이로 동작한다.

## 2. 설정 파일 생성

새 기기의 설정은 새로 만든다. 기존 기기의 `config.json`을 그대로 복사하면
디바이스 이름과 스토리지 경로가 겹쳐 서버가 두 기기를 같은 디바이스로 취급한다.

### 2-A. GUI로 생성 (권장)

1. `stardustfs.exe`를 더블클릭(또는 `python stardustfs.py gui`).
2. 메뉴 `파일` → `새 설정...`
3. 입력:
   - `설정/저장 폴더 선택` — 비어 있는 폴더 (예: `C:\Users\me\StardustFS`)
   - `서버 URL` — 기존 기기와 동일한 값
   - `디바이스 이름` — 기본값은 이 PC의 호스트명. 기존 기기와 겹치지 않게 한다
   - `이 기기에서 새 암호화 키 생성` — 반드시 체크 해제 (기존 계정이므로 서버
     백업에서 복원한다. 체크하면 다른 키가 생성돼 기존 파일을 열 수 없다)
4. 완료 후 상태바에 복원 안내가 표시된다. 다음 파일이 생성된다.

```
<폴더>\config.json      설정
<폴더>\storage\         이 기기의 스토리지(디렉터리 소스)
<폴더>\metadata.db      메타데이터 DB (로그인 후 생성)
<폴더>\master.key       마스터 키 (daemon 첫 기동에서 복원)
```

### 2-B. 설정 파일 직접 작성

`C:\Users\me\StardustFS\config.json` (경로는 절대 경로, 파일은 UTF-8/LF):

```json
{
  "version": 2,
  "server": {
    "url": "https://stardustfs.noizze.net",
    "device_name": "laptop-01"
  },
  "sources": [
    {
      "type": "directory",
      "id": "local-1",
      "path": "C:/Users/me/StardustFS/storage"
    }
  ],
  "metadata_db": "C:/Users/me/StardustFS/metadata.db",
  "key_file": "C:/Users/me/StardustFS/master.key",
  "sync": {
    "interval_seconds": 30,
    "conflict_strategy": "copy"
  },
  "p2p": {
    "port": 9090,
    "enabled": true
  }
}
```

- `sources[].path`의 디렉터리는 미리 만들어 둔다(없으면 기동이 실패한다).
- `key_file`은 아직 없는 경로를 가리킨다. `server.url`이 있으면 미존재가 검증
  오류가 아니며, daemon이 서버 백업에서 복원해 이 경로에 쓴다.
- `metadata_db`는 자동 생성된다. 같은 경로 옆에 `metadata.db.credentials.json`
  (토큰·백업 암호), `metadata.db.daemon.json`(daemon 제어), `metadata.db.daemon.log`가
  함께 만들어진다.
- 한 호스트에서 여러 설정을 동시에 돌릴 때만 `p2p.port`를 서로 다르게 한다.

## 3. 로그인 — 토큰과 백업 암호 저장

로그인은 키 없이도 실행되는 유일한 준비 단계다. 반드시 daemon 실행보다 먼저 한다.

### GUI

우상단 `로그인` → 이메일, 비밀번호, `마스터키 백업 암호(선택, 없으면 비움)`를
입력한다. 새 기기에서는 백업 암호를 비워 두면 안 된다.

### CLI

```
python stardustfs.py login --config C:/Users/me/StardustFS/config.json \
  --email me@example.com --key-password '<백업암호>'
```

`--password`를 생략하면 대화형으로 입력받는다(입력값은 저장되지 않는다).
환경변수 `STARDUST_EMAIL` / `STARDUST_PASSWORD` / `STARDUST_KEY_PASSWORD`도
쓸 수 있다. 성공 출력:

```
로그인 성공: me@example.com
자격증명 저장: C:/Users/me/StardustFS/metadata.db.credentials.json
```

저장되는 것은 액세스/리프레시 토큰과 백업 암호이며, 로그인 비밀번호는 저장하지
않는다. 파일은 소유자 전용 권한으로 기록된다.

## 4. daemon 실행 — 키 복원·디바이스 등록·초기 동기화

daemon은 이 기기를 온라인 피어로 유지한다(키 복원, 디바이스 등록, heartbeat,
P2P 서버, 주기 메타데이터 동기화).

### GUI

GUI는 설정이 채택된 상태에서 daemon 생존을 5초 주기로 확인하고, 죽어 있으면
자동으로 백그라운드에서 시작한다. 상태바 왼쪽 점이 녹색이 되면 실행 중이다.
별도 조작이 필요 없다. daemon 로그는 `metadata.db.daemon.log`에 쌓인다.

### CLI

```
python stardustfs.py daemon --config C:/Users/me/StardustFS/config.json
```

포그라운드 상주 프로세스다. 첫 기동 로그에서 다음 순서를 확인한다.

```
key_file 미존재, 서버에서 key 백업 다운로드 시도...
key_file 복원 완료: C:/Users/me/StardustFS/master.key
로컬 스토리지 초기화 완료
(디바이스 등록 → 복제 정책 조회 → 초기 동기화)
```

상태 확인·정지는 별도 셸에서:

```
python stardustfs.py daemon status --config C:/Users/me/StardustFS/config.json
python stardustfs.py daemon stop   --config C:/Users/me/StardustFS/config.json
```

`daemon 실행 중: pid=… (heartbeat …s 전)`이 나오면 정상이다.

주의: daemon 실행 전에 `ls`/`df`/`status`(오프라인 명령)를 먼저 실행하면
`Encryption_Key 로드 실패`로 종료된다. 키 복원은 daemon(또는 `get`/`put`/`devices`
같은 온라인 명령)이 수행하므로 위 순서를 지킨다.

## 5. 동기화·연결 확인

```
python stardustfs.py devices --config C:/Users/me/StardustFS/config.json
python stardustfs.py status  --config C:/Users/me/StardustFS/config.json
python stardustfs.py df      --config C:/Users/me/StardustFS/config.json
python stardustfs.py ls      --config C:/Users/me/StardustFS/config.json
```

기대 결과:

- `devices` — 새 기기가 목록에 있고 online 표시. 기존 기기도 함께 보인다.
- `status` — 보류 변경 수 0(초기 동기화 완료).
- `ls` — 기존 기기에서 올린 파일·폴더가 그대로 보인다. 메타데이터는 서버를 통해
  증분 동기화되므로 새 기기의 스토리지가 비어 있어도 목록은 전부 보인다.
- `df` — 총/가용 용량. 새 기기가 제공하는 용량 + 원격 소스가 합산된다.

GUI에서는 파일 목록과 하단 관리 패널(스토리지/디바이스)에서 동일한 내용을
확인한다.

## 6. 파일 접근

```
python stardustfs.py get /docs/report.pdf ./report.pdf --config <cfg>
python stardustfs.py put ./photo.jpg /photos/photo.jpg --config <cfg>
```

- 다운로드는 청크를 보관한 디바이스에서 P2P로 받는다. 해당 기기가 모두
  오프라인이면 실패한다 — 그 기기를 켜거나, 복제본이 있으면
  `restore <경로>`로 복구한다.
- 업로드는 이 기기의 로컬 스토리지에 암호문 청크로 저장되고, 메타데이터가 서버로
  전파된다. 다른 기기에서 보이려면 그 기기의 다음 동기화 주기(기본 30초)를
  기다린다.
- 가상 경로는 선행 슬래시 없이 상대 경로로 써도 된다(Git Bash 경로 변환 회피).

## 7. 이 기기의 스토리지 제공 (선택)

기본 설정의 디렉터리 소스만으로도 동작한다. 용량을 고정 컨테이너로 제공하려면
GUI 하단 관리 패널에서 스토리지를 추가한다(루프백 이미지 생성·포맷까지 수행).
설정 파일을 직접 편집하는 경우 `sources`에 다음을 추가하고 daemon을 재시작한다.

```json
{
  "type": "loopback",
  "id": "loopback-a1b2c3",
  "path": "C:/Users/me/StardustFS/storage_001.img",
  "size": 10737418240
}
```

`size`는 10 MB ~ 2 TB 범위의 바이트 값이다. 파일이 이미 있으면 덮어쓰지 않는다.

## 8. 부팅 시 자동 시작 (선택)

GUI를 시작 항목에 등록하는 것이 가장 간단하다. GUI가 daemon을 감독·재시작하고,
창을 닫으면 트레이로 숨는다(트레이는 `pystray`/`Pillow`가 있을 때).

- Windows: `Win+R` → `shell:startup` → `stardustfs.exe` 바로가기를 넣고, 인자로
  `gui --config C:\Users\me\StardustFS\config.json`을 지정한다.
- daemon만 상주시키려면 작업 스케줄러에 "로그온 시" 트리거로
  `stardustfs.exe daemon --config <cfg>`를 등록한다.

## 9. 완료 판정

- [ ] `daemon status` → 실행 중, heartbeat 60초 이내
- [ ] `master.key` 파일이 생성됨 (32바이트)
- [ ] `devices` 목록에 새 기기가 online으로 표시
- [ ] `ls`로 기존 파일 목록이 보임
- [ ] 기존 기기에서 올린 파일을 `get`으로 받아 내용이 정상
- [ ] 새 기기에서 `put`한 파일이 기존 기기의 `ls`에 나타남

## 10. 트러블슈팅

| 메시지 / 증상 | 원인 | 조치 |
|---------------|------|------|
| `key_file이 존재하지 않고 key 백업 암호도 없습니다` | 로그인 시 백업 암호 미저장 | `login --key-password '<암호>'` 재실행 후 daemon 재시작 |
| `서버에 key 백업이 존재하지 않습니다` | 계정에 키 백업 없음 | 0절 "키 백업 존재 확인"을 기존 기기에서 수행 |
| `복호화 실패: 비밀번호 불일치 또는 백업 데이터 변조` | 백업 암호 오입력 | 정확한 암호로 재로그인. 분실 시 기존 기기의 `master.key` 복사 |
| `저장된 자격증명이 없습니다` / 오프라인 모드로 시작 | 로그인 전 daemon 실행 | `login` 후 daemon 재시작 |
| `Encryption_Key 로드 실패` | 키 복원 전 오프라인 명령 실행 | daemon을 먼저 띄운다 |
| `이 device를 서버 목록에서 찾지 못했습니다` | 등록 전 CLI 실행(등록은 daemon 담당) | daemon 실행 후 재시도 |
| `daemon이 이미 실행 중입니다 (pid=…)` | 중복 실행 | `daemon status`로 확인, 필요 시 `daemon stop` |
| 두 기기가 서버에서 한 대로 보임 | `device_name`이 같고 OS도 동일 | 새 기기의 `server.device_name`을 바꾸고 daemon 재시작 |
| 목록은 보이는데 `get` 실패 | 청크 보유 기기가 오프라인 | 해당 기기 기동, 또는 `restore <경로>` |
| `sources[N].path: 존재하는 디렉토리가 아닙니다` | 스토리지 폴더 미생성 | 폴더 생성 후 재시작 |
| 토큰 만료/무효 경고 후 오프라인 모드 | 리프레시 토큰 만료 | `login` 재실행. daemon은 60초 주기로 온라인 복구를 재시도한다 |
| 트레이 아이콘 없음 | `pystray`/`Pillow` 미설치 | `pip install -r requirements.txt` (없으면 창 닫기가 종료) |

로그 확인 위치: GUI가 띄운 daemon은 `<metadata_db>.daemon.log`, CLI로 띄운 daemon은
해당 터미널 출력.

## 부록 A. 첫 디바이스 (계정 최초 설정)

계정을 만든 직후의 첫 기기는 복원할 키가 없으므로 키를 직접 생성한다. 위 절차와
다른 부분만 적는다.

1. 설치는 1절과 동일하다.
2. 설정 생성 — GUI `새 설정...`에서 `이 기기에서 새 암호화 키 생성`을 체크한다
   (32바이트 `master.key`를 즉시 생성). 설정 파일을 직접 쓰는 경우 2-B 예시대로
   작성하고 키를 만든다.

   ```
   python -c "import os; open('C:/Users/me/StardustFS/master.key','wb').write(os.urandom(32))"
   ```

3. 로그인 — 반드시 `--key-password`를 지정한다. 여기서 정한 값이 이후 모든 신규
   기기의 복원 암호이며, 서버는 이 암호를 갖고 있지 않다.

   ```
   python stardustfs.py login --config <cfg> --email me@example.com --key-password '<백업암호>'
   ```

4. daemon 실행 — 첫 기동에서 `master.key`를 백업 암호로 암호화해 서버에 업로드한다
   (`PUT /sync/key`). 서버에 이미 백업이 있으면 덮어쓰지 않는다. 로그에
   `key 백업 암호 미설정, key 백업 건너뜀` 경고가 남으면 3번을 다시 수행한다.

키 로드 우선순위는 `key_file` → 환경변수 `STARDUST_KEY`이며, 키는 정확히 32바이트여야
한다. `master.key`를 잃고 서버 백업도 없으면 저장된 파일을 복호화할 방법이 없다.
백업 암호와 키 파일은 별도 매체에 보관한다.

## 관련 문서

- [docs/CONFIGURATION.md](CONFIGURATION.md) — 설정 필드
- [docs/TRANSPORT.md](TRANSPORT.md) — 직접 연결·홀펀칭·릴레이
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — 전체 구조
- [README.md](../README.md) — CLI 명령 요약
