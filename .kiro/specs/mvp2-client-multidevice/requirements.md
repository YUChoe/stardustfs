# Requirements Document

## Introduction

StardustFS 클라이언트의 MVP2 확장으로, 기존 단일 디바이스 WebDAV 클라이언트에 멀티디바이스 접근 및 중앙 서버 백업 기능을 추가한다. 사용자는 여러 PC에서 동일 계정으로 자신의 원격 스토리지에 접근하며, 중앙 서버는 인증/라우팅/메타데이터 백업만 담당하고 실제 파일 데이터는 PC 간 P2P로 직접 전송한다.

## Glossary

- **Auth_Client**: 중앙 서버 인증 API를 호출하여 JWT 토큰을 관리하는 모듈
- **Sync_Client**: 중앙 서버와 metadata_db 및 key_file을 동기화하는 모듈
- **Device_Manager**: 디바이스 등록, heartbeat 전송, 온라인 상태 관리를 담당하는 모듈
- **Remote_Source**: 다른 PC의 스토리지를 StorageSource 인터페이스로 래핑하는 구현체
- **P2P_Server**: 다른 디바이스의 파일 요청을 처리하는 경량 HTTP 서버
- **Conflict_Resolver**: 메타데이터 동기화 시 충돌을 감지하고 conflict copy를 생성하는 모듈
- **Central_Server**: StardustFS 중앙 서버 (인증, 디바이스 관리, 메타데이터/키 백업, 라우팅)
- **Config_Loader**: JSON 설정 파일을 파싱하고 검증하는 기존 모듈 (v2 확장)
- **Metadata_Store**: SQLite 기반 메타데이터 저장소 (버전 추적 컬럼 확장)
- **Key_Backup_Engine**: master_key를 사용자 비밀번호로 2차 암호화하여 백업 blob을 생성/복원하는 모듈

## Requirements

### Requirement 1: 중앙 서버 인증

**User Story:** 사용자로서, 중앙 서버에 로그인하여 JWT 토큰을 발급받고 싶다. 이를 통해 멀티디바이스 기능에 접근할 수 있다.

#### Acceptance Criteria

1. WHEN 사용자가 유효한 이메일 형식과 비밀번호를 제공하면, THE Auth_Client SHALL Central_Server의 POST /auth/login 엔드포인트를 호출하여 access_token과 refresh_token을 수신한다
2. WHEN 로그인이 성공하면, THE Auth_Client SHALL access_token과 refresh_token을 메모리에 저장한다
3. IF 로그인 요청에 대해 서버가 401 응답을 반환하면 (잘못된 자격 증명), THEN THE Auth_Client SHALL AuthenticationError를 발생시킨다
4. WHEN access_token 만료 시각이 현재 시각으로부터 1분 이내이면, THE Auth_Client SHALL refresh_token을 사용하여 POST /auth/refresh 엔드포인트를 호출하고 새 토큰 쌍을 수신한다
5. IF refresh_token 갱신이 실패하면 (401 응답), THEN THE Auth_Client SHALL 저장된 토큰을 삭제하고 재로그인이 필요함을 로그에 기록한다
6. IF refresh_token 갱신 시 네트워크 오류 또는 5xx 응답이 발생하면, THEN THE Auth_Client SHALL 기존 토큰을 유지하고 다음 요청 시 재시도한다
7. IF Central_Server에 10초 이내에 연결할 수 없으면, THEN THE Auth_Client SHALL 기존 로컬 기능(WebDAV)은 정상 동작하도록 오프라인 모드로 전환한다

### Requirement 2: 설정 파일 v2 지원

**User Story:** 사용자로서, 설정 파일에 서버 연결 정보와 멀티디바이스 옵션을 지정하고 싶다. 이를 통해 클라이언트가 중앙 서버 및 P2P 기능을 활성화할 수 있다.

#### Acceptance Criteria

