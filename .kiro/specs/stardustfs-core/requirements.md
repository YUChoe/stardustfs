# Requirements Document

## Introduction

StardustFS는 PC 및 Linux 환경에서 동작하는 WebDAV 기반 암호화 가상 파일시스템이다. 복수의 물리적 디렉토리 또는 루프백 파일(iso/dmg 형태)을 JBOD(Just a Bunch of Disks) 방식으로 통합하여 단일 WebDAV 서비스로 제공한다. 모든 저장 데이터는 AES-256으로 암호화되며, 메타데이터는 SQLite에 저장된다. WebDAV 서버는 wsgidav 라이브러리를 사용하여 구현한다.

## Glossary

- **StardustFS**: WebDAV 기반 암호화 가상 파일시스템 전체 시스템
- **WebDAV_Server**: wsgidav 라이브러리 기반의 WebDAV 서비스 모듈
- **DAVProvider**: wsgidav의 DAVProvider를 확장하여 JBOD 통합 뷰를 제공하는 커스텀 프로바이더
- **Encryption_Engine**: AES-256 기반 파일 암호화/복호화를 수행하는 모듈
- **JBOD_Manager**: 복수의 스토리지 소스를 단일 논리 볼륨으로 통합하는 모듈
- **Storage_Source**: JBOD에 참여하는 개별 스토리지 단위 (디렉토리 또는 루프백 파일)
- **Directory_Source**: 로컬 파일시스템 디렉토리를 스토리지 소스로 사용하는 유형
- **Loopback_Source**: 고정 크기 파일을 루프백 파일시스템으로 마운트하여 스토리지 소스로 사용하는 유형
- **Metadata_Store**: SQLite 기반 메타데이터 저장소
- **Virtual_Path**: WebDAV 클라이언트가 접근하는 논리적 경로
- **Physical_Path**: 실제 Storage_Source 내의 파일시스템 경로
- **Encryption_Key**: AES-256 암호화에 사용되는 256비트 대칭 키

## Requirements

### Requirement 1: WebDAV 서비스

**User Story:** As a 사용자, I want PC 또는 Linux에서 WebDAV 서버로 파일에 접근하기를, so that OS의 네트워크 드라이브 기능으로 암호화된 파일시스템을 사용할 수 있다.

#### Acceptance Criteria

1. WHEN StardustFS가 시작되면, THE WebDAV_Server SHALL wsgidav 라이브러리를 사용하여 설정된 호스트와 포트에서 WebDAV 서비스를 시작한다
2. THE WebDAV_Server SHALL WebDAV 프로토콜의 기본 메서드를 지원한다: GET, PUT, DELETE, MKCOL, MOVE, COPY, PROPFIND, PROPPATCH
3. WHEN WebDAV 클라이언트가 파일을 요청하면, THE DAVProvider SHALL JBOD_Manager를 통해 파일을 조회하고 Encryption_Engine으로 복호화하여 반환한다
4. IF WebDAV 클라이언트가 존재하지 않는 Virtual_Path의 파일을 요청하면, THEN THE DAVProvider SHALL HTTP 404 Not Found 응답을 반환한다
5. WHEN WebDAV 클라이언트가 파일을 업로드하면, THE DAVProvider SHALL Encryption_Engine으로 암호화한 후 JBOD_Manager를 통해 여유 공간이 있는 Storage_Source에 저장한다
6. WHEN WebDAV 클라이언트가 이미 존재하는 Virtual_Path에 파일을 업로드하면, THE DAVProvider SHALL 기존 파일을 새 데이터로 덮어쓰고 Metadata_Store의 수정 시간과 파일 크기를 갱신한다
7. THE WebDAV_Server SHALL HTTP Basic Authentication을 통해 접근을 제어한다
8. IF WebDAV 클라이언트가 인증 없이 접근하면, THEN THE WebDAV_Server SHALL HTTP 401 Unauthorized 응답을 반환한다
9. IF WebDAV 클라이언트가 잘못된 인증 정보로 접근하면, THEN THE WebDAV_Server SHALL HTTP 401 Unauthorized 응답을 반환하고 인증 실패를 로그에 기록한다

### Requirement 2: JBOD 스토리지 통합

**User Story:** As a 사용자, I want 복수의 디렉토리와 루프백 파일을 하나의 논리 볼륨으로 사용하기를, so that 분산된 스토리지를 단일 파일시스템처럼 사용할 수 있다.

#### Acceptance Criteria

