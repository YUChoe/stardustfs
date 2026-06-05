---
inclusion: manual
---

# 스토리지 attach/detach (evacuate 후 분리) — Requirements

스토리지 소스를 추가(attach)/분리(detach)한다. 디렉터리 타입은 폐지하고 loopback만
사용한다. detach 시 그 소스의 파일을 남은 로컬 소스와 리모트(현재 개발: 다른 디바이스,
기획: 다른 사용자)로 분산 이동(evacuate)하고, 모든 파일 이동이 끝난 뒤에만 소스를
분리한다(원자적).

## Requirement 1: 디렉터리 타입 폐지

### Acceptance Criteria
1. THE 소스 추가 기능 SHALL loopback 타입만 허용한다(`type="directory"` 거부).
2. THE GUI 스토리지 관리 SHALL 디렉터리 추가 수단을 제공하지 않는다.
3. IF 기존 설정에 directory 소스가 있으면 THE 시스템 SHALL 로드는 허용하되(하위호환)
   신규 추가는 막는다.

## Requirement 2: 소스 attach

### Acceptance Criteria
1. WHEN 사용자가 loopback 소스를 추가하면 THE 시스템 SHALL 경로·크기로 .img를 만들고
   설정의 sources에 추가한다(daemon 재시작 후 마운트).

## Requirement 3: detach 시 evacuate(분산 이동)

### Acceptance Criteria
1. WHEN 사용자가 소스 detach를 요청하면 THE 시스템 SHALL 그 소스의 활성 파일
   (deleted=0, 로컬 소유)을 모두 다른 위치로 이동한 뒤에만 소스를 분리한다.
2. THE evacuate SHALL 우선 남은 로컬 소스(용량 충분한)로 이동하고, 로컬 용량이 부족한
   파일은 온라인 리모트 디바이스(같은 사용자, 향후 타 사용자)로 이동한다.
3. THE evacuate SHALL 파일의 암호문(at-rest blob)을 재암호화 없이 그대로 옮기고
   메타데이터(source_id/physical_path, 리모트면 device_id)를 갱신한다.
4. IF 한 파일이라도 이동할 대상(로컬 용량/도달 가능한 리모트)이 없으면 THE 시스템
   SHALL detach를 중단하고(이미 옮긴 것은 유지) 남은 파일과 사유를 보고한다.
5. WHEN 모든 활성 파일 이동이 완료되면 THE 시스템 SHALL 소스를 설정에서 제거한다.
6. THE detach SHALL .img 파일 자체를 삭제하지 않는다(분리만; 디스크 정리는 별도).

## Requirement 4: 원자성·안전

### Acceptance Criteria
1. THE evacuate SHALL 각 파일에 대해 대상에 기록 성공을 확인한 뒤에야 원본 소스의
   물리 블록을 삭제한다(이동 중 손실 방지).
2. IF 리모트 이동 중 대상이 오프라인/실패면 THE 시스템 SHALL 그 파일을 미이동으로
   남기고 detach를 중단한다(부분 이동 허용, 데이터 손실 없음).
3. THE 메타데이터 변경 SHALL 동기화로 다른 디바이스에 전파된다.

## 비범위 (후속)
- 타 사용자(리모트)로의 evacuate(현재는 같은 사용자 디바이스 간). 권한·인가 확장 필요.
- erasure coding, 자동 재배치 정책.