1. THE Config_Loader SHALL version 필드가 2인 설정 파일을 파싱하여 StardustConfig 객체를 반환한다
2. IF version 필드가 2이면, THEN THE Config_Loader SHALL server, sync, p2p 섹션이 모두 존재하는지 검증하고, 하나라도 누락된 경우 누락된 섹션명을 포함하는 검증 에러를 반환한다
3. THE Config_Loader SHALL server.url 필드가 "https://" 스킴으로 시작하고 호스트명을 포함하는 URL인지 검증한다
4. THE Config_Loader SHALL server.device_name 필드가 1자 이상 64자 이하의 문자열인지 검증한다
5. THE Config_Loader SHALL sync.interval_seconds 필드가 10 이상 3600 이하의 정수인지 검증한다
6. THE Config_Loader SHALL sync.conflict_strategy 필드가 "copy" 값인지 검증한다
7. THE Config_Loader SHALL p2p.port 필드가 1024 이상 65535 이하의 정수인지 검증한다
8. THE Config_Loader SHALL p2p.enabled 필드가 boolean 값인지 검증한다
9. THE Config_Loader SHALL sources 배열에서 type이 "remote"인 항목의 device_id 필드가 RFC 4122 형식의 하이픈 포함 UUID 문자열(8-4-4-4-12)인지 검증한다
10. IF version 필드가 1이면, THEN THE Config_Loader SHALL 기존 v1 검증 로직을 그대로 적용한다 (하위 호환성)
11. IF 검증 에러가 1개 이상 존재하면, THEN THE Config_Loader SHALL 모든 에러를 문자열 리스트로 수집하여 반환하고, 유효한 설정 객체를 반환하지 않는다
12. IF version 필드가 누락되었거나 정수가 아니거나 1 또는 2가 아닌 값이면, THEN THE Config_Loader SHALL 지원되지 않는 버전임을 나타내는 검증 에러를 반환한다

### Requirement 3: 디바이스 등록 및 Heartbeat

**User Story:** 사용자로서, 클라이언트 시작 시 자동으로 디바이스를 등록하고 주기적으로 온라인 상태를 갱신하고 싶다. 이를 통해 다른 디바이스가 이 PC의 접속 가능 여부를 알 수 있다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작되고 인증이 완료되면, THE Device_Manager SHALL Central_Server의 POST /devices 엔드포인트를 호출하여 디바이스를 등록하고, 서버가 응답한 device_id를 로컬 설정 파일에 저장한다
2. WHEN 디바이스 등록 시, THE Device_Manager SHALL 설정 파일의 device_name(최대 64자), 운영체제 정보, P2P 접속 주소(IP:port)를 전송한다
3. IF 디바이스 등록 요청이 실패하면(네트워크 오류 또는 서버 4xx/5xx 응답), THEN THE Device_Manager SHALL 10초 간격으로 최대 5회 재시도하고, 모두 실패 시 등록 실패 상태를 로그에 기록하며 클라이언트를 오프라인 모드로 전환한다
4. WHILE 클라이언트가 실행 중이고 디바이스 등록이 완료된 상태이면, THE Device_Manager SHALL 60초마다 PUT /devices/{device_id}/heartbeat 엔드포인트를 호출한다
5. WHEN heartbeat 전송 시, THE Device_Manager SHALL 현재 P2P 접속 주소(IP:port)를 함께 갱신한다
6. IF heartbeat 전송이 3회 연속 실패하면(응답 타임아웃 10초 초과 또는 서버 5xx 응답), THEN THE Device_Manager SHALL 서버 연결 불가 상태를 로그에 기록하고 재시도 간격을 120초로 증가시킨다
7. WHEN 증가된 재시도 간격(120초) 중 heartbeat 전송이 성공하면(서버가 2xx 응답을 반환), THE Device_Manager SHALL 재시도 간격을 60초로 복원하고 정상 상태를 로그에 기록한다
8. IF 로컬에 이미 유효한 device_id가 캐시되어 있으면, THEN THE Device_Manager SHALL 신규 등록 대신 기존 device_id로 heartbeat를 전송하여 재등록을 생략한다

### Requirement 4: 메타데이터 동기화

**User Story:** 사용자로서, 여러 디바이스에서 동일한 파일 목록을 보고 싶다. 이를 통해 어떤 PC에서든 전체 파일 구조를 확인할 수 있다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작되면, THE Sync_Client SHALL Central_Server에서 최신 metadata_db를 다운로드한다 (GET /sync/metadata)
2. WHEN 서버의 metadata_db를 수신하면, THE Sync_Client SHALL 각 파일 레코드의 version 값을 비교하여 높은 version을 가진 쪽의 레코드를 채택하는 방식으로 로컬 metadata_db와 병합한다. version이 동일하고 device_id가 다르면 해당 레코드의 sync_status를 "conflict"로 설정한다
3. WHEN 로컬에서 파일이 생성, 수정, 또는 삭제되면, THE Sync_Client SHALL 해당 변경을 로컬 metadata_db에 1초 이내에 반영하고 sync_status를 "pending"으로 설정한다
4. WHILE 클라이언트가 실행 중이면, THE Sync_Client SHALL sync.interval_seconds(기본값 30) 간격으로 로컬 metadata_db 스냅샷을 Central_Server에 업로드한다 (PUT /sync/metadata)
5. THE Metadata_Store SHALL files 테이블에 version (INTEGER, 기본값 1), device_id (TEXT), sync_status (TEXT, 기본값 "synced") 컬럼을 포함한다
6. WHEN 파일이 로컬에서 변경되면, THE Metadata_Store SHALL 해당 레코드의 version을 1 증가시키고 device_id를 현재 디바이스 ID로 설정한다
7. IF 클라이언트 시작 시 Central_Server에 연결할 수 없으면, THEN THE Sync_Client SHALL 로컬 metadata_db를 그대로 사용하여 오프라인 모드로 동작하고, 연결이 복구된 후 다음 sync.interval_seconds 주기에 동기화를 재개한다
8. IF metadata_db 업로드가 실패하면, THEN THE Sync_Client SHALL sync_status를 "pending" 상태로 유지하고 다음 sync.interval_seconds 주기에 업로드를 재시도한다. 3회 연속 실패 시 오류를 로그에 기록한다

