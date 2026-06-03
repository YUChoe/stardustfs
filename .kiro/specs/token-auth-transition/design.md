---
inclusion: manual
---

# 토큰 기반 디바이스 인증 전환 — Design

## 개요

자격증명 저장소(credential store)를 도입해 토큰을 영속화하고, `login`/`logout`
명령으로 인증을 부트스트랩한다. 서버는 기존 user-scoped 토큰 발급(`/auth/login`,
`/auth/refresh`)을 그대로 사용한다(서버 변경 없음). `AuthClient`는 메모리 전용에서
저장소 연동으로 바뀐다.

## Components and Interfaces

### CredentialStore (신규: `stardustlib/credential_store.py`)
자격증명 저장소 파일을 관리한다.

- `__init__(metadata_db_path)`: 경로 `{metadata_db}.credentials.json` 결정.
- `load() -> dict | None`: 저장소를 읽는다(없으면 None, 손상 시 예외).
- `save(data: dict) -> None`: tmp+replace로 원자적 기록, 소유자 전용 권한 부여.
- `clear() -> None`: 저장소 파일 삭제.
- `path -> str`, `exists() -> bool`.
- 권한: POSIX는 `os.open(..., 0o600)` 또는 생성 후 `os.chmod(0o600)`. Windows는
  `icacls`로 소유자 외 권한 제거(베스트 에포트, 실패 시 WARNING 로그).

### AuthClient (변경: `stardustlib/auth_client.py`)
- 생성자에 `credential_store: CredentialStore | None` 추가. 주어지면 토큰을 저장소와
  동기화한다(하위호환: None이면 기존 메모리 전용 동작).
- `login(email, password)`: 기존대로 토큰 발급 후, 저장소가 있으면 `save`로 영속화.
- `load_from_store()`: 시작 시 저장소에서 access/refresh/expiry/user_id를 로딩.
- `get_valid_token()`: 만료 임박 시 갱신. 갱신은 `_refresh_with_lock()`로 위임.
- `_refresh_with_lock()`: 파일 락 획득 → 저장소 재로딩 → 다른 프로세스가 이미 갱신해
  유효 토큰이 있으면 그대로 사용, 아니면 `/auth/refresh` 호출 후 저장소에 회전 토큰
  기록 → 락 해제. (Requirement 4)
- `refresh_token()`: 401이면 저장소의 토큰을 무효화(저장소는 유지하되 토큰 필드
  제거 또는 별도 플래그)하고 `AuthenticationError`.

### 파일 락 (`stardustlib/credential_store.py` 내 헬퍼)
- 크로스플랫폼 권고: 별도 락 파일 `{credentials}.lock`을 `os.open(O_CREAT|O_EXCL)`로
  생성하는 스핀 + 타임아웃 방식(데몬/CLI 단일 호스트 가정). 보유시간이 짧아(단일 HTTP
  refresh) 경쟁이 드물다. 타임아웃 시 갱신을 건너뛰고 기존 토큰 사용.

### CLI 명령 (변경: `stardustlib/cli/`)
- `login` (오프라인 분류, 자체 처리): email/password를 (flag > 환경변수 > 대화형
  프롬프트) 순으로 수집 → `AuthClient.login` → 저장소 기록. key_password도 동일하게
  수집해 저장소에 보관(없으면 건너뜀). `getpass`로 비밀번호 입력 에코 방지.
- `logout`: 서버측 취소 best-effort 후 `CredentialStore.clear()`.
- `CLISession.open_online()`: `STARDUST_EMAIL/PASSWORD` 직접 읽기를 제거하고,
  `CredentialStore`에서 토큰을 로딩한 `AuthClient`를 구성한다. 토큰이 없으면 온라인
  명령은 "login 필요" 규격 에러.

### daemon / 복구 경로 (변경: `stardustfs.py`, `online_recovery.py`)
- `startup_v2`와 `OnlineRecoveryManager`의 `STARDUST_EMAIL/PASSWORD` 로그인 대신
  저장소 토큰 사용. 토큰 없으면 오프라인 모드로 강등(Requirement 9).
- key 백업/복원(`_backup_key_to_server`/`_restore_key_from_server`)의
  `STARDUST_KEY_PASSWORD` 직접 읽기를 저장소 key_password 우선, 없으면 대화형으로 변경.

## Data Models

