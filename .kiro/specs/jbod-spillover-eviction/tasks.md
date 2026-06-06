---
inclusion: manual
---

# JBOD 스필오버 + 콜드 축출 — Tasks

## Phase A: 리모트 스필오버(쓰기)
- [x] A.1 jbod_manager._write_to_remote(virtual_path, encrypted, file_size): 활성 리모트에
      push_blob → metadata.insert(device_id=remote). 도달 가능 없으면 False.
- [x] A.2 write_file 신규 분기: 로컬 select_source 실패(InsufficientStorageError) 시
      _write_to_remote 폴백, 실패하면 에러 재발생.
- [x] A.3 테스트: 로컬 만석→리모트 기록(메타 device_id=remote), 리모트 없음/오프라인→
      InsufficientStorageError, 로컬 여유 시 로컬 기록 유지.

## Phase B: 콜드 축출(로컬 회수)
- [x] B.1 metadata_store: evicted 컬럼(v5 마이그레이션) + mark_evicted +
      list_eviction_candidates + lookup 노출. update가 evicted 해제.
- [x] B.2 jbod.evict_cold(is_safe, bytes_to_free): replicated·로컬 소유 파일을 오래된
      순으로, is_safe(온라인 복제본 실측 ≥min_replicas)인 것만 로컬 삭제+mark_evicted.
      회수량이 목표 이상이면 중단.
- [x] B.3 read_file: evicted면 _recover_fn(=ReplicationManager.recover)으로 재구성→
      write_file 재기록(evicted 해제)→제공. 콜백 없으면(오프라인) OSError.
- [x] B.4 daemon: _recover_fn 주입 + _eviction_loop(여유<low면 high까지 축출).
      설정 eviction.{enabled(기본 False), interval/low/high_watermark}.
- [x] B.5 테스트: 복제 미달 건너뜀, 충분 시 삭제+evicted, evicted 읽기 recover,
      복구 콜백 없으면 에러, 후보 쿼리(replicated·미축출·오래된 순).

## Phase C: 검증/문서
- [x] C.1 클라이언트 회귀 테스트 그린(523/1 skip).
- [x] C.2 ARCHITECTURE에 스필오버·티어링 반영.

## 남은 후속(데이터 안전 관련)
- 교차 디바이스 P2P 읽기(_op_read)의 축출 파일 폴백: 현재 _op_read는 physical_path
  직접 읽기라 evicted를 모름 → 타 디바이스가 축출 파일을 P2P로 받으면 404. 안전을 위해
  daemon 자동 축출은 기본 비활성(eviction.enabled=False). 활성 전, get/다운로드 경로의
  복제 recover 폴백(또는 _op_read 역참조 재구체화) 필요.
- evicted를 동기화에서 제외(디바이스-로컬 상태) 확인 — mark_evicted는 version/sync_status
  미변경이라 업로드되지 않음(현재 안전), 명시 테스트는 후속.

## 비범위
- 타 사용자 스토리지로의 원본 스필오버(소유권·인가 재설계).
- 등급별 min_replicas≥2 집행(MVP4).