### Requirement 5: 메타데이터 병합 및 충돌 해결

**User Story:** 사용자로서, 두 디바이스에서 동시에 같은 파일을 수정해도 데이터가 유실되지 않기를 원한다. 이를 통해 양쪽 변경사항이 모두 보존된다.

#### Acceptance Criteria

1. WHEN 서버 metadata_db와 로컬 metadata_db를 병합할 때, IF 동일 virtual_path에 대해 서버 version이 로컬 base_version보다 크면, THEN THE Conflict_Resolver SHALL 해당 파일을 충돌로 판정한다
2. WHEN 충돌이 감지되면, THE Conflict_Resolver SHALL 로컬 버전의 파일을 "{원본이름} (conflict - {device_name} - {YYYY-MM-DD HH-MM-SS}).{확장자}" 형식으로 rename한다
3. WHEN 충돌이 감지되면, THE Conflict_Resolver SHALL 서버 버전의 메타데이터를 원본 virtual_path에 적용하고 로컬 version을 서버 version 값으로 갱신한다
4. WHEN conflict copy가 생성되면, THE Conflict_Resolver SHALL 해당 파일의 sync_status를 "conflict"로 설정한다
5. WHEN 서버 metadata_db와 로컬 metadata_db를 병합할 때, IF 충돌이 발생하지 않고 서버 version이 로컬 version보다 높으면, THEN THE Sync_Client SHALL 서버의 메타데이터로 로컬을 갱신하고 로컬 version을 서버 version 값으로 설정한다
6. WHEN 서버 metadata_db와 로컬 metadata_db를 병합할 때, IF 충돌이 발생하지 않고 로컬 version이 서버 version보다 높으면, THEN THE Sync_Client SHALL 로컬 메타데이터를 서버에 업로드한다
7. WHEN 서버 metadata_db와 로컬 metadata_db를 병합할 때, IF 동일 virtual_path에 대해 서버 version과 로컬 version이 동일하면, THEN THE Sync_Client SHALL 해당 파일에 대해 병합 작업을 수행하지 않는다
8. IF conflict copy rename 중 동일 파일명이 이미 존재하면, THEN THE Conflict_Resolver SHALL 파일명 끝에 순번 "(2)", "(3)" 등을 추가하여 고유한 파일명을 생성한다
9. IF conflict copy 생성 중 파일시스템 오류가 발생하면, THEN THE Conflict_Resolver SHALL 해당 파일의 sync_status를 "pending"으로 유지하고 오류를 나타내는 로그를 기록하며 다음 동기화 주기에서 재시도한다

### Requirement 6: Key 백업 및 복원

**User Story:** 사용자로서, master_key를 서버에 안전하게 백업하고 새 디바이스에서 복원하고 싶다. 이를 통해 디바이스 분실 시에도 데이터에 접근할 수 있다.

#### Acceptance Criteria

