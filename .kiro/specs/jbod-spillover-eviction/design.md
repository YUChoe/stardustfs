---
inclusion: manual
---

# JBOD 스필오버 + 콜드 축출 — 설계

## 개요
로컬 만석 시 신규 쓰기를 온라인 리모트(같은 계정)로 흘려보내고(스필오버),
로컬 공간 부족 시 복제본이 충분한 콜드 파일의 로컬 원본을 비운다(축출). 둘 다
기존 _evacuate_to_remote / replication recover 인프라를 재사용한다.

## Components and Interfaces

### Phase A: 리모트 스필오버 (쓰기)
- `jbod_manager._write_to_remote(virtual_path, encrypted, file_size) -> bool`:
  _evacuate_to_remote와 동형(신규 insert판). 활성 _remote_devices 중 한 곳에
  push_blob(new_phys, encrypted) → metadata.insert(device_id=remote,
  source_id=remote_src_id, physical_path=new_phys, file_size, ...). 성공 True.
  도달 가능 리모트 없으면 False.
- `write_file` 신규 파일 분기: select_source(local) 시도 → InsufficientStorageError면
  _write_to_remote 시도 → 실패하면 InsufficientStorageError 재발생.
- 읽기: device_id=remote가 되므로 기존 read_file 원격 라우팅이 그대로 처리(변경 없음).

### Phase B: 콜드 축출 (로컬 회수)
- 메타 표현: local-evicted를 나타내기 위해 files에 별도 플래그 대신 기존 필드 활용 —
  source_id를 센티넬(예: "__evicted__")로 두고 physical_path는 비우며 device_id는
  소유 device 유지. 또는 replication 기반 복구를 트리거하는 read 경로에서 판별.
  (구현 시 metadata_store에 `mark_evicted(virtual_path)` + lookup이 evicted 플래그
  노출하도록 최소 확장.)
- `EvictionManager`(또는 jbod 메서드): 로컬 여유 < low_watermark이면 replicated 로컬
  파일을 modified_at 오름차순으로 순회하며, ReplicationManager로 온라인 복제본 수를
  실측(≥min_replicas)한 파일만 로컬 원본 삭제 + mark_evicted. 여유가 high_watermark
  이상 회복되면 중단. daemon 백그라운드 주기 실행(가벼운 간격, 공간 압박 시만 작동).
- read 경로: jbod.read_file이 evicted(또는 로컬 물리 부재)면 ReplicationManager.recover로
  재구성 → 로컬에 재기록(re-cache) 후 제공. 로컬 만석이면 recover 전에 축출이 공간을
  확보(또는 임시 복구). 재구성 실패는 RecoveryError.

## Data Models
- Phase A: 변경 없음(insert가 device_id=remote로 기록).
- Phase B: files에 evicted 표현(센티넬 source_id 또는 신규 컬럼). 마이그레이션은
  IF NOT EXISTS/NULL 호환.

## Correctness Properties

### Property 1: 무손실 스필오버
*임의의* 신규 쓰기에 대해, 리모트 push_blob과 metadata.insert가 성공한 경우에만
파일이 등록되며, 로컬·리모트 어디에도 못 쓰면 InsufficientStorageError로 실패한다
(부분 상태 없음).

### Property 2: 무손실 축출
*임의의* 콜드 축출에 대해, 로컬 원본은 현재 온라인 복제본 수 ≥ min_replicas를 실측
확인한 뒤에만 삭제된다. 확인 실패 파일은 보존된다.

### Property 3: 읽기 일관성
*임의의* 파일에 대해, 로컬/리모트/evicted 어느 상태든 read_file은 동일한 평문을
반환한다(로컬 직접 / 원격 fetch / 복제 recover 경로).

## Error Handling
- 스필오버 대상 없음: InsufficientStorageError.
- 축출 후 읽기에서 복구 불가: RecoveryError(누락 chunk_id 명시).
- 리모트 push 실패: 다음 리모트 시도, 모두 실패면 미기록(쓰기 에러).

## Testing Strategy
- Phase A: 로컬 만석 + 온라인 _FakeRemote → 리모트 기록, 메타 device_id=remote;
  리모트 없음/오프라인 → InsufficientStorageError. 로컬 여유 있으면 로컬 기록(기존 유지).
- Phase B: 축출이 온라인 복제본 미달 파일을 건너뜀; 충분하면 로컬 삭제+evicted 표시;
  evicted 읽기가 recover로 재구성; high_watermark 회복 시 중단.
