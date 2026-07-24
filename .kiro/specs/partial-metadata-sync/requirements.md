# Requirements Document

## Introduction

현재 동기화는 매 사이클마다 전체 metadata DB를 AES-256-GCM으로 통째 암호화해
업로드하고, 다운로드 시에도 전체 blob을 받아 레코드 단위로 병합한다. 레코드가
많아질수록(현재 500개) 전송량과 병합 비용이 선형으로 증가하고, 롱폴링으로 즉시
통지를 받아도 실제 반영까지 전체 전송을 기다려야 한다.

이 기능은 서버가 레코드 단위 암호문을 저장하도록 바꿔, 변경된 레코드만 주고받는
증분 동기화를 도입한다. zero-knowledge 원칙(서버는 암호문과 정수 version만 본다)을
유지한다.

## Glossary

- record_id: 레코드 식별용 불투명 값. `HMAC-SHA256(subkey, virtual_path)`의 hex.
  서버는 이 값만 보고 경로 평문은 보지 못한다.
- record_version: 해당 레코드가 마지막으로 변경된 시점의 서버 글로벌 version.
- current_version: user별 단조 증가 글로벌 version(기존 metadata version과 동일 의미).
- purge: 보관기간이 지난 tombstone 레코드를 서버 저장소에서 물리 제거하는 것.

## Requirements

### Requirement 1: 레코드 단위 서버 저장

**User Story:** 사용자로서, 서버가 메타데이터를 레코드 단위 암호문으로 저장하기를
원한다. 그래야 변경분만 주고받을 수 있고 서버는 여전히 내용을 보지 못한다.

#### Acceptance Criteria
1. WHEN 클라이언트가 레코드를 업로드하면 THE 서버 SHALL 각 레코드를 (user_id,
   record_id) 단위로 암호문 그대로 저장한다.
2. THE 서버 SHALL 레코드 평문(virtual_path, 크기 등)을 저장하거나 해석하지 않는다.
3. THE 서버 SHALL user별 current_version을 단조 증가로 유지한다.

### Requirement 2: 증분 다운로드

**User Story:** 사용자로서, 마지막으로 받은 version 이후 변경된 레코드만 받기를
원한다. 그래야 동기화가 빠르다.

#### Acceptance Criteria
1. WHEN 클라이언트가 `since` version으로 레코드 목록을 요청하면 THE 서버 SHALL
   record_version > since 인 레코드만 반환한다.
2. THE 서버 SHALL 응답에 현재 current_version을 포함한다.
3. WHEN `since=0`이면 THE 서버 SHALL 모든 활성 레코드를 반환한다(신규 디바이스 초기 동기화).

### Requirement 3: 증분 업로드 + 낙관적 잠금(CAS)

**User Story:** 사용자로서, 여러 디바이스가 동시에 업로드해도 변경이 유실되지 않기를
원한다.

#### Acceptance Criteria
1. WHEN 클라이언트가 base_version과 변경 레코드 배치를 업로드하면 THE 서버 SHALL
   base_version이 current_version과 일치할 때만 기록을 허용한다.
2. IF base_version != current_version THEN THE 서버 SHALL 409와 현재 version을 반환한다.
3. WHEN 기록에 성공하면 THE 서버 SHALL current_version을 1 증가시키고, 배치 내 모든
   레코드의 record_version을 새 version으로 설정한 뒤 새 version을 반환한다.

### Requirement 4: 삭제/tombstone 전파

**User Story:** 사용자로서, 한 디바이스에서 삭제한 파일이 다른 디바이스에도 전파되고
오래된 tombstone은 정리되기를 원한다.

#### Acceptance Criteria
1. THE 클라이언트 SHALL 삭제를 deleted=true 페이로드를 가진 레코드 업서트로 전파한다.
2. WHEN 클라이언트가 보관기간이 지난 tombstone을 GC하면 THE 클라이언트 SHALL 해당
   record_id의 서버 레코드 물리 삭제(purge)를 요청할 수 있다.
3. THE 서버 SHALL purge 요청을 받은 record_id를 저장소에서 제거한다.

### Requirement 5: 병합 정합성 유지

**User Story:** 개발자로서, 증분 전환 후에도 기존 충돌 해소/소유권 이전 규칙이
그대로 동작하기를 원한다.

#### Acceptance Criteria
1. THE 클라이언트 SHALL 증분으로 받은 레코드에 기존 version 비교 병합 규칙
   (충돌 감지·conflict copy·tombstone 전파·소유권 이전 감지)을 그대로 적용한다.

### Requirement 6: 롱폴링 연동

**User Story:** 사용자로서, version 변경 통지를 받으면 변경분만 즉시 반영되기를
원한다.

#### Acceptance Criteria
1. WHEN 롱폴링이 version 변경을 통지하면 THE 클라이언트 SHALL 전체가 아닌 증분
   다운로드(since=last_synced_version)만 수행한다.

### Requirement 7: 하위 호환/전환

**User Story:** 사용자로서, 구버전 서버/클라이언트가 섞여 있어도 동기화가 깨지지
않기를 원한다.

#### Acceptance Criteria
1. IF 서버가 레코드 엔드포인트를 지원하지 않으면(404) THEN THE 클라이언트 SHALL
   기존 전체 blob 방식으로 fallback한다.
2. THE 서버 SHALL 기존 전체 blob 엔드포인트(`/sync/metadata`)를 당분간 유지한다.

### Requirement 8: 레코드 크기 패딩

**User Story:** 사용자로서, 서버가 레코드 암호문 크기로 경로 길이를 추정하지
못하기를 원한다.

#### Acceptance Criteria
1. THE 클라이언트 SHALL 레코드 평문을 암호화 전에 고정 블록 크기(256바이트)의
   배수로 패딩한다.
2. THE 클라이언트 SHALL 복호화 후 패딩을 제거해 원래 평문을 정확히 복원한다.
3. WHEN 레코드가 복원되면 THE 복원된 평문 SHALL 패딩 전 평문과 바이트 단위로 동일하다.

### Requirement 9: 비기능/제약

**User Story:** 개발자로서, 보안·정합성·플랫폼 제약을 지키기를 원한다.

#### Acceptance Criteria
1. THE 시스템 SHALL zero-knowledge를 유지한다(서버는 record_id·암호문·정수 version만 저장).
2. THE 시스템 SHALL 실패를 규격 에러로 반환한다("graceful 건너뛰기" 금지).
3. THE 코드 SHALL 프로덕션 Python 3.9와 호환된다(3.10+ 문법 금지), 파일은 LF+UTF8.

## 수용된 zero-knowledge 트레이드오프

전체 blob 대비 서버가 추가로 관측 가능한 정보:
- 레코드 개수(파일 수 근사)
- 개별 레코드 암호문 크기(메타데이터 크기 근사, 파일 내용 크기 아님)
- 레코드별 변경 빈도/시각과 고정 record_id(경로별 활동 패턴)

경로 평문·파일 내용은 여전히 노출되지 않는다. 이 트레이드오프를 수용하는 전제로 설계한다.