1. WHEN 사용자가 key 백업을 요청하면, THE Key_Backup_Engine SHALL 사용자 비밀번호(최소 8자)에서 PBKDF2-SHA256 (iterations=600000, salt=16바이트 랜덤)으로 32바이트 파생 키를 생성한다
2. WHEN 파생 키가 생성되면, THE Key_Backup_Engine SHALL master_key를 AES-256-GCM (IV=12바이트 랜덤)으로 암호화하여 salt(16바이트) + iv(12바이트) + tag(16바이트) + ciphertext 형식의 blob을 생성한다
3. WHEN 암호화된 blob이 생성되면, THE Sync_Client SHALL Central_Server의 PUT /sync/key 엔드포인트에 10초 이내에 업로드한다
4. WHEN 새 디바이스에서 key 복원을 요청하면, THE Sync_Client SHALL GET /sync/key 엔드포인트에서 암호화된 blob을 10초 이내에 다운로드한다
5. WHEN 암호화된 blob을 수신하면, THE Key_Backup_Engine SHALL 사용자 비밀번호로 파생 키를 재생성하고 AES-256-GCM으로 복호화하여 master_key를 복원한다
6. IF 복호화가 실패하면 (비밀번호 불일치 또는 blob 변조), THEN THE Key_Backup_Engine SHALL IntegrityError를 발생시키고 복호화 실패를 나타내는 에러 메시지를 반환한다
7. WHEN 유효한 master_key와 비밀번호 조합으로 암호화 후 동일 비밀번호로 복호화를 수행하면, THE Key_Backup_Engine SHALL 원본 master_key와 바이트 단위로 동일한 결과를 반환한다 (라운드트립 속성)
8. IF key 복원 요청 시 서버에 백업된 blob이 존재하지 않으면, THEN THE Sync_Client SHALL KeyNotFoundError를 발생시키고 백업이 존재하지 않음을 나타내는 에러 메시지를 반환한다
9. IF 업로드 또는 다운로드 중 네트워크 오류가 발생하면, THEN THE Sync_Client SHALL 최대 3회까지 재시도하고, 모든 재시도 실패 시 네트워크 오류를 나타내는 에러 메시지와 함께 예외를 발생시킨다

### Requirement 7: 원격 스토리지 소스

**User Story:** 사용자로서, 다른 PC의 스토리지를 로컬 WebDAV에서 접근하고 싶다. 이를 통해 원격 PC의 파일을 마치 로컬처럼 읽고 쓸 수 있다.

#### Acceptance Criteria

1. THE Remote_Source SHALL StorageSource 추상 클래스의 모든 메서드(read, write, delete, exists, mkdir, rmdir, list_dir, get_available_space, get_total_space)를 구현한다
2. WHEN Remote_Source가 초기화되면, THE Remote_Source SHALL Central_Server의 GET /routing/{device_id} 엔드포인트에서 대상 디바이스의 접속 주소를 조회하고, 조회 성공 시 활성 상태로 전환한다
3. WHEN read가 호출되면, THE Remote_Source SHALL 대상 디바이스의 P2P_Server에 POST /p2p/read 요청을 전송하고 파일 데이터를 bytes로 반환한다
4. WHEN write가 호출되면, THE Remote_Source SHALL 대상 디바이스의 P2P_Server에 POST /p2p/write 요청을 전송한다
5. WHEN delete가 호출되면, THE Remote_Source SHALL 대상 디바이스의 P2P_Server에 POST /p2p/delete 요청을 전송한다
6. WHEN list_dir가 호출되면, THE Remote_Source SHALL 대상 디바이스의 P2P_Server에 POST /p2p/list 요청을 전송하고 엔트리 이름 목록(list[str])을 반환한다
7. IF 초기화 시 GET /routing/{device_id} 요청이 실패하거나 대상 디바이스가 오프라인 상태를 반환하면, THEN THE Remote_Source SHALL 비활성 상태로 전환하고 사유를 로그에 기록한다
8. IF P2P 요청이 10초 내에 응답을 받지 못하면, THEN THE Remote_Source SHALL OSError를 발생시킨다
9. IF P2P 요청이 HTTP 4xx 또는 5xx 응답을 반환하면, THEN THE Remote_Source SHALL OSError를 발생시키고 응답 상태 코드를 에러 메시지에 포함한다
10. WHEN exists, mkdir, rmdir, get_available_space, get_total_space가 호출되면, THE Remote_Source SHALL 대상 디바이스의 P2P_Server에 해당 작업의 POST 요청을 전송하고 결과를 반환한다
11. IF Remote_Source가 비활성 상태에서 메서드가 호출되면, THEN THE Remote_Source SHALL OSError를 발생시킨다

### Requirement 8: P2P 파일 서버