자격증명 저장소 `{metadata_db}.credentials.json`:
```json
{
  "version": 1,
  "server_url": "https://...",
  "access_token": "<JWT, 약 15분>",
  "refresh_token": "<JWT, 약 30일, 회전>",
  "access_expires_at": 1780000000,
  "user_id": "uuid",
  "email": "user@example.com",
  "key_password": "<선택: 마스터키 백업 암호>"
}
```
- 비밀번호(로그인용)는 저장하지 않는다.
- `key_password`는 선택 필드. 미보관 시 복원 때 대화형 입력.

## Correctness Properties

### Property 1: 비밀번호 비저장
*임의의* 로그인 입력에 대해, 자격증명 저장소 파일과 로그에는 로그인 비밀번호가
평문/해시 어떤 형태로도 기록되지 않는다(토큰과 key_password만 보관).

### Property 2: 토큰 갱신 직렬화
*임의의* 동시 갱신 시도(daemon + CLI)에 대해, refresh_token 회전은 최대 1회만
발생하고, 유효한 refresh_token이 유실되지 않는다(락 + 재로딩으로 보장).

### Property 3: 저장소 원자적 기록
*임의의* 저장 시점에 프로세스가 중단되어도, 자격증명 저장소는 직전 유효 상태이거나
새 유효 상태이며, 부분 기록된 손상 파일이 남지 않는다(tmp + os.replace).

### Property 4: 저장소 파일 권한
*임의의* 저장소 생성/갱신 후, 파일 권한은 소유자 전용이다(POSIX 0600 / Windows
소유자 ACL).

### Property 5: 마이그레이션 무손상
*임의의* 전환 수행에 대해, master.key·metadata_db·서버 디바이스 레코드는 변경되지
않는다(자격증명 저장소만 신규 생성).

## Error Handling

- 저장소 없음 + 온라인 명령: `AuthenticationError` 등가 → 사용자 메시지 "stardustfs
  login 먼저 실행", 종료 코드 비0(예: 1). daemon은 오프라인 강등.
- refresh 401: 저장소 토큰 무효화 + 재로그인 요구 메시지, 종료 코드 비0.
- 저장소 JSON 손상: 명확한 오류 + 재로그인 안내(저장소 백업 후 재생성 권고).
- 파일 락 타임아웃: 갱신 건너뛰고 기존 access_token 사용(만료면 401로 이어져 재로그인
  유도). WARNING 로그.
- 네트워크 오류: 기존 토큰 보존, 오프라인 강등(예외 전파 금지).

## Testing Strategy

- 단위(CredentialStore): save→load 라운드트립, 원자적 기록(tmp 잔존 없음), 권한
  0600(POSIX), clear, 손상 파일 처리.
- 단위(AuthClient): login 시 저장소 기록, load_from_store, 만료 임박 갱신 후 저장소
  회전 기록, refresh 401 시 토큰 무효화.
- 동시성: 두 스레드/프로세스가 동시에 `_refresh_with_lock` 호출 시 refresh 호출이 1회
  (mock 서버 카운트)임을 검증.
- 마이그레이션: 저장소 없음 + .env 자격증명 → `login` 부트스트랩 → 저장소 생성,
  master.key/metadata 미변경 단언.
- E2E(로컬 서버): `login` → `devices`/`put`/`get`(저장소 토큰 사용, .env 미사용) →
  토큰 만료 시뮬레이션 후 자동 갱신 → `logout` → 온라인 명령이 "login 필요" 반환.
- 회귀: 기존 클라이언트/서버 pytest 그린 유지.

## 마이그레이션 절차 (기존 사용자)

1. 클라이언트 업데이트 후 `stardustfs login --config <cfg>` 실행. 저장소가 없으면
   `.env`의 EMAIL/PASSWORD/KEY_PASSWORD를 일회성 부트스트랩으로 읽어 로그인·저장
   (또는 대화형 입력).
2. `stardustfs devices`로 토큰 동작 확인(저장소 토큰만으로 접근되는지).
3. `.env`에서 `STARDUST_EMAIL`/`STARDUST_PASSWORD`/`STARDUST_KEY_PASSWORD` 제거.
4. master.key·metadata_db·device 레코드는 그대로 유지(전환은 인증 계층만 변경).
- 롤백: 저장소 파일 삭제 후 `.env` 복원 시 기존 동작으로 회귀 가능(전환기 동안 코드가
  저장소 우선·.env 부트스트랩을 모두 지원).
