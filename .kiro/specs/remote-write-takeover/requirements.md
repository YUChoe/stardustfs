# Requirements: 원격 파일 수정 시 로컬 소유권 이전 (Copy-on-Write 3a)

## Introduction

크로스 디바이스 라우팅에서 원격 디바이스(예: PC-B) 소유 파일은 현재 읽기 전용이며,
다른 디바이스(PC-A)에서 수정하면 OSError가 발생한다. 사용자는 자신의 어느
디바이스에서든 파일을 편집할 수 있기를 기대한다.

방식 3a(소유권 이전)를 채택한다: PC-A가 원격 소유 파일을 수정하면, PC-A 로컬
소스에 새 내용을 기록하고 메타데이터의 소유권(device_id/source_id/physical_path)을
PC-A로 이전한다. 가상 경로는 동일하게 유지되어 사용자에게는 같은 파일 1개로 보인다.

원래 소유 디바이스(PC-B)의 물리 파일은 메타데이터가 더 이상 가리키지 않는
고아(orphan)가 되며, PC-B가 다음에 동기화할 때 자체적으로 정리(GC)한다.

## 용어

- 소유권 이전(takeover): 원격 소유 파일 수정 시 device_id를 로컬로 변경
- 고아 물리 파일(orphan): metadata가 더 이상 가리키지 않는, 디바이스 로컬 디스크에
  남은 물리 파일
- orphan GC: 각 디바이스가 자신의 물리 파일 중 현재 metadata가 자신을 소유자로
  지정하지 않는 것을 삭제하는 정리 작업

## 전제

- StardustFS는 클라이언트 구동 중에만 파일 접근이 가능하다. 따라서 PC-A가 원격
  파일을 수정하는 시점에 PC-B는 반드시 온라인이며(라우팅/릴레이로 접근 가능),
  PC-B가 같은 순간 같은 파일을 동시 수정하는 상황은 발생하지 않는다.
- 같은 유저의 디바이스 간에만 적용한다. 교차 사용자에는 적용하지 않는다.

## Requirements

### Requirement 1: 원격 소유 파일 수정 시 로컬 소유권 이전

User Story: 사용자로서, 내 다른 디바이스가 소유한 파일을 현재 디바이스에서
수정하면 그 변경이 반영되고 파일이 현재 디바이스 소유가 되기를 원한다.

#### Acceptance Criteria

1. WHEN 원격 소유(device_id != 로컬) 파일에 write_file이 호출되면 THEN OSError를
   발생시키지 않고, 로컬 소스에 새 내용을 기록한다.
2. WHEN 소유권 이전이 일어나면 THEN metadata의 device_id를 로컬로, source_id와
   physical_path를 로컬 기록 위치로, file_size/modified_at/version을 갱신한다.
3. WHEN 소유권 이전이 일어나면 THEN 가상 경로(virtual_path)는 변경되지 않는다.
4. WHEN 소유권 이전 기록이 실패하면 THEN 부분 기록 파일을 정리하고 metadata를
   롤백한다(원자성).
5. WHEN 로컬 소스에 공간이 부족하면 THEN InsufficientStorageError를 발생시킨다
   (조용한 건너뛰기 금지).

### Requirement 2: 변경의 동기화 전파

User Story: 사용자로서, 소유권 이전 후 다른 디바이스에서도 최신 내용과 새 소유자가
반영되기를 원한다.

#### Acceptance Criteria

1. WHEN 소유권 이전이 일어나면 THEN 해당 레코드의 sync_status가 pending이 되어
   다음 동기화에서 서버로 업로드된다.
2. WHEN 다른 디바이스가 동기화로 이 레코드를 수신하면 THEN device_id가 새 소유자
   (PC-A)로 갱신되고, 그 디바이스에서 읽으면 PC-A로 라우팅된다.
3. WHEN version 비교로 병합되면 THEN 더 높은 version(이전 후)이 우선한다.

### Requirement 3: 고아 물리 파일 정리 (orphan GC)

User Story: 디바이스로서, 소유권이 다른 디바이스로 넘어가 더 이상 내 metadata가
가리키지 않는 물리 파일을 정리해 디스크를 회수하고 싶다.

#### Acceptance Criteria

1. WHEN 디바이스가 동기화를 완료하면 THEN 자신의 로컬 소스에 있는 물리 파일 중
   현재 metadata에서 자신이 소유자(device_id == 자신)로 그 physical_path를
   가리키지 않는 파일을 식별한다.
2. WHEN 고아 물리 파일이 식별되면 THEN 해당 물리 파일을 삭제한다.
3. WHEN 어떤 물리 파일이 활성 metadata(deleted=0)에서 현재 디바이스 소유로
   참조되면 THEN 절대 삭제하지 않는다.
4. WHEN orphan GC가 동작하면 THEN 다른 디바이스 소유 레코드의 physical_path는
   고려 대상이 아니다(각 디바이스는 자기 소유만 검사).
5. orphan GC는 즉시 일어나지 않아도 되며(대상 디바이스가 오프라인일 수 있음),
   다음 구동/동기화 시 정리되면 된다.

### Requirement 4: 안전성 및 회귀 방지

#### Acceptance Criteria

1. WHEN 로컬 소유 파일을 수정하면 THEN 기존 동작(같은 위치 덮어쓰기)이 유지된다.
2. WHEN device_id가 NULL인 레거시 레코드를 수정하면 THEN 로컬 소유로 간주하여
   기존 동작(덮어쓰기)을 유지한다.
3. orphan GC는 오작동 시 데이터 손실 위험이 크므로, 활성 metadata가 참조하는
   파일을 보존하는 조건을 엄격히 검증한다.
