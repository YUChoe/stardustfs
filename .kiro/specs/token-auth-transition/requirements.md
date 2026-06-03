---
inclusion: manual
---

# 토큰 기반 디바이스 인증 전환 — Requirements

## 개요

현재 인증은 `.env`의 `STARDUST_EMAIL`/`STARDUST_PASSWORD`(평문 비밀번호)로 매
프로세스가 재로그인하고, `AuthClient`는 토큰을 메모리에만 보관한다. 디바이스 정보
(`device_name`)는 config.json에 있고 device_id는 런타임에 서버 조회로 얻는다.

이 스펙은 다음 흐름으로 전환한다.

1. 사용자는 웹 서버에서 계정을 생성한다(기존 `/auth/register`).
2. 사용자는 클라이언트를 설치하고 GUI/CLI로 email/password 인증을 1회 수행한다.
3. 서버는 토큰 쌍(access/refresh)을 발급한다(기존 `/auth/login`).
4. 클라이언트는 발급된 토큰을 로컬 자격증명 저장소에 보관하고 그 토큰으로 서버에
   접근한다(비밀번호는 저장하지 않는다).
5. access_token 만료 전에 refresh_token으로 자동 갱신하고 회전된 토큰을 저장한다.

확정된 설계 결정:
- 토큰 모델: 서버는 기존 user-scoped 토큰을 그대로 발급한다. "디바이스 토큰"은
  클라이언트가 디바이스별로 토큰을 영속화하는 방식으로 실현한다(서버 최소 변경).
- 저장 방식: 권한 제한 평문 JSON 파일(자격증명 저장소).
- `.env`의 EMAIL/PASSWORD는 `login` 명령으로 대체 후 제거한다.
- 마스터키 백업 암호(key_password)도 자격증명 저장소에 함께 보관한다.

## 용어

- 자격증명 저장소(credential store): 인증 토큰과 key_password를 보관하는 클라이언트
  로컬 파일. 위치는 `{metadata_db}.credentials.json`(daemon.json/syncstate.json과 동일
  규칙). 파일 권한은 소유자 전용.

## Acceptance Criteria (EARS)

### Requirement 1: 로그인 및 토큰 발급
- WHEN 사용자가 `login`을 실행하고 유효한 email/password를 제공하면, THE 시스템은
  SHALL `/auth/login`으로 토큰 쌍을 발급받아 자격증명 저장소에 저장한다.
- IF 자격증명이 무효하면(401), THE 시스템은 SHALL 저장 없이 인증 실패를 보고하고
  비0 종료 코드를 반환한다.
- THE 시스템은 SHALL 비밀번호를 저장소나 로그에 남기지 않는다.

### Requirement 2: 저장된 토큰으로 서버 접근
- WHEN 온라인 CLI 명령 또는 daemon이 시작되면, THE 시스템은 SHALL 자격증명
  저장소의 access_token을 사용한다.
- IF 저장소가 없거나 유효 토큰이 없으면, THE 시스템은 SHALL "login 필요"를 안내하고
  온라인 명령은 비0 종료 코드를 반환한다(daemon은 오프라인 모드로 강등).

### Requirement 3: 자동 토큰 갱신
- WHILE access_token이 만료 60초 이내이면, THE 시스템은 SHALL refresh_token으로
  갱신을 시도한다.
- WHEN 갱신이 성공하면, THE 시스템은 SHALL 회전된 access/refresh 토큰을 저장소에
  원자적으로 기록한다.
- IF refresh가 401이면, THE 시스템은 SHALL 저장된 토큰을 무효화하고 재로그인을
  요구한다(규격 에러).
- IF 네트워크 오류면, THE 시스템은 SHALL 기존 토큰을 보존하고 오프라인으로 강등한다.

### Requirement 4: 동시 갱신 직렬화
- WHEN daemon과 CLI가 동시에 토큰 갱신을 시도하면, THE 시스템은 SHALL 파일 락으로
  직렬화하고, 락 획득 후 저장소를 재로딩하여 이미 갱신된 토큰이 있으면 재갱신하지
  않는다(refresh 회전 유실 방지).

### Requirement 5: 로그아웃
- WHEN 사용자가 `logout`을 실행하면, THE 시스템은 SHALL 자격증명 저장소를 삭제한다.
- THE 시스템은 SHALL 서버측 refresh 토큰 취소를 best-effort로 시도하되, 실패해도
  로컬 삭제는 완료한다.

### Requirement 6: 저장소 파일 권한
- WHEN 자격증명 저장소를 생성/갱신하면, THE 시스템은 SHALL 소유자 전용 권한으로
  기록한다(POSIX 0600, Windows는 소유자 ACL).

### Requirement 7: 마스터키 백업 암호 보관
- WHEN 사용자가 `login` 시 key_password를 제공하면, THE 시스템은 SHALL 이를 자격증명
  저장소에 보관한다.
- WHEN key 백업/복원이 필요하면, THE 시스템은 SHALL 저장소의 key_password를
  사용하고, 없으면 대화형으로 입력받는다.

### Requirement 8: 기존 사용자 마이그레이션
- IF 자격증명 저장소가 없고 `.env`에 EMAIL/PASSWORD(및 KEY_PASSWORD)가 있으면, THE
  `login`은 SHALL 이를 일회성 부트스트랩 입력으로 사용할 수 있다.
- THE 전환은 SHALL 기존 로컬 자산(master.key, metadata_db)과 서버 디바이스 레코드를
  변경하지 않는다.
- WHEN 전환이 완료되면, THE 문서는 SHALL `.env`의 EMAIL/PASSWORD/KEY_PASSWORD 제거를
  안내한다.

### Requirement 9: 오프라인 시나리오
- IF 서버 도달 불가이고 저장소에 유효 토큰이 없으면, THE daemon은 SHALL 기존과 같이
  오프라인 모드로 강등한다(master.key가 로컬에 있어야 동작).