1. THE JBOD_Manager SHALL 설정된 모든 Storage_Source의 파일을 단일 네임스페이스로 통합하여, 동일한 Virtual_Path 체계로 접근 가능하도록 제공한다
2. WHEN 디렉토리 목록이 요청되면, THE JBOD_Manager SHALL 모든 활성 Storage_Source에서 해당 경로의 엔트리를 수집하고, 동일한 파일명을 가진 엔트리는 하나만 포함하여 통합된 목록을 반환한다
3. WHEN 새 파일이 생성되면, THE JBOD_Manager SHALL 해당 파일 크기 이상의 여유 공간을 가진 활성 Storage_Source 중 가용 공간이 가장 많은 소스를 선택하여 파일을 저장한다
4. WHEN 기존 파일에 접근하면, THE JBOD_Manager SHALL Metadata_Store를 조회하여 해당 파일이 위치한 Storage_Source를 결정한다
5. WHEN 디렉토리가 생성되면, THE JBOD_Manager SHALL 모든 활성 Storage_Source에 해당 디렉토리를 생성한다
6. IF 파일 저장 시 모든 활성 Storage_Source의 여유 공간이 해당 파일 크기 미만이면, THEN THE JBOD_Manager SHALL 공간 부족 에러를 반환하고 파일을 생성하지 않는다
7. WHEN 파일이 삭제되면, THE JBOD_Manager SHALL Metadata_Store에서 해당 파일의 위치를 조회하여 해당 Storage_Source에서 파일을 삭제하고 메타데이터를 제거한다
8. WHEN 파일이 이동 또는 이름 변경되면, THE JBOD_Manager SHALL 동일 Storage_Source 내에서 파일을 이동하고 Metadata_Store의 Virtual_Path를 갱신한다
9. IF 디렉토리 생성 시 일부 Storage_Source에서 생성이 실패하면, THEN THE JBOD_Manager SHALL 실패한 소스를 로그에 기록하고 성공한 소스에서는 디렉토리를 유지한다

### Requirement 3: 스토리지 소스 관리

**User Story:** As a 사용자, I want 디렉토리와 루프백 파일을 스토리지 소스로 설정하기를, so that 다양한 형태의 저장 공간을 유연하게 활용할 수 있다.

#### Acceptance Criteria

1. THE JBOD_Manager SHALL Directory_Source 유형의 스토리지 소스를 지원한다 (기존 로컬 디렉토리를 그대로 사용)
2. THE JBOD_Manager SHALL Loopback_Source 유형의 스토리지 소스를 지원한다 (고정 크기 파일을 논리적 스토리지로 사용)
3. WHEN Loopback_Source가 초기화되면, THE 시스템 SHALL 설정된 크기(10MB 이상 2TB 이하)의 파일을 생성하고 내부 파일시스템 구조를 초기화한다
4. WHEN Loopback_Source가 활성화되면, THE 시스템 SHALL 루프백 파일을 논리적으로 마운트하여 읽기/쓰기가 가능한 상태로 만든다
5. WHEN Storage_Source에 파일 생성, 삭제, 또는 쓰기 작업이 완료되면, THE 시스템 SHALL 해당 Storage_Source의 전체 용량과 사용 가능 용량을 갱신한다
6. IF Storage_Source의 경로가 존재하지 않거나 접근 불가능하면, THEN THE 시스템 SHALL 해당 소스를 비활성 상태로 표시하고 에러를 로그에 기록한다
7. IF Loopback_Source 초기화 시 파일 생성 또는 파일시스템 구조 초기화에 실패하면, THEN THE 시스템 SHALL 부분 생성된 파일을 삭제하고 에러를 로그에 기록하며 해당 소스를 비활성 상태로 표시한다
8. IF Loopback_Source 초기화 시 해당 경로에 파일이 이미 존재하면, THEN THE 시스템 SHALL 기존 파일을 덮어쓰지 않고 해당 파일을 기존 Loopback_Source로 활성화한다

### Requirement 4: AES-256 암호화

**User Story:** As a 사용자, I want 저장되는 모든 파일이 AES-256으로 암호화되기를, so that WebDAV 서비스를 통하지 않으면 파일 내용을 읽을 수 없다.

#### Acceptance Criteria

