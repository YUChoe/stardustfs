# Requirements Document

## Introduction

현재 복제 청크에는 내용 해시가 없다. `chunk_id`는 `SHA-256(file_ref:idx)`로 만든
위치 식별자일 뿐 내용과 무관하다. 그래서 홀더가 손상되거나 잘못된 바이트를 돌려주면,
`recover`가 모든 청크를 결합해 복호화하는 마지막 단계에서 AES-GCM 인증 태그 실패로만
드러난다. 결과적으로 (1) 어느 청크가 문제인지 특정할 수 없고, (2) 그 청크만 다른
홀더에서 다시 받는 부분 재시도가 불가능하며, (3) `heal`이 손상된 청크를 그대로 새
홀더로 복사해 오염을 전파할 수 있다.

이 기능은 청크마다 암호문 내용 해시를 등록·검증해 손상을 받는 즉시 감지하고, 해당
청크만 다른 홀더에서 재요청하도록 한다.

## Glossary

- chunk_id: 위치 식별자. `SHA-256(file_ref:idx)`. 내용과 무관하며 기존 정의 유지.
- chunk_hash: 청크 암호문 바이트의 `SHA-256` hex. 내용 검증용으로 신규 도입.
- 홀더(holder): 청크 암호문을 보관하는 디바이스.

## Requirements

### Requirement 1: 청크 해시 등록

**User Story:** 사용자로서, 백업한 청크의 내용 해시가 함께 기록되기를 원한다. 그래야
나중에 받은 청크가 원본과 같은지 확인할 수 있다.

#### Acceptance Criteria
1. WHEN 클라이언트가 청크를 복제하면 THE 클라이언트 SHALL 그 청크 암호문의
   `SHA-256` hex를 계산해 서버 청크 등록에 포함한다.
2. THE 서버 SHALL 청크 레코드에 chunk_hash를 저장한다.
3. THE 서버 SHALL 청크 목록 조회 응답에 chunk_hash를 포함한다.
4. THE 서버 SHALL chunk_hash를 불투명 문자열로 취급한다(내용 해석·복호화 불가).

### Requirement 2: 복구 시 검증과 부분 재시도

**User Story:** 사용자로서, 손상된 사본을 가진 홀더가 있어도 다른 홀더에서 받아
파일을 복구하기를 원한다.

#### Acceptance Criteria
1. WHEN 복구 중 홀더에서 청크를 받으면 THE 클라이언트 SHALL 받은 바이트의 해시를
   등록된 chunk_hash와 비교한다.
2. IF 해시가 불일치하면 THEN THE 클라이언트 SHALL 그 홀더의 응답을 버리고 같은
   청크를 다음 홀더에서 재요청한다.
3. IF 모든 홀더가 불일치하거나 도달 불가면 THEN THE 클라이언트 SHALL 누락 chunk_id를
   명시한 규격 에러(RecoveryError)를 낸다.
4. WHERE 등록된 chunk_hash가 없는 청크(레거시)에서는 THE 클라이언트 SHALL 검증을
   건너뛰고 기존 동작을 유지한다.

### Requirement 3: 재복제 시 오염 전파 방지

**User Story:** 개발자로서, 재복제가 손상된 청크를 새 홀더로 퍼뜨리지 않기를 원한다.

#### Acceptance Criteria
1. WHEN heal이 온라인 홀더에서 청크를 받아 새 홀더로 복사하면 THE 클라이언트 SHALL
   복사 전에 chunk_hash를 검증한다.
2. IF 검증에 실패하면 THEN THE 클라이언트 SHALL 그 홀더를 소스로 쓰지 않고 다음
   온라인 홀더를 시도한다.
3. IF 유효한 소스가 없으면 THEN THE 클라이언트 SHALL 그 청크를 unrecoverable로
   보고한다(조용한 성공 처리 금지).

### Requirement 4: 하위 호환

**User Story:** 사용자로서, 이미 백업된 파일이 이 변경으로 깨지지 않기를 원한다.

#### Acceptance Criteria
1. THE 서버 SHALL chunk_hash가 없는 기존 청크 레코드를 그대로 유지한다(마이그레이션은
   컬럼 추가만, 기존 행은 NULL).
2. IF 서버가 chunk_hash를 반환하지 않으면(구버전) THEN THE 클라이언트 SHALL 검증을
   건너뛰고 정상 동작한다.
3. WHEN 기존 파일이 다시 복제되면 THE 클라이언트 SHALL 그때 chunk_hash를 채운다.

### Requirement 5: 비기능/제약

**User Story:** 개발자로서, 보안·정합성·플랫폼 제약을 지키기를 원한다.

#### Acceptance Criteria
1. THE 시스템 SHALL zero-knowledge를 유지한다. chunk_hash는 암호문의 해시이므로
   평문 내용을 노출하지 않는다.
2. THE 시스템 SHALL 검증 실패를 조용히 넘기지 않고 다음 홀더 시도 또는 규격 에러로
   처리한다.
3. THE 코드 SHALL 프로덕션 Python 3.9와 호환된다(3.10+ 문법 금지), 파일은 LF+UTF8.

## 수용된 트레이드오프

chunk_hash를 서버에 두면 서버가 동일 암호문 청크를 식별할 수 있다(같은 해시 = 같은
암호문). AES-GCM은 매번 다른 nonce를 쓰므로 같은 평문이라도 암호문이 달라져 실질적인
정보 누출은 거의 없다. 검증 주체가 소유자 클라이언트여야 하므로 해시는 소유자가
접근 가능한 곳(서버 레지스트리)에 있어야 한다.