**User Story:** 사용자로서, 다른 디바이스가 이 PC의 파일에 접근할 수 있도록 P2P 서버를 실행하고 싶다. 이를 통해 원격 디바이스가 로컬 스토리지를 읽고 쓸 수 있다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작될 때, IF p2p.enabled가 true이면, THEN THE P2P_Server SHALL 설정된 p2p.port(범위: 1024-65535)에서 aiohttp 기반 HTTP 서버를 시작한다
2. THE P2P_Server SHALL POST /p2p/read, POST /p2p/write, POST /p2p/delete, POST /p2p/list 엔드포인트를 제공한다
3. WHEN 요청을 수신하면, THE P2P_Server SHALL 요청 본문의 auth_token 필드에 대해 JWT 서명 검증 및 만료 시간 확인을 수행한다
4. IF auth_token이 누락되었거나 JWT 서명이 유효하지 않거나 만료되었으면, THEN THE P2P_Server SHALL 401 Unauthorised 응답을 반환한다
5. IF auth_token의 user_id가 로컬 사용자와 일치하지 않으면, THEN THE P2P_Server SHALL 403 Forbidden 응답을 반환한다
6. WHEN /p2p/read 요청이 유효하면, THE P2P_Server SHALL physical_path에 해당하는 파일 데이터를 응답 본문으로 반환한다
7. WHEN /p2p/write 요청이 유효하면, THE P2P_Server SHALL physical_path에 data를 기록하고 상위 디렉토리가 없으면 자동 생성한 뒤, 기록된 바이트 수를 포함한 JSON 성공 응답을 반환한다
8. WHEN /p2p/delete 요청이 유효하면, THE P2P_Server SHALL physical_path의 파일을 삭제하고 JSON 성공 응답을 반환한다
9. WHEN /p2p/list 요청이 유효하면, THE P2P_Server SHALL physical_path 디렉토리의 엔트리 목록을 JSON 배열로 반환한다
10. IF physical_path가 존재하지 않으면, THEN THE P2P_Server SHALL 404 Not Found 응답을 반환한다
11. IF physical_path가 소스 루트 경로 외부를 참조하면 (path traversal), THEN THE P2P_Server SHALL 400 Bad Request 응답을 반환한다
12. IF /p2p/write 요청의 data 크기가 100MB를 초과하면, THEN THE P2P_Server SHALL 413 Payload Too Large 응답을 반환한다
13. IF p2p.port가 이미 사용 중이어서 서버 시작에 실패하면, THEN THE P2P_Server SHALL 에러를 로깅하고 P2P 기능 없이 클라이언트를 계속 실행한다

### Requirement 9: P2P 통신 보안

**User Story:** 사용자로서, P2P 통신이 안전하게 보호되기를 원한다. 이를 통해 인가되지 않은 접근으로부터 파일을 보호할 수 있다.

#### Acceptance Criteria

1. THE P2P_Server SHALL 모든 요청에 대해 중앙 서버에 auth_token 검증을 위임하여 토큰의 유효성을 확인한다
2. IF auth_token이 없거나 만료되었거나 서명이 유효하지 않으면, THEN THE P2P_Server SHALL 해당 요청을 거부하고 401 Unauthorised 응답을 반환한다
3. IF auth_token의 user_id가 로컬 사용자의 user_id와 일치하지 않으면, THEN THE P2P_Server SHALL 해당 요청을 거부하고 403 Forbidden 응답을 반환한다
4. WHEN P2P 요청을 전송할 때, THE Remote_Source SHALL 현재 유효한 access_token을 auth_token 필드에 포함한다
5. IF access_token이 만료된 상태에서 P2P 요청이 필요하면, THEN THE Remote_Source SHALL Auth_Client를 통해 토큰을 갱신한 후 요청을 최대 1회 재시도한다
6. IF 토큰 갱신이 실패하면(refresh_token 만료 또는 중앙 서버 불가용), THEN THE Remote_Source SHALL P2P 요청을 중단하고 인증 갱신 실패를 나타내는 오류를 호출자에게 반환한다
7. IF P2P_Server가 토큰 검증을 위해 중앙 서버에 접근할 수 없으면, THEN THE P2P_Server SHALL 해당 요청을 거부하고 503 Service Unavailable 응답을 5초 이내에 반환한다

### Requirement 10: NAT 트래버설

**User Story:** 사용자로서, NAT 환경에서도 다른 디바이스가 이 PC에 접근할 수 있기를 원한다. 이를 통해 공유기 뒤에 있는 PC도 P2P 통신이 가능하다.

#### Acceptance Criteria

1. WHEN P2P_Server가 시작되면, THE Device_Manager SHALL UPnP를 사용하여 설정된 P2P 포트에 대한 외부 포트 매핑을 시도하되, 10초 이내에 응답이 없으면 매핑 실패로 간주한다
2. WHEN UPnP 포트 매핑이 성공하면, THE Device_Manager SHALL 매핑된 외부 IP와 외부 포트를 "IP:port" 형식으로 heartbeat의 connection_address 필드에 사용한다
3. IF UPnP 포트 매핑이 실패하면, THEN THE Device_Manager SHALL 로컬 네트워크 IP와 P2P 포트를 "IP:port" 형식으로 connection_address에 사용하고 로그에 WARNING 레벨로 UPnP 실패 사유를 기록한다
4. WHEN 클라이언트가 종료되면, THE Device_Manager SHALL UPnP 포트 매핑 해제를 시도한다
5. IF 클라이언트 종료 시 UPnP 포트 매핑 해제가 실패하면, THEN THE Device_Manager SHALL 로그에 WARNING 레벨로 해제 실패를 기록하고 종료 절차를 계속 진행한다