1. THE Encryption_Engine SHALL AES-256-GCM 모드를 기본 암호화 모드로 사용하여 파일 데이터를 암호화한다
2. WHEN 파일이 Storage_Source에 저장되면, THE Encryption_Engine SHALL 파일 전체를 암호화하여 저장한다
3. WHEN 암호화된 파일이 읽히면, THE Encryption_Engine SHALL 인증 태그를 검증한 후 파일을 복호화하여 원본 데이터를 반환한다
4. THE Encryption_Engine SHALL 각 파일마다 암호학적으로 안전한 난수 생성기(CSPRNG)를 사용하여 128비트 초기화 벡터(IV)를 생성한다
5. THE Encryption_Engine SHALL 암호화된 파일의 헤더에 IV, 사용된 암호화 모드 식별자, 인증 태그를 저장한다
6. WHEN 동일한 데이터를 암호화한 후 복호화하면, THE Encryption_Engine SHALL 원본과 동일한 데이터를 반환한다 (round-trip 보장)
7. IF 잘못된 Encryption_Key로 복호화를 시도하면, THEN THE Encryption_Engine SHALL 복호화 실패 에러를 반환하고 손상된 데이터를 반환하지 않는다
8. IF 암호화된 파일의 인증 태그 검증이 실패하면 (파일 변조 또는 손상), THEN THE Encryption_Engine SHALL 무결성 검증 실패 에러를 반환하고 손상된 데이터를 반환하지 않는다

### Requirement 5: 메타데이터 저장소

**User Story:** As a 시스템, I want 파일 위치와 속성 정보를 SQLite에 저장하기를, so that 파일 조회 시 모든 Storage_Source를 순회하지 않고 빠르게 위치를 결정할 수 있다.

#### Acceptance Criteria

1. THE Metadata_Store SHALL SQLite 데이터베이스를 사용하여 파일 메타데이터를 저장한다
2. THE Metadata_Store SHALL 각 파일에 대해 Virtual_Path(최대 4096자), 소속 Storage_Source ID, Physical_Path(최대 4096자), 파일 크기(0 이상의 정수, 바이트 단위), 생성 시간(UTC 타임스탬프), 수정 시간(UTC 타임스탬프)을 저장하며, Virtual_Path는 유일성 제약을 갖는다
3. WHEN 파일이 생성되면, THE Metadata_Store SHALL 파일 저장 작업과 동기적으로 해당 파일의 메타데이터를 데이터베이스에 기록한다
4. WHEN 파일이 삭제되면, THE Metadata_Store SHALL 해당 파일의 메타데이터를 데이터베이스에서 제거한다
5. WHEN 파일이 이동 또는 이름 변경되면, THE Metadata_Store SHALL 해당 파일의 Virtual_Path를 갱신한다
6. WHEN 디렉토리가 이동 또는 이름 변경되면, THE Metadata_Store SHALL 해당 디렉토리 하위의 모든 파일에 대해 Virtual_Path 접두사를 새 경로로 갱신한다
7. WHEN Virtual_Path로 파일 위치를 조회하면, THE Metadata_Store SHALL O(log n) 이하의 시간 복잡도로 Storage_Source ID와 Physical_Path를 반환한다
8. IF 존재하지 않는 Virtual_Path로 조회하면, THEN THE Metadata_Store SHALL 해당 파일이 존재하지 않음을 나타내는 결과(None 또는 빈 결과)를 반환한다
9. IF 데이터베이스 파일이 존재하지 않으면, THEN THE Metadata_Store SHALL 필요한 테이블 스키마를 생성하여 초기화한다

### Requirement 6: 설정 관리

**User Story:** As a 시스템 관리자, I want 스토리지 소스와 서비스 설정을 관리하기를, so that 시스템 구성을 유연하게 변경할 수 있다.

#### Acceptance Criteria

1. WHEN StardustFS가 시작되면, THE 시스템 SHALL JSON 형식의 설정 파일에서 WebDAV 서비스 설정(호스트, 포트, 인증 정보)을 로드한다
2. WHEN StardustFS가 시작되면, THE 시스템 SHALL 설정 파일에서 Storage_Source 목록(유형, 경로, 크기)을 로드한다
3. WHEN 설정 파일을 로드한 후, THE 시스템 SHALL 설정에 최소 하나의 Storage_Source가 정의되어 있는지 검증한다
4. WHEN 설정 파일을 로드한 후, THE 시스템 SHALL 각 Directory_Source의 경로가 존재하는 절대 경로인지 검증한다
5. WHEN 설정 파일을 로드한 후, THE 시스템 SHALL 각 Loopback_Source의 파일 경로가 절대 경로이고 크기가 1MB 이상 16TB 이하의 정수 값인지 검증한다
6. IF 설정 파일이 없거나 JSON 파싱에 실패하면, THEN THE 시스템 SHALL 실패 원인(파일 미존재, 파싱 에러 위치)을 포함한 에러 메시지를 표준 에러에 출력하고 시작을 중단한다
7. IF 설정 파일의 논리적 검증이 실패하면, THEN THE 시스템 SHALL 실패한 필드명과 사유를 포함한 에러 메시지를 표준 에러에 출력하고 시작을 중단한다
8. THE 시스템 SHALL Encryption_Key를 설정 파일과 분리된 별도의 키 파일 또는 환경 변수에서 로드한다
9. IF Encryption_Key가 키 파일과 환경 변수 모두에서 발견되지 않으면, THEN THE 시스템 SHALL 키 미설정 에러 메시지를 표준 에러에 출력하고 시작을 중단한다

