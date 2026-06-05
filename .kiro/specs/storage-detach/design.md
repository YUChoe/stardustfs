---
inclusion: manual
---

# 스토리지 attach/detach (evacuate) — Design

## 개요

detach는 "evacuate 후 분리"다. 대상 소스의 활성 파일을 (1) 남은 로컬 소스, (2) 온라인
리모트 디바이스로 분산 이동하고, 모든 파일이 옮겨진 뒤에만 소스를 설정에서 제거한다.
파일 암호문(at-rest)은 재암호화 없이 그대로 복사하고 메타데이터만 갱신한다.

## Components and Interfaces

### metadata_store
- `list_files_in_source(source_id)`: 그 소스의 활성(deleted=0) 파일 목록
  (virtual_path/physical_path/file_size/device_id). evacuate 대상 산출.
- 기존 `update(virtual_path, source_id=, physical_path=, device_id=)`로 위치 갱신.

### jbod_manager
- `select_source(file_size, exclude_ids=())`: 용량 충분한 로컬 활성 소스 중 가용량 큰
  것을 고른다(원격·비활성·exclude 제외). 기존 select_source에 exclude 추가.
- `evacuate_source(source_id, remote_targets) -> EvacuateReport`:
  - 대상 소스의 각 파일에 대해:
    1. 남은 로컬 소스 중 용량 가능한 곳 선택 → 원본의 물리 블록을 raw로 읽어 대상에
       write(재암호화 없음) → 메타 source_id/physical_path 갱신 → 성공 시 원본 블록 삭제.
    2. 로컬 불가 시 온라인 리모트로 push(P2P write) → 메타 device_id/source_id/
       physical_path 갱신 → 성공 시 원본 블록 삭제.
    3. 둘 다 불가면 해당 파일을 미이동(unmoved)으로 기록.
  - 모든 파일 이동 성공 시 ok=True. 하나라도 unmoved면 ok=False(detach 중단).
- raw I/O: StorageSource.read/write(physical_path[, source_id])는 암호문 그대로 다룸.

### 리모트 evacuate (Phase 3)
- RemoteSource.write로 대상 디바이스의 소스에 암호문 블록을 기록하고, 메타데이터
  device_id=대상, source_id=대상 소스, physical_path=신규로 갱신. 대상 디바이스는
  메타 동기화로 소유를 인식. 대상이 오프라인이면 그 파일은 이동 불가.

### 설정/액션 (gui/actions, stardustfs cli)
- attach: loopback만. `add_source(type="loopback", path, size)`. directory 거부.
- detach: `detach_source(config, source_id)` →
  온라인 세션에서 jbod.evacuate_source 호출 → ok면 config에서 소스 제거 + 저장.
  unmoved 있으면 config 유지(분리 안 함) + 보고.

## Data Models
- 신규 테이블 없음. files.source_id/physical_path/device_id 갱신으로 이동 표현.

## Correctness Properties

### Property 1: 무손실 이동
*임의의* 파일 이동에 대해, 대상 기록이 성공으로 확인된 뒤에만 원본 물리 블록을
삭제한다. 따라서 어느 시점에도 최소 한 곳에 온전한 사본이 존재한다.

### Property 2: 원자적 분리
*임의의* detach에 대해, 대상 소스의 모든 활성 파일이 다른 위치로 이동 완료된 경우에만
소스가 설정에서 제거된다. 미이동 파일이 있으면 소스는 유지된다.

### Property 3: 암호문 보존(zero-knowledge)
*임의의* 이동에 대해, 파일은 재암호화 없이 at-rest 암호문 그대로 옮겨지며 평문/키가
노출되지 않는다(리모트 이동도 암호문 블록 전송).

### Property 4: 디렉터리 타입 폐지
*임의의* 신규 소스 추가에 대해, 타입은 loopback이어야 하며 directory는 거부된다.

## Error Handling
- 로컬 용량 부족 + 리모트 도달 불가: 해당 파일 unmoved, detach 중단, 사유 보고.
- 리모트 push 실패(타임아웃/오프라인): unmoved 처리(원본 보존). 부분 이동은 허용.
- 대상 소스가 마지막 1개(이동 대상 없음)이고 파일 존재: detach 거부.
- 빈 소스(활성 파일 0): 즉시 분리.

## Testing Strategy
- 단위: list_files_in_source, select_source(exclude), evacuate_source(로컬 이동 성공/
  용량부족 중단/원본 삭제는 기록 성공 후), 빈 소스 즉시 분리.
- detach 액션: ok 시 config에서 제거, unmoved 시 유지.
- 리모트 evacuate(Phase 3): 온라인 디바이스로 push + 메타 device_id 갱신, 오프라인 시
  unmoved.

## 단계
1. 디렉터리 타입 폐지(attach loopback 전용 + GUI).
2. 로컬 evacuate + 원자적 detach.
3. 리모트(타 디바이스) evacuate.
4. GUI 연동(detach 진행/결과) + 문서.