### Requirement 11: 오프라인 모드 및 복구

**User Story:** 사용자로서, 서버 연결이 끊어져도 로컬 파일 작업을 계속하고 싶다. 이를 통해 네트워크 장애 시에도 작업 중단 없이 사용할 수 있다.

#### Acceptance Criteria

1. IF Central_Server에 대한 연결 시도가 10초 이내에 응답을 받지 못하거나 HTTP 5xx 응답을 수신하면, THEN THE Sync_Client SHALL 해당 시점부터 발생하는 로컬 변경사항을 sync_status "pending"으로 표시하여 로컬 metadata_db에 축적한다
2. WHILE 오프라인 상태이면, THE Config_Loader SHALL 로컬 스토리지 소스(directory, loopback)에 대한 WebDAV 읽기, 쓰기, 삭제, 목록 조회 연산을 온라인 시와 동일하게 제공하고, remote 타입 소스에 대한 요청은 오류 응답을 반환한다
3. WHEN 서버 연결이 복구되면, THE Sync_Client SHALL 축적된 pending 변경사항을 생성 시각 순서대로 서버에 업로드하고, 업로드 완료 후 해당 항목의 sync_status를 "synced"로 갱신한다
4. WHEN 오프라인에서 온라인으로 전환되면, THE Sync_Client SHALL pending 변경사항 업로드 완료 후 서버의 최신 metadata_db를 다운로드하여 병합을 수행하고, 동일 파일에 대해 로컬과 서버의 version이 상이하면 conflict_resolver의 conflict copy 전략을 적용한다
5. IF pending 변경사항 업로드 중 서버 연결이 다시 끊어지면, THEN THE Sync_Client SHALL 업로드에 실패한 항목의 sync_status를 "pending" 상태로 유지하고, 다음 연결 복구 시 미완료 항목부터 업로드를 재개한다

### Requirement 12: 클라이언트 시작 흐름

**User Story:** 사용자로서, 클라이언트 시작 시 인증, 동기화, P2P 서버가 순차적으로 초기화되기를 원한다. 이를 통해 안정적인 시작 순서가 보장된다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작되면, THE StardustFS SHALL 다음 순서로 초기화를 수행한다: (1) 설정 로드 → (2) 로컬 스토리지 초기화 → (3) 인증 → (4) 디바이스 등록 → (5) 메타데이터 동기화 → (6) P2P 서버 시작 → (7) WebDAV 서버 시작
2. IF 인증 단계에서 실패하면 (서버 미응답 10초 타임아웃 포함), THEN THE StardustFS SHALL 오프라인 모드로 전환하고 (4) 디바이스 등록, (5) 메타데이터 동기화, (6) P2P 서버 시작 단계를 건너뛰어 WebDAV 서버를 시작하며, 로그에 경고를 기록한다
3. IF 메타데이터 동기화 단계에서 실패하면, THEN THE StardustFS SHALL 로컬 metadata_db만 사용하여 P2P 서버와 WebDAV 서버를 순서대로 시작하고, 로그에 경고를 기록한다
4. IF version 2 설정에 server 섹션이 없으면, THEN THE StardustFS SHALL (1) 설정 로드 → (2) 로컬 스토리지 초기화 → (7) WebDAV 서버 시작만 수행하는 오프라인 전용 모드로 동작한다
5. IF 설정 로드 또는 로컬 스토리지 초기화 단계에서 실패하면, THEN THE StardustFS SHALL 실패 원인을 로그에 기록하고 0이 아닌 종료 코드로 프로세스를 종료한다

### Requirement 13: MVP1 설정 파일 마이그레이션

**User Story:** 사용자로서, 기존 MVP1 설정 파일을 수동 편집 없이 MVP2 형식으로 자동 변환하고 싶다. 이를 통해 기존 환경에서 원활하게 업그레이드할 수 있다.

#### Acceptance Criteria