### Requirement 7: 시스템 초기화

**User Story:** As a 시스템 관리자, I want 시스템 시작 시 모든 구성 요소가 검증되기를, so that 잘못된 구성으로 인한 런타임 오류를 방지할 수 있다.

#### Acceptance Criteria

1. WHEN StardustFS가 시작되면, THE 시스템 SHALL 각 Directory_Source 경로가 존재하고 읽기/쓰기 권한이 있는지 검증한다
2. WHEN StardustFS가 시작되면, THE 시스템 SHALL 각 Loopback_Source 파일이 존재하거나, 파일 경로의 상위 디렉토리에 쓰기 권한이 있고 설정된 크기만큼의 여유 공간이 있어 생성 가능한지 검증한다
3. WHEN StardustFS가 시작되면, THE 시스템 SHALL 10초 이내에 Metadata_Store 데이터베이스 연결을 확인하고 스키마를 초기화한다
4. WHEN StardustFS가 시작되면, THE 시스템 SHALL Encryption_Key가 정확히 32바이트(256비트) 길이이고 로드 가능한 형식인지 검증한다
5. WHEN 모든 검증이 성공하면, THE 시스템 SHALL 설정 로드 → 스토리지 검증 → 데이터베이스 초기화 → 키 검증 순서를 완료한 후 WebDAV 서비스를 시작하고 준비 완료 메시지를 로그에 기록한다
6. IF 어떤 검증이 실패하면, THEN THE 시스템 SHALL 모든 검증 항목을 수행하여 발견된 모든 실패 원인을 로그에 기록하고, 0이 아닌 종료 코드로 시작을 중단한다
7. IF Metadata_Store 데이터베이스 연결이 10초 이내에 응답하지 않으면, THEN THE 시스템 SHALL 연결 타임아웃 에러를 로그에 기록하고 해당 검증을 실패로 처리한다

### Requirement 8: 에러 처리

**User Story:** As a 사용자, I want 시스템 오류가 적절히 처리되기를, so that 데이터 손실 없이 예측 가능하게 동작한다.

#### Acceptance Criteria

1. IF Storage_Source가 런타임에 접근 불가능해지면, THEN THE JBOD_Manager SHALL 해당 소스를 비활성으로 표시하고 에러를 로그에 기록하며 나머지 활성 소스로 서비스를 계속한다
2. IF 파일 쓰기 중 Storage_Source의 공간이 부족해지면, THEN THE 시스템 SHALL 쓰기 작업을 실패로 처리하고 부분 기록된 파일을 Storage_Source에서 삭제하며 관련 메타데이터가 생성되었다면 이를 롤백한다
3. IF 암호화 또는 복호화 중 에러가 발생하면, THEN THE Encryption_Engine SHALL 에러를 로그에 기록하고 손상된 데이터를 반환하지 않으며 호출자에게 에러를 전파한다
4. IF Metadata_Store 데이터베이스 작업이 실패하면, THEN THE 시스템 SHALL 트랜잭션을 롤백하고 에러를 로그에 기록하며 해당 작업과 연관된 물리 파일 변경이 있었다면 이를 원래 상태로 복원한다
5. IF 비활성 상태의 Storage_Source에만 존재하는 파일에 접근하면, THEN THE JBOD_Manager SHALL 해당 파일을 사용 불가로 판단하고 에러를 반환한다
6. WHEN 에러가 발생하면, THE 시스템 SHALL 에러 유형에 따라 다음의 HTTP 상태 코드를 클라이언트에 반환한다: 공간 부족 시 507 Insufficient Storage, 인증 실패 시 403 Forbidden, 그 외 내부 에러 시 500 Internal Server Error
