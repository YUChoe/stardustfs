# StardustFS 개발 준수사항

StardustFS는 WebDAV/FUSE 기반 분산 파일시스템입니다. 여러 스토리지 노드를 하나의 마운트 포인트로 통합하고, P2P 동기화와 암호화 메타데이터를 제공합니다.

## 개발 환경
- Python, 가상환경 `.venv` (Windows Git Bash: `source .venv/Scripts/activate`)
- 메인 진입점: `stardustfs.py`
- 핵심 라이브러리: `stardustlib/`
- 중앙 서버: 별도 저장소 `../stardustfs-server/`
- WebDAV 서버: http://127.0.0.1:8080/ (개발 계정: admin / stardust)
- 비동기 프로그래밍: `async/await` (aiohttp, httpx)
- 의존성: `requirements.txt` (버전 고정)

## 실행
```bash
# 개발용 실행 (10MB 루프백 스토리지 3개)
./run-dev.sh

# 직접 실행
source .venv/Scripts/activate
python stardustfs.py --config dev-config.json

# 테스트
source .venv/Scripts/activate
pytest
```

## 기본 원칙
- PEP8 준수, snake_case 네이밍
- 가상환경 필수
- Windows 환경에서 Git Bash 사용
- 타입 힌트, enum 적극 활용
- 방어적 프로그래밍, 충분한 로깅
- 소스 파일 한 개의 라인 수는 가능하면 500줄 이하로 유지
- 코드 추가 전 동일 기능이 코드베이스/라이브러리에 이미 있는지 먼저 확인
- 파일은 LF 라인 끝, UTF-8 인코딩
- `&&` 를 이용한 데이지체인 실행을 최소화 할 것 
- string 은 반드시 UTF-8 인코딩을 명시 하고 사용 할 것 

## 로깅
- Python 표준 `logging` 모듈 사용. 모듈별로 `logger = logging.getLogger(__name__)`.
- 출력 포맷(이미 `stardustfs.py`에 적용됨):
  `%(asctime)s.%(msecs)03d %(levelname)s [%(filename)s:%(lineno)d] %(message)s`, `datefmt="%H:%M:%S"`
  - 예시: `14:23:45.123 INFO [sync_client.py:45] 동기화 시작`
- `%`-스타일 지연 포매팅 사용(기존 코드 관례): `logger.info("설정 로드 실패: %s", e)`
- 레벨 가이드: DEBUG(실행 흐름) / INFO(주요 이벤트) / WARNING(잠재 문제) / ERROR(실패, `exc_info=True`) / CRITICAL(종료급 오류)
- 민감정보(암호화 키, 토큰, 비밀번호) 로깅 금지
- 외부 라이브러리 로그는 WARNING으로 억제(httpx, httpcore, wsgidav)

## 데이터베이스 (메타데이터)
- 메타데이터 저장소: `stardustlib/metadata_store.py`
- pysqlcipher3(SQLCipher 암호화) 사용, 미설치 시 표준 `sqlite3`로 폴백(Windows)
- WAL 모드(`journal_mode=WAL`), `busy_timeout=5000` 설정됨
- 테이블 스키마는 절대로 추측하지 말 것. 실제 코드(`metadata_store.py`의 `CREATE TABLE` / `PRAGMA table_info`)를 확인 후 사용.
- DB 조작 시 `python -c` 단발 실행 대신 `scripts/` 디렉토리에 스크립트를 작성한 뒤 실행

## 영어 텍스트 (영국 영어)
사용자 대상 영어(문서, UI, 에러/시스템 메시지)는 영국 영어 철자·어휘를 일관되게 사용.
- 철자: `-ise`(recognise, organise), `-our`(colour, behaviour), `-re`(centre), `defence`/`licence`
- 어휘: elevator→lift, apartment→flat, garbage→rubbish

## 스펙 문서 작성 가이드
스펙 문서는 `.kiro/specs/<spec>/`의 requirements.md, design.md, tasks.md로 작성한다.

### 언어 규칙
- 본문은 한국어로 작성한다.
- 단, 스펙 형식이 요구하는 섹션 헤더는 영어를 유지한다:
  `## Correctness Properties`, `### Property N: {한글 제목}`, `## Error Handling`,
  `## Testing Strategy`, `## Components and Interfaces`, `## Data Models`
- Property 설명문에서 "For any"는 "*임의의*"로 번역한다.
- 코드 블록 내 주석은 한국어로 작성한다.

### 형식 제약 (진단 통과 필수)
- design.md의 Correctness Properties 섹션 헤더는 반드시 `## Correctness Properties` (한글 변환 금지)
- 각 속성 헤더는 반드시 `### Property N:` 형식 유지 (숫자 + 콜론 필수)
- requirements.md의 Acceptance Criteria는 EARS 패턴(WHEN/IF/WHILE/THE...SHALL) 사용

### 내용 규칙
- 타임아웃, 재시도 횟수, 범위 등 구체적 수치를 명시한다.
- 에러 발생 시 동작(예외 타입, HTTP 상태 코드)을 명확히 기술한다.
- 오프라인/실패 시나리오를 반드시 포함한다.
- 마이그레이션이 필요한 경우 백업 + 롤백 전략을 명시한다.


### git 사용 가이드

1. `source .venv/Scripts/activate` 등을 사용할 필요 없음
2. `&&` 데이지체인을 사용하지 말 것 
3. @'...'@는 PowerShell 구문인데 Git Bash에서는 @가 리터럴로 들어가 커밋 메시지에 @가 붙게됨 

#### 잘못된 사례

* `source .venv/Scripts/activate && rtk git status --short | grep -v '^??'`

* ```source .venv/Scripts/activate && git add stardustfs.py stardustlib/gui/actions.py && git commit -m @'
fix: exe 더블클릭 시 즉시 종료 → GUI 실행 + 프로즌 daemon 호출 수정

- 인자 없이 실행(exe 더블클릭)하면 command=None→daemon 모드인데 --config가 없어
  parser.error로 즉시 종료되던 문제. 인자·config 모두 없으면 GUI를 연다(명시적
  --config/daemon/gui/단발 명령은 기존대로).
- GUI가 daemon을 띄울 때 프로즌(PyInstaller) exe면 stardustfs.py(소스, exe엔 없음)
  대신 exe 자신의 daemon 서브커맨드를 호출(sys.frozen 분기, cwd 상속).
- 클라이언트 497 passed/1 skip.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
'@```

#### 올바른 사례

* 상태 확인 (한 명령씩, `&&` 없이):

```
git status --short
```

* 스테이징:

```
git add stardustfs.py stardustlib/gui/actions.py
```

* 멀티라인 커밋 메시지는 bash heredoc(`-F -`)을 사용 (`@'...'@` 금지):

```
git commit -F - <<'EOF'
fix: 제목 줄

- 본문 첫째 줄
- 본문 둘째 줄

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```