1. WHEN version 1 설정 파일이 감지되면, THE Config_Loader SHALL 사용자에게 마이그레이션 여부를 확인하지 않고 자동으로 v2 형식으로 변환한다
2. WHEN v1 → v2 마이그레이션 시, THE Config_Loader SHALL 기존 webdav, sources, metadata_db, key_file 필드를 그대로 보존한다
3. WHEN v1 → v2 마이그레이션 시, THE Config_Loader SHALL version 필드를 2로 갱신한다
4. WHEN v1 → v2 마이그레이션 시, THE Config_Loader SHALL server 섹션을 빈 상태(url: null)로 추가하여 오프라인 전용 모드로 동작하게 한다
5. WHEN v1 → v2 마이그레이션 시, THE Config_Loader SHALL sync 섹션을 기본값(interval_seconds: 30, conflict_strategy: "copy")으로 추가한다
6. WHEN v1 → v2 마이그레이션 시, THE Config_Loader SHALL p2p 섹션을 기본값(port: 9090, enabled: false)으로 추가한다
7. WHEN 마이그레이션이 완료되면, THE Config_Loader SHALL 원본 설정 파일을 "{원본파일명}.v1.bak" 형식으로 백업한 후 변환된 v2 설정을 원본 경로에 저장한다
8. IF 백업 파일 생성에 실패하면(디스크 공간 부족 등), THEN THE Config_Loader SHALL 마이그레이션을 중단하고 원본 파일을 변경하지 않으며 에러를 로그에 기록한다
9. IF 이미 "{원본파일명}.v1.bak" 파일이 존재하면, THEN THE Config_Loader SHALL 기존 백업을 덮어쓰지 않고 "{원본파일명}.v1.bak.{N}" 형식으로 순번을 부여한다

### Requirement 14: MVP1 메타데이터 DB 스키마 마이그레이션

**User Story:** 사용자로서, 기존 MVP1 메타데이터 DB를 MVP2 스키마로 자동 업그레이드하고 싶다. 이를 통해 기존 파일 메타데이터를 유지하면서 동기화 기능을 사용할 수 있다.

#### Acceptance Criteria

1. WHEN 클라이언트가 시작되고 metadata_db의 files 테이블에 version 컬럼이 존재하지 않으면, THE Metadata_Store SHALL 스키마 마이그레이션을 자동 수행한다
2. WHEN 스키마 마이그레이션 시, THE Metadata_Store SHALL files 테이블에 version (INTEGER NOT NULL DEFAULT 1), device_id (TEXT), sync_status (TEXT DEFAULT 'synced') 컬럼을 ALTER TABLE로 추가한다
3. WHEN 스키마 마이그레이션 시, THE Metadata_Store SHALL 기존 모든 레코드의 version을 1, device_id를 현재 디바이스 ID(또는 NULL), sync_status를 "synced"로 설정한다
4. WHEN 스키마 마이그레이션 시, THE Metadata_Store SHALL schema_version 테이블을 생성하고 현재 스키마 버전(2)을 기록한다
5. IF 스키마 마이그레이션 중 오류가 발생하면, THEN THE Metadata_Store SHALL 트랜잭션을 롤백하여 기존 스키마를 보존하고 에러를 로그에 기록한다
6. WHEN 마이그레이션 시작 전, THE Metadata_Store SHALL metadata_db 파일을 "{원본파일명}.v1.bak" 형식으로 복사하여 백업한다
7. IF 백업 파일 생성에 실패하면, THEN THE Metadata_Store SHALL 마이그레이션을 중단하고 에러를 로그에 기록한다
8. IF schema_version 테이블이 이미 존재하고 버전이 2 이상이면, THEN THE Metadata_Store SHALL 마이그레이션을 건너뛴다

### Requirement 15: 오프라인 리모트 파일 Placeholder 표시

**User Story:** 사용자로서, WebDAV 파일 목록에서 리모트 디바이스가 오프라인이라 접근 불가능한 파일을 placeholder로 확인하고, 접근 시 명확한 에러를 받고 싶다. 이를 통해 어떤 파일이 현재 사용 가능한지 즉시 파악할 수 있다.

#### Acceptance Criteria

