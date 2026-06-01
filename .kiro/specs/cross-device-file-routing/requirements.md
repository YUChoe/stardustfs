# Requirements Document

크로스 디바이스 파일 자동 라우팅 (MVP2 확장)

## Introduction

같은 계정의 여러 디바이스가 metadata를 동기화하면, PC-B는 PC-A가 만든 파일의 레코드를 보게 된다. 그러나 현재는 그 파일을 PC-B에서 열면 로컬에서 `source_id`로 소스를 찾으려다 실패한다(그 파일의 실데이터는 PC-A에 있기 때문). 본 기능은 파일 레코드의 `device_id`를 기준으로 "로컬 파일이면 로컬에서, 다른 디바이스 파일이면 그 디바이스의 P2P 서버에서" 자동으로 읽어오는 라우팅을 구현한다.

목표는 사용자가 PC-B의 WebDAV에서 PC-A가 만든 파일을 그냥 열면 투명하게 P2P로 데이터가 전송되는 것이다. 단 디바이스가 오프라인이면 기존 오프라인 placeholder(503) 동작을 따른다.

## Glossary

- **소유 디바이스(Owner_Device):** 파일의 실데이터를 보유한 디바이스. 파일 레코드의 device_id로 식별.
- **로컬 디바이스(Local_Device):** 현재 실행 중인 클라이언트 자신.
- **원격 디바이스(Remote_Device):** 로컬이 아닌 같은 계정의 다른 디바이스.
- **디바이스 라우터(Device_Router):** 파일의 device_id를 보고 로컬/원격 읽기를 결정하는 JBODManager의 라우팅 로직.
- **레거시 레코드(Legacy_Record):** device_id가 NULL인 파일 레코드(이 기능 이전에 생성됨).

## Requirements

### Requirement 1: device_id 기반 읽기 라우팅

**User Story:** 사용자로서, PC-B에서 PC-A가 만든 파일을 그냥 열면 자동으로 PC-A에서 데이터를 가져오기를 원한다. 이를 통해 어느 디바이스에서 만든 파일이든 투명하게 접근할 수 있다.

#### Acceptance Criteria

1. WHEN read_file(virtual_path)가 호출되면, THE Device_Router SHALL 파일 레코드의 device_id를 확인한다
2. IF 파일의 device_id가 로컬 디바이스의 device_id와 같거나 NULL(레거시)이면, THEN THE Device_Router SHALL 기존 방식대로 로컬 source_id로 읽는다
3. IF 파일의 device_id가 원격 디바이스의 것이면, THEN THE Device_Router SHALL 해당 디바이스의 P2P 서버에 (source_id, physical_path)를 보내 데이터를 읽는다
4. WHEN 원격 디바이스로 읽기를 라우팅할 때, THE Device_Router SHALL 그 디바이스가 마운트되어 있고 활성(온라인) 상태인 경우에만 읽기를 수행한다
5. IF 원격 디바이스가 비활성(오프라인)이면, THEN THE Device_Router SHALL OSError를 발생시킨다 (WebDAV는 이를 503으로 변환)

### Requirement 2: P2P 서버의 다중 소스 노출

**User Story:** 원격 디바이스로서, 요청자가 지정한 소스의 파일을 제공하고 싶다. 이를 통해 한 디바이스가 여러 스토리지 소스를 가져도 정확한 소스에서 파일을 읽어줄 수 있다.

#### Acceptance Criteria

1. WHEN P2P 서버가 /p2p/read 요청을 받을 때, THE P2P_Server SHALL 요청 body의 source_id로 자신의 JBOD에서 해당 소스를 찾는다
2. IF 요청에 source_id가 없으면(구버전 호환), THEN THE P2P_Server SHALL 첫 번째 소스를 사용한다
3. IF 지정된 source_id의 소스가 존재하지 않으면, THEN THE P2P_Server SHALL HTTP 404를 반환한다
4. WHEN source_id로 소스를 찾아 읽을 때, THE P2P_Server SHALL 기존 path traversal 방지 검증을 해당 소스 루트 기준으로 적용한다
5. THE 다중 소스 노출은 /p2p/read, /p2p/exists, /p2p/list에 적용되며, 기존 단일 소스 동작과 하위 호환되어야 한다

### Requirement 3: 디바이스 단위 원격 프록시

**User Story:** 로컬 디바이스로서, 원격 디바이스의 여러 소스에 (source_id, physical_path)로 접근하고 싶다. 이를 통해 소스마다 별도 RemoteSource를 만들지 않고 디바이스 하나로 라우팅할 수 있다.

#### Acceptance Criteria

1. THE Device_Router SHALL device_id → 원격 디바이스 접근 객체(RemoteDevice 또는 동등물)의 매핑을 보유한다
2. WHEN 원격 디바이스에 읽기를 요청할 때, THE 원격 프록시 SHALL P2P 요청에 source_id와 physical_path를 함께 포함한다
3. WHEN 자동 디바이스 마운트(기존 기능)가 수행될 때, THE 시스템 SHALL device_id로 원격 프록시를 등록하여 Device_Router가 라우팅에 사용할 수 있게 한다
4. IF 파일의 device_id에 해당하는 원격 프록시가 등록되어 있지 않으면, THEN THE Device_Router SHALL OSError를 발생시킨다

### Requirement 4: 쓰기/삭제의 소유 디바이스 제약

**User Story:** 사용자로서, 다른 디바이스의 파일을 실수로 원격 변경하지 않기를 원한다. 이를 통해 데이터 소유권을 명확히 유지한다.

#### Acceptance Criteria

1. WHEN write_file이 원격 디바이스 소유 파일에 대해 호출되면, THE Device_Router SHALL 본 기능 범위에서 원격 쓰기를 수행하지 않고 OSError를 발생시킨다 (읽기 전용 라우팅)
2. WHEN delete_file이 원격 디바이스 소유 파일에 대해 호출되면, THE Device_Router SHALL 로컬 metadata에서만 처리하고 원격 물리 삭제는 수행하지 않는다 (삭제 동기화는 기존 tombstone 메커니즘이 담당)
3. THE 본 기능은 읽기 라우팅에 집중하며, 원격 쓰기는 향후 확장으로 남긴다

### Requirement 5: 하위 호환 및 안전성

**User Story:** 사용자로서, 기존 단일 디바이스/로컬 파일 동작이 그대로 유지되기를 원한다.

#### Acceptance Criteria

1. WHEN device_id가 NULL인 레거시 레코드를 읽을 때, THE Device_Router SHALL 로컬 읽기로 처리하여 기존 동작을 보존한다
2. WHEN 로컬 디바이스가 소유한 파일을 읽을 때, THE Device_Router SHALL 네트워크를 경유하지 않고 로컬에서 직접 읽는다
3. THE 원격 라우팅 추가는 기존 read_file/P2P read 단위 테스트를 깨지 않아야 한다
4. WHEN 원격 읽기 중 P2P 오류(타임아웃, 4xx/5xx)가 발생하면, THE Device_Router SHALL OSError로 변환하여 WebDAV가 적절한 에러로 응답하게 한다
