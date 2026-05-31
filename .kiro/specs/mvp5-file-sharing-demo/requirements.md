# Requirements Document

MVP5 파일 공유 데모 (최소 슬라이스)

> ⚠️ 방향 결정(2026-05): "평문 사용자 간 파일 공유"로서의 MVP5는 폐기되었다.
> 교차 계정 P2P 전송은 보안상 암호화된 복제/패리티 청크에 한해서만 허용하며,
> 평문 파일을 타 계정에 직접 건네는 기능은 구현하지 않는다. 본 스펙과 구현은
> "교차 계정 토큰 인가 + 경로 격리"의 PoC로 보존하며, 이 인프라는 MVP3(암호화
> 리플리케이션)의 복제 노드 접근 인가로 승계된다. 데모에서 전송된 바이트는
> 소유자 키로 암호화된 상태였으므로(평문 미전달) 보안 모델에 위배되지 않는다.

## Introduction

사용자 간 파일 공유(MVP5)의 실사용 데모/검증을 목적으로 하는 최소 기능 슬라이스다. 소유자(사용자 A)가 자신의 파일 하나에 대해 읽기 전용 공유 토큰을 발급하고, 다른 사용자(사용자 B)가 그 토큰으로 중앙 서버를 거치지 않고 A의 P2P 서버에서 직접 파일을 읽는다.

기존 MVP2 인프라(P2PServer, RemoteSource, 중앙 서버 /auth/verify, routing)를 재사용하며, 교차 사용자 인가 레이어만 최소한으로 추가한다. 그룹 공유, 읽기-쓰기 권한, TURN/STUN, 과금, 디렉토리 단위 공유는 본 슬라이스의 범위가 아니다.

## Glossary

- **공유 토큰(Share_Token):** 특정 디바이스의 특정 물리 경로 하나에 대한 읽기 접근을 인가하는 불투명 토큰. 만료 시각을 가진다.
- **소유자(Owner):** 공유 대상 파일을 보유하고 공유 토큰을 발급하는 사용자.
- **수신자(Recipient):** 공유 토큰을 받아 파일을 읽는 다른 사용자.
- **중앙 서버:** 공유 토큰을 발급·저장·조회하는 StardustFS 중앙 서버.
- **P2P 서버:** 소유자 디바이스에서 동작하며 파일 read 요청을 처리하는 서버.

## Requirements

### Requirement 1: 공유 토큰 발급

**User Story:** 소유자로서, 내 파일 하나에 대해 읽기 전용 공유 토큰을 발급하여 다른 사용자에게 전달하고 싶다. 이를 통해 중앙 서버를 거치지 않고 파일을 직접 공유할 수 있다.

#### Acceptance Criteria

1. WHEN 인증된 소유자가 POST /shares에 device_id, physical_path, expires_in_seconds를 담아 요청하면, THE 중앙 서버 SHALL 고유한 share_token을 생성하여 shares 레코드로 저장하고 share_token과 만료 시각을 반환한다
2. WHEN 공유 토큰을 생성할 때, THE 중앙 서버 SHALL 해당 device_id가 요청한 소유자의 소유인지 검증하고, 소유가 아니면 HTTP 403을 반환한다
3. WHEN 공유 토큰을 저장할 때, THE 중앙 서버 SHALL owner_user_id, device_id, physical_path, expires_at을 함께 기록한다
4. IF expires_in_seconds가 누락되거나 유효 범위(1초 ~ 30일)를 벗어나면, THEN THE 중앙 서버 SHALL HTTP 422를 반환한다

### Requirement 2: 공유 토큰 조회

**User Story:** 수신자로서, 전달받은 공유 토큰으로 어느 디바이스의 어떤 경로에 접근해야 하는지 조회하고 싶다. 이를 통해 대상 P2P 서버에 직접 연결할 수 있다.

#### Acceptance Criteria

1. WHEN 인증된 수신자가 GET /shares/{share_token}을 요청하면, THE 중앙 서버 SHALL 해당 토큰의 device_id와 만료 여부를 반환한다 (physical_path는 노출하지 않고 토큰 내부에서만 사용)
2. IF share_token이 존재하지 않으면, THEN THE 중앙 서버 SHALL HTTP 404를 반환한다
3. IF share_token이 만료되었으면, THEN THE 중앙 서버 SHALL HTTP 410 Gone을 반환한다
4. WHEN 수신자가 대상 디바이스의 접속 주소를 알아야 할 때, THE 중앙 서버 SHALL 기존 GET /routing/{device_id}로 접속 주소를 제공하되, 유효한 share_token 보유자에 대해서는 소유권 검증을 우회하여 해당 device_id의 라우팅 정보를 반환한다

### Requirement 3: 공유 토큰 기반 P2P 읽기 인가

**User Story:** 수신자로서, 공유 토큰으로 소유자의 P2P 서버에서 해당 파일만 읽고 싶다. 이를 통해 소유자의 다른 파일은 접근할 수 없으면서 공유된 파일만 안전하게 받을 수 있다.

#### Acceptance Criteria

1. WHEN P2P 서버가 /p2p/read 요청을 받을 때, IF 요청에 유효한 share_token이 포함되어 있으면, THEN THE P2P_Server SHALL user_id 일치 검증을 우회하고 읽기를 허용한다
2. WHEN share_token 기반 읽기를 인가할 때, THE P2P_Server SHALL 중앙 서버에 share_token의 유효성(존재·미만료)과 토큰에 묶인 physical_path를 검증 위임한다
3. WHEN share_token으로 읽기를 허용할 때, THE P2P_Server SHALL 요청된 physical_path가 토큰에 묶인 physical_path와 정확히 일치하는 경우에만 허용하고, 불일치 시 HTTP 403을 반환한다
4. IF share_token이 만료되었거나 존재하지 않으면, THEN THE P2P_Server SHALL HTTP 401을 반환한다
5. WHEN share_token 기반 읽기 요청이 들어와도, THE P2P_Server SHALL 기존 path traversal 방지 검증(Requirement 8.11, MVP2)을 동일하게 적용한다
6. THE share_token 기반 인가는 /p2p/read에만 적용되며, /p2p/write·/p2p/delete 등 변경 작업에는 적용되지 않는다 (읽기 전용)

### Requirement 4: 데모 검증

**User Story:** 개발자로서, 사용자 A 발급 → 사용자 B 읽기 전체 흐름을 자동으로 검증하고 싶다. 이를 통해 사용자 간 공유가 실제로 동작함을 확인할 수 있다.

#### Acceptance Criteria

1. WHEN 사용자 A가 파일을 공유하고 사용자 B가 share_token으로 읽으면, THE 통합 테스트 SHALL 원본과 동일한 파일 내용을 B가 수신함을 확인한다
2. WHEN share_token이 만료된 후 B가 읽기를 시도하면, THE 통합 테스트 SHALL 거부됨을 확인한다
3. WHEN B가 토큰에 묶이지 않은 다른 경로를 share_token으로 읽으려 하면, THE 통합 테스트 SHALL 403으로 거부됨을 확인한다
4. THE 데모 검증은 별도 테스트 계정(사용자 A, 사용자 B)을 사용하며 실제 사용자 계정을 오염시키지 않는다
