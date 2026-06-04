# Implementation Plan: 원격 파일 수정 시 로컬 소유권 이전 (3a)

## Overview

원격 소유 파일 수정 시 OSError 대신 로컬 소유권 이전(takeover)을 수행하고,
소유권을 잃은 디바이스가 동기화 후 orphan 물리 파일을 정리(GC)한다.

## Tasks

- [x] 1. MetadataStore.update에 source_id/physical_path 갱신 지원
  - [x] 1.1 update가 source_id, physical_path를 선택 인자로 받아 갱신
    - 기존 호출(파일 크기/시각/device_id만)은 그대로 동작(하위 호환)
    - version 증가, sync_status='pending' 유지
    - _Requirements: 1.2, 2.1_

- [x] 2. 소유권 이전 write
  - [x] 2.1 JBODManager._takeover_write 구현
    - 로컬 소스 선택 → 새 physical_path 기록 → metadata update(device_id=로컬,
      source_id/physical_path 갱신) → commit. 실패 시 파일 정리 + 롤백
    - 공간 부족 시 InsufficientStorageError
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 2.2 write_file의 원격 차단 OSError를 _takeover_write 호출로 교체
    - 로컬/레거시(NULL) 수정은 기존 덮어쓰기 유지
    - _Requirements: 1.1, 4.1, 4.2_
  - [x]* 2.3 소유권 이전 단위 테스트
    - 원격 소유 수정→로컬 소유 전환, 경로 유지, 내용 반영, version↑/pending
    - 로컬/레거시 수정은 기존 동작 유지(회귀)
    - _Requirements: 1.1-1.5, 4.1, 4.2_

- [x] 3. orphan GC
  - [x] 3.1 MetadataStore.live_physical_paths_for_device
    - deleted=0 AND (device_id==자신 OR device_id IS NULL)의 (source_id,
      physical_path) 집합 반환
    - _Requirements: 3.1, 3.3, 4.2_
  - [x] 3.2 StorageSource.list_physical_files
    - DirectorySource: 소스 루트 직속 파일명. LoopbackSource: 동반 디렉토리 직속
      파일명. 디렉토리 제외. RemoteSource는 미지원/빈 목록
    - _Requirements: 3.1, 3.4_
  - [x] 3.3 JBODManager.gc_orphan_files
    - 활성 로컬 소스만 스캔, 보존 집합에 없는 물리 파일 삭제, 삭제 수 반환
    - device_id None이면 GC 건너뜀(안전장치), 원격 소스 제외
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.3_
  - [x]* 3.4 orphan GC 단위 테스트
    - 보존(자신 소유/레거시 참조)·삭제(미참조) 분기, device_id None 시 무동작
    - _Requirements: 3.1-3.4, 4.3_

- [x] 4. 동기화 통합 (디바운스 + 병합 감지)
  - [x] 4.1 JBODManager 디바운스 플래그
    - _gc_needed 플래그, _takeover_write가 set, gc_orphan_files_if_needed()가
      사이클당 1회만 스캔(다중 파일 동시 수정 시에도 1회). set/clear
    - mark_gc_needed() 공개 메서드(병합 측에서 호출)
    - _Requirements: 3.5_
  - [x] 4.2 SyncClient 통합
    - 시작 시(초기 동기화 후) gc_orphan_files() 1회
    - 매 사이클 종료 후 gc_orphan_files_if_needed() 호출
    - 병합에서 "기존 로컬 소유(device_id==자신) 레코드가 서버 레코드에서 타
      디바이스 소유로 바뀜"을 감지하면 jbod.mark_gc_needed() (PC-B가 소유권을
      넘겨받은 변경을 수신한 경우)
    - jbod_manager 없으면 무동작, 예외는 로깅 후 무시
    - _Requirements: 2.2, 3.5_

- [ ] 5. 통합 검증
  - [ ] 5.1 소유권 이전 E2E (mock 또는 로컬 서버)
    - PC-A가 PC-B 소유 파일 수정 → metadata device_id=PC-A, 내용 반영
    - PC-B 동기화 후 orphan 물리 파일 삭제 확인
    - _Requirements: 1.1-1.3, 2.1, 2.2, 3.1, 3.2_
  - [ ]* 5.2 Property 테스트 (이전 후 일관성/orphan 보존/소유권 단일성)
    - _Requirements: 1.1-1.3, 3.1-3.3_

- [ ] 6. Final Checkpoint
  - 전체 테스트 통과 + 회귀 없음.

## Notes

- orphan GC는 데이터 손실 위험이 있으므로 "활성 metadata 참조 파일 보존" 불변식을
  엄격히 지킨다. device_id None이면 GC 전체 건너뜀.
- 원격 소스(is_remote)는 GC 스캔 대상이 아니다.
- `*` 표시는 선택적 태스크.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.1", "3.2"] },
    { "id": 1, "tasks": ["2.1", "3.3", "4.1"] },
    { "id": 2, "tasks": ["2.2", "3.4", "4.2"] },
    { "id": 3, "tasks": ["2.3", "5.1"] },
    { "id": 4, "tasks": ["5.2", "6"] }
  ]
}
```