1. WHEN WebDAV PROPFIND 요청으로 디렉토리 목록을 응답할 때, IF 파일의 source가 Remote_Source이고 해당 Remote_Source가 비활성(오프라인) 상태이면, THEN THE WebDAV_Provider SHALL 해당 파일을 placeholder로 목록에 포함한다
2. WHEN 오프라인 리모트 파일을 placeholder로 표시할 때, THE WebDAV_Provider SHALL 파일명을 "{원본파일명}.offline" 확장자로 변경하여 OS 파일 탐색기에서 알 수 없는 파일 타입 아이콘으로 표시되게 한다
3. WHEN 오프라인 리모트 파일을 placeholder로 표시할 때, THE WebDAV_Provider SHALL DAV:getcontentlength를 0으로 설정한다
4. WHEN 오프라인 리모트 파일을 placeholder로 표시할 때, THE WebDAV_Provider SHALL DAV:getlastmodified를 메타데이터에 기록된 원본 수정 시각으로 설정한다
5. WHEN 오프라인 placeholder 파일에 대해 WebDAV GET(읽기) 요청이 수신되면, THE WebDAV_Provider SHALL HTTP 503 Service Unavailable 응답을 반환하고 "Remote device is offline" 메시지를 포함한다
6. WHEN 오프라인 placeholder 파일에 대해 WebDAV PUT(쓰기) 요청이 수신되면, THE WebDAV_Provider SHALL HTTP 503 Service Unavailable 응답을 반환하고 "Remote device is offline" 메시지를 포함한다
7. WHEN 오프라인 placeholder 파일에 대해 WebDAV DELETE 요청이 수신되면, THE WebDAV_Provider SHALL HTTP 503 Service Unavailable 응답을 반환한다
8. WHEN 이전에 오프라인이었던 Remote_Source가 다시 활성(온라인) 상태로 전환되면, THE WebDAV_Provider SHALL ".offline" 확장자를 제거하고 실제 파일 속성(크기, 수정 시각)을 반환한다
9. IF 설정에서 remote source가 없거나 모든 remote source가 온라인이면, THEN THE WebDAV_Provider SHALL 모든 파일을 원본 파일명으로 정상 표시한다
10. WHEN WebDAV PROPFIND 응답에서 오프라인 placeholder 파일의 속성을 반환할 때, THE WebDAV_Provider SHALL 커스텀 속성 stardust:availability를 "offline"으로, stardust:original-name을 원본 파일명으로 설정한다

### Requirement 16: Tombstone 정리(GC) 및 장기 오프라인 디바이스 재조정

**User Story:** 사용자로서, 삭제된 파일의 tombstone이 메타데이터에 무한히 쌓이지 않고, 오랫동안 사용하지 않던 디바이스를 다시 켰을 때 삭제된 파일이 되살아나지 않기를 원한다. 이를 통해 메타데이터 크기를 일정하게 유지하고 멀티디바이스 간 삭제 일관성을 보장한다.

**전제:** StardustFS는 클라이언트 구동 중에만 WebDAV로 파일 접근이 가능하다. 클라이언트가 종료된 디바이스("오프라인 디바이스")에서는 파일 생성·수정·삭제가 발생할 수 없으며, 종료 직전 마지막 동기화 사이클에서 모든 변경이 서버에 반영(pending 없음)된다. 단, 종료 직전 사이클에서 동기화가 실패한 경우에만 pending 변경이 로컬에 잔존할 수 있다.

#### Acceptance Criteria

1. THE 중앙 서버 SHALL tombstone 보관기간(retention_days)을 설정값으로 보유하며, 기본값은 30일이다 (환경변수 STARDUST_TOMBSTONE_RETENTION_DAYS로 조정 가능)
2. WHEN 클라이언트가 GET /sync/metadata/status를 호출하면, THE 중앙 서버 SHALL 응답에 tombstone_retention_days 필드를 포함하여 정책값을 전달한다
3. WHEN 클라이언트가 메타데이터를 병합하거나 업로드하기 전에, THE SyncClient SHALL deleted=1이고 modified_at이 (현재시각 - retention_days)보다 오래된 tombstone 레코드를 로컬 메타데이터에서 물리적으로 제거(GC)한다
4. THE tombstone GC는 메타데이터 blob을 복호화할 수 있는 클라이언트에서만 수행되며, 서버는 암호화된 blob을 보관할 뿐 GC를 직접 수행하지 않는다
5. WHEN 클라이언트가 시작될 때, THE SyncClient SHALL 마지막으로 성공한 동기화 시각(last_sync_at)을 로컬에 보존된 값에서 읽는다
6. IF 현재 시각과 last_sync_at의 차이가 retention_days를 초과하면(장기 오프라인 디바이스), THEN THE SyncClient SHALL 해당 디바이스를 stale 상태로 판정하고 재조정(re-baseline) 절차를 수행한다
7. WHEN stale 재조정을 수행할 때, IF 로컬에 pending 변경이 없으면, THEN THE SyncClient SHALL 로컬 메타데이터를 서버의 정본으로 전면 교체(전체 재수신)한다
8. WHEN stale 재조정을 수행할 때, IF 로컬에 pending 변경이 존재하면(종료 직전 동기화 실패), THEN THE SyncClient SHALL 해당 pending 레코드를 conflict copy 경로로 격리 보존한 뒤 서버 정본을 전면 채택하고, 격리한 변경을 신규로 업로드한다
9. WHEN 동기화가 성공적으로 완료될 때마다, THE SyncClient SHALL last_sync_at을 현재 시각으로 갱신하여 로컬에 보존한다
10. THE stale 판정 임계값은 tombstone 보관기간(retention_days)과 동일한 값을 사용하여, retention_days 이내에 동기화한 디바이스는 GC되지 않은 모든 tombstone을 관측했음을 보장한다
