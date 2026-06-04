# Requirements: 메타데이터 버전 롱폴링 이벤트 (즉시 동기화)

## Introduction

현재 클라이언트는 30초 주기 폴링으로 서버 메타데이터 version을 확인한다. 이 때문에
한 디바이스의 변경(예: 소유권 이전)이 다른 디바이스에 반영되기까지 최대 30초+
지연이 발생한다.

서버에 메타데이터 version 변경을 롱폴링으로 알리는 엔드포인트를 추가하여, 변경
즉시(폴링 대기 없이) 다른 디바이스가 다운로드·병합하도록 한다. 이로써 체감 동기화
지연을 크게 줄인다.

zero-knowledge를 유지한다: 서버는 version(정수)과 알림만 다루며, 메타데이터 내용은
기존처럼 불투명 암호 blob으로만 저장·전송한다. 이 변경은 전송량을 줄이지 않으며
(파셜 전송은 별도 범위), "변경 발생 시점을 빠르게 통지"하는 것이 목적이다.

## 용어

- known_version: 클라이언트가 마지막으로 알고 있는 서버 metadata version
- 버전 대기(version wait): 서버 version > known_version이 될 때까지 대기하는 롱폴

## Requirements

### Requirement 1: 서버 버전 롱폴링 엔드포인트

User Story: 클라이언트로서, 내가 아는 version보다 서버 version이 올라가면 즉시
통지받아 빠르게 다운로드하고 싶다.

#### Acceptance Criteria

1. WHEN 클라이언트가 `GET /sync/metadata/wait?known_version=N`을 호출하면 THEN
   서버 version이 N보다 클 경우 즉시 현재 version을 반환한다.
2. WHEN 호출 시 서버 version이 N과 같으면 THEN 서버는 version이 증가할 때까지
   (타임아웃 내) 대기 후, 증가하면 새 version을 반환한다.
3. WHEN 타임아웃까지 version이 증가하지 않으면 THEN 현재 version을 그대로 반환한다
   (변경 없음 신호). 클라이언트는 즉시 재대기한다.
4. WHEN 다른 디바이스가 메타데이터를 업로드하여 version이 올라가면 THEN 대기 중인
   모든 해당 사용자의 롱폴러가 깨어나 새 version을 받는다.
5. 엔드포인트는 인증(JWT)을 요구하며, 자기 사용자(user_id)의 version만 조회한다.
6. WHEN 서버에 아직 메타데이터가 없으면(version 없음) THEN version 0으로 간주한다.

### Requirement 2: 업로드 시 버전 변경 통지

User Story: 시스템으로서, 메타데이터 업로드로 version이 증가하면 대기 중인
롱폴러를 즉시 깨워야 한다.

#### Acceptance Criteria

1. WHEN upload_metadata가 version을 증가시키면 THEN 해당 user_id의 버전 대기자에게
   알림을 보낸다(깨운다).
2. WHEN CAS 충돌로 version이 증가하지 않으면 THEN 알림을 보내지 않는다.
3. 알림은 메모리 기반이며(단일 워커 가정), 메타데이터 내용을 포함하지 않는다
   (version 정수만).

### Requirement 3: 클라이언트 즉시 동기화

User Story: 클라이언트로서, 버전 증가를 통지받으면 즉시 다운로드·병합하여 다른
디바이스의 변경을 빠르게 반영하고 싶다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작되면 THEN 백그라운드 버전 대기 루프를 시작하여
   `/sync/metadata/wait`를 반복 호출한다.
2. WHEN 새 version을 통지받으면 THEN 즉시 다운로드·병합(_download_and_merge)을
   수행하고 known_version을 갱신한다.
3. WHEN 롱폴 호출이 실패(네트워크 오류)하면 THEN 재시도하며, 전체 동작을 막지
   않는다(기존 주기 폴링이 안전망으로 계속 동작).
4. WHEN 클라이언트가 종료되면 THEN 버전 대기 루프도 정상 종료한다.
5. 기존 주기적 동기화(_periodic_loop)는 안전망으로 유지한다(롱폴 실패 대비).

### Requirement 4: 안전성 및 하위 호환

#### Acceptance Criteria

1. WHEN 서버가 wait 엔드포인트를 지원하지 않으면(구버전, 404) THEN 클라이언트는
   버전 대기 루프를 비활성화하고 기존 주기 폴링만 사용한다.
2. 롱폴링 타임아웃은 리버스 프록시 한계(통상 60초) 이내로 설정한다.
3. 버전 대기 루프와 주기 폴링이 동시에 다운로드·병합해도 결과는 일관된다
   (version 비교 병합은 멱등적).
