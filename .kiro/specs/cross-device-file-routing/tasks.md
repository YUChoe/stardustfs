# Implementation Plan: 크로스 디바이스 파일 자동 라우팅

## Overview

파일 레코드의 device_id를 라우팅 키로 사용하여 read_file이 로컬/원격을 자동 선택하도록 한다. P2PServer는 source_id로 다중 소스를 노출하고, RemoteSource는 P2P 요청에 source_id를 포함한다. 읽기 전용 범위. 기존 동작과 하위 호환을 유지한다.

## Tasks

- [x] 1. P2PServer 다중 소스 노출
  - [x] 1.1 source_id 기반 소스 선택 구현
    - _select_source(body): source_id로 소스 조회, 없으면 첫 소스(호환), 미존재 시 404
    - _validate_path(physical_path, source_root)로 source_root 파라미터화
    - handle_read/handle_exists/handle_list가 _select_source 사용
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [x]* 1.2 P2PServer 다중 소스 단위 테스트
    - source_id 지정 read, 미존재 404, source_id 없을 때 첫 소스, traversal 검증
    - _Requirements: 2.1-2.5_

- [x] 2. RemoteSource에 source_id 전달
  - [x] 2.1 read/exists/list_dir가 P2P 요청에 source_id 포함
    - read(physical_path, source_id=None): body에 source_id 추가
    - 기존 호출(source_id 없음)은 그대로 동작 (하위 호환)
    - _Requirements: 3.2_
  - [x]* 2.2 RemoteSource source_id 단위 테스트
    - source_id 포함 요청 전송 확인
    - _Requirements: 3.2_

- [x] 3. JBODManager Device_Router
  - [x] 3.1 read_file에 device_id 기반 라우팅 추가
    - _read_local(metadata)로 기존 로컬 읽기 로직 추출
    - device_id가 NULL/로컬이면 로컬, 원격이면 프록시 라우팅
    - register_remote_device(device_id, remote), _remote_devices dict 추가
    - 원격 프록시 미등록/비활성 시 OSError
    - 원격 read 후 로컬 복호화
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 3.1, 3.3, 3.4, 5.1, 5.2, 5.4_
  - [x] 3.2 원격 파일 write/delete 제약
    - write_file이 원격 소유 파일이면 OSError (읽기 전용)
    - delete_file은 로컬 metadata만 처리 (tombstone), 원격 물리 삭제 안 함
    - _Requirements: 4.1, 4.2, 4.3_
  - [x]* 3.3 Device_Router 단위 테스트
    - 로컬/레거시/원격활성/원격비활성/프록시없음 분기, 원격 write 거부
    - _Requirements: 1.1-1.5, 4.1, 5.1-5.4_

- [x] 4. 자동 마운트 통합
  - [x] 4.1 _mount_remote_sources에서 register_remote_device 호출
    - RemoteSource 마운트 시 jbod_manager에 device_id로도 등록
    - _Requirements: 3.3_

- [x] 5. Property 테스트 및 통합 검증
  - [x]* 5.1 Property 1, 2 PBT
    - **Property 1: 읽기 라우팅 결정성**
    - **Property 2: P2P 소스 선택 정합성**
    - _Requirements: 1.2-1.5, 2.1-2.4_
  - [x] 5.2 크로스 디바이스 read 통합 테스트
    - PC-A가 소스에 파일 저장 → PC-B metadata에 device_id=PC-A 레코드 →
      PC-B read_file이 원격 라우팅으로 동일 바이트 수신
    - 원격 디바이스 오프라인 시 OSError
    - mock 중앙 서버 + 실제 P2PServer
    - _Requirements: 1.1-1.5, 2.1, 3.1-3.4_

- [x] 6. Final Checkpoint
  - 전체 테스트 통과 + 기존 회귀 없음 확인.

## Notes

- 읽기 전용 라우팅: 원격 쓰기는 향후 확장
- 복호화는 항상 로컬(같은 계정 = 같은 master_key)
- device_id NULL 레거시 레코드는 로컬 읽기로 호환
- `*` 표시 태스크는 선택적

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.1"] },
    { "id": 2, "tasks": ["3.2", "4.1"] },
    { "id": 3, "tasks": ["3.3", "5.1", "5.2"] },
    { "id": 4, "tasks": ["6"] }
  ]
}
```
