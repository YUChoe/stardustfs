# Requirements: P2P 릴레이 Fallback (long-polling)

## Introduction

이중 NAT/CGNAT 환경에서는 두 디바이스가 같은 최상위 공인 IP를 공유하더라도 서로
다른 하위 NAT에 격리되어 직접 P2P 연결 경로가 존재하지 않는다. UPnP·reflexive
공인 IP 보정으로도 인바운드 포트를 열 수 없어 직접 연결이 실패한다(실측 확인).

이를 해결하기 위해 중앙 서버를 경유하는 HTTP long-polling 릴레이를 도입한다. 양쪽
디바이스가 모두 outbound 연결만 사용하므로 NAT 종류와 무관하게 동작한다. 데이터는
클라이언트에서 master_key로 이미 암호화되므로 서버는 암호문만 중계한다
(zero-knowledge 유지).

릴레이는 직접 P2P 연결의 대체가 아니라 fallback이다. RemoteSource는 직접 연결을
먼저 시도하고, 실패 시 릴레이로 전환한다.

## 용어

- 요청자(requester): 원격 파일을 읽으려는 디바이스 (예: PC-A)
- 대상(target): 파일을 보유한 디바이스 (예: PC-B)
- 릴레이 요청(relay request): 요청자가 서버에 올리는 P2P 작업 요청
- 릴레이 응답(relay response): 대상이 처리 후 서버에 올리는 결과

## Requirements

### Requirement 1: 서버 릴레이 중계 (요청자 측)

User Story: 요청자로서, 대상 디바이스에 직접 연결할 수 없을 때 서버를 통해 P2P
작업을 전달하고 결과를 받고 싶다.

#### Acceptance Criteria

1. WHEN 요청자가 `POST /relay/request`로 (target_device_id, op, payload)를 올리면
   THEN 서버는 고유한 request_id를 발급하고 대상별 대기열에 적재한다.
2. WHEN 요청자가 `GET /relay/response/{request_id}`를 long-poll로 호출하면 THEN
   서버는 해당 요청의 응답이 도착할 때까지(타임아웃 내) 대기 후 응답을 반환한다.
3. WHEN 응답이 타임아웃 내 도착하지 않으면 THEN 서버는 504를 반환한다.
4. WHEN 요청자와 대상의 user_id가 다르면 THEN 서버는 403을 반환한다 (같은 유저
   디바이스 간만 허용).
5. WHEN target_device_id가 존재하지 않으면 THEN 서버는 404를 반환한다.

### Requirement 2: 서버 릴레이 중계 (대상 측)

User Story: 대상으로서, 인바운드 연결을 받을 수 없으므로 서버에 대기하여 나에게
온 요청을 받아 처리하고 결과를 올리고 싶다.

#### Acceptance Criteria

1. WHEN 대상이 `GET /relay/poll`을 long-poll로 호출하면 THEN 서버는 그 디바이스
   앞으로 온 요청이 있을 때까지(타임아웃 내) 대기 후 (request_id, op, payload)를
   반환한다.
2. WHEN 대기열에 요청이 없이 타임아웃되면 THEN 서버는 빈 결과(204 또는 빈 본문)를
   반환하고, 대상은 즉시 재폴링한다.
3. WHEN 대상이 `POST /relay/response/{request_id}`로 결과를 올리면 THEN 서버는
   그 결과를 대기 중인 요청자에게 전달한다.
4. WHEN 대상의 토큰 user_id가 요청의 target user_id와 다르면 THEN 서버는 403을
   반환한다.
5. 모든 relay 엔드포인트는 인증(JWT)을 요구한다.

### Requirement 3: 페이로드 불투명성 및 보안

User Story: 사용자로서, 서버가 내 파일 내용을 보지 못한 채 중계만 하길 바란다.

#### Acceptance Criteria

1. WHEN 릴레이가 payload를 중계하면 THEN 서버는 payload를 불투명 blob으로 취급하고
   내용을 해석하거나 저장(영속화)하지 않는다.
2. WHEN 릴레이로 전달되는 파일 데이터는 THEN 이미 master_key로 암호화된 암호문이다
   (서버는 평문을 볼 수 없음).
3. 릴레이 요청/응답은 메모리 대기열에만 존재하며 처리 후 즉시 제거된다.
4. WHEN op가 쓰기 계열(write/delete/mkdir/rmdir)이면 THEN 기존 P2P 인가 규칙과
   동일하게 같은 유저 디바이스 간에만 허용한다. 교차 사용자 릴레이는 허용하지
   않는다.

### Requirement 4: 클라이언트 직접연결 → 릴레이 Fallback

User Story: 요청자로서, 직접 연결이 가능하면 그 경로를(빠름), 불가능하면 릴레이를
자동으로 사용하고 싶다.

#### Acceptance Criteria

1. WHEN RemoteSource가 P2P 작업을 수행하면 THEN 먼저 직접 연결(peer_address)을
   시도한다.
2. WHEN 직접 연결이 연결 실패/타임아웃이면 THEN 동일 요청을 서버 릴레이로 전환한다.
3. WHEN 직접 연결이 성공하면 THEN 릴레이를 사용하지 않는다.
4. WHEN 릴레이도 실패하면 THEN OSError를 발생시킨다 (조용한 건너뛰기 금지).
5. 릴레이 경유 여부와 무관하게 read/write/list/exists 등 동작 결과는 직접 연결과
   동일하다.

### Requirement 5: 대상 측 릴레이 워커

User Story: 대상 디바이스로서, 클라이언트 구동 중 백그라운드로 릴레이 요청을
수신·처리하고 싶다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작되고 P2P가 활성화되면 THEN 백그라운드 릴레이 워커가
   `/relay/poll` 루프를 시작한다.
2. WHEN 릴레이 요청을 수신하면 THEN 기존 P2P 핸들러와 동일한 로직(소스 선택, 경로
   검증, 읽기/쓰기)으로 처리한 뒤 결과를 `/relay/response`로 올린다.
3. WHEN 처리 중 오류(파일 없음 등)가 발생하면 THEN 오류를 응답 결과에 담아 올린다
   (요청자에게 규격 오류로 전달).
4. WHEN 클라이언트가 종료되면 THEN 릴레이 워커도 정상 종료한다.
5. 릴레이 워커 폴링 실패(서버 일시 장애)는 재시도하며, 전체 동작을 막지 않는다.
