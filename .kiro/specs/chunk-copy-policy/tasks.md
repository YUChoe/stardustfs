---
inclusion: manual
---

# 청크 3카피 정책 — Tasks

작업 순서는 의존 관계를 따른다. Phase 1(프로비저닝)은 독립이고, Phase 2(스키마)가
Phase 3~5의 전제다.

## Phase 1: 호스팅 상한을 서버 프로비저닝으로 (Requirement 4)

- [x] 1.1 서버 `hosting`에 `quota_bytes` 추가(멱등 ALTER, `_migrate`에 편입).
- [x] 1.2 `/replication/policy` 응답에 `target_copies`(3)와 요청 기기의
      `hosting_quota_bytes` 추가. 스키마·라우터·서비스 갱신.
- [x] 1.3 `placement`의 가용량 계산을 `quota_bytes - hosted_bytes`로 변경
      (`RECIPROCITY_FRACTION` 제거). `_reciprocity_fraction()` 사용처 정리.
- [x] 1.4 클라이언트: `fetch_policy`가 새 필드를 읽고, `_apply_policy`가 받은
      할당량을 `ParityStore.set_max_bytes`에 넣는다. 제공 용량 신고
      (`report_hosting`)는 사용량 보고(`report_usage`)로 교체 — 서버는
      `set_hosted_usage`로 회계를 실측에 맞춘다.
- [x] 1.5 클라이언트: `_current_provided`·`_RECIPROCITY_FRACTION`·
      `_build_parity_store`의 fraction 인자 정리.
- [x] 1.6 테스트: 정책 조회 실패 시 직전 값 유지·최초 실패 시 할당량 0,
      서버 placement가 비율 없이 후보를 고르는지.

## Phase 2: 청크 위치 다중화 (Requirement 5)

- [x] 2.1 `stardustlib/chunk_location.py` 추가: `ChunkLocation`,
      `distinct_devices`.
- [x] 2.2 MetadataStore v8 마이그레이션: `file_chunks`를 새 PK
      `(virtual_path, chunk_index, device_id, source_id)`로 재생성하고 기존 행을
      위치 1개로 이관. `.v7.bak` 백업 후 진행, 실패 시 경로 로그 + 중단.
      `device_id` NULL은 이 기기 id로 채운다.
- [x] 2.3 `get_chunk_locations`/`add_chunk_location`/`remove_chunk_location` 추가.
      기존 `get_chunks`/`put_chunks`는 "이 기기 위치"만 다루는 형태로 유지해
      호출부 회귀를 줄인다.
- [x] 2.4 서버 `replicas`에 `source_id` 추가 + `UNIQUE(chunk_id, holder_device_id,
      source_id)`. 기존 UNIQUE 제약 제거를 위해 테이블 재생성이 필요하다.
- [x] 2.5 `record_replica`/`remove_replica`/`list_replicas`가 `source_id`를 다룬다.
      클라이언트 `_record_replica` 호출부도 함께 갱신.
- [x] 2.6 테스트: 마이그레이션 이관·백업·실패 중단, 위치 CRUD 멱등성,
      서버 기기+소스 단위 멱등 등록.

## Phase 2b: 보관 청크를 스토리지 소스로 (Requirement 6)

- [x] 2b.1 v8에 `hosted_chunks` 테이블 추가(chunk_id PK, owner_user_id, source_id,
      physical_path, size, stored_at) + owner 인덱스.
- [x] 2b.2 `ParityStore` 재구현: 자체 파일 I/O·`index.json` 제거, `StorageSource`로
      쓰고 `hosted_chunks`로 인덱싱. `used_bytes()`는 `SUM(size)` 집계.
      물리 경로는 내 청크와 구분되는 접두사(예: `p/<hh>/<chunk_id>`).
- [x] 2b.3 소스 선택: 여유가 가장 많은 활성 로컬 소스. 공간 부족 시
      `QuotaExceededError`(p2p 507 유지).
- [x] 2b.4 `_build_parity_store` 호출부를 storage_pool·metadata_store 주입으로 변경.
- [x] 2b.5 기존 `.parity/` 디렉토리 이관: 청크를 소스로 옮기고 인덱스를 DB로.
      공간 부족으로 못 옮긴 청크는 로그로 남기고 `.parity/`에 유지(다음 기동 재시도).
- [x] 2b.6 테스트: 소스 저장·소유자 인가·DB 집계 일치·공간 부족 507,
      `get_available_space()`에 보관 청크가 반영되는지, 이관 동작.

## Phase 3: 3카피 배치 (Requirement 1·2)

- [x] 3.1 `TARGET_COPIES`를 정책값으로 받아 `min_replicas`를 대체한다. 서버
      `min_replicas`/`reciprocity_fraction`은 구버전 클라이언트 호환 필드로만 남기고
      클라이언트는 읽지 않는다.
- [x] 3.2 `_target_locations` 구현: 다른 기기 우선, 다른 기기 후보가 하나도 없을
      때만 같은 기기의 미사용 소스, 같은 소스 재사용 금지. 여유 공간 임계는 두지
      않는다(로컬이 카피로 차는 것은 허용된 결과).
- [x] 3.3 `_replicate_chunks`가 위치 목록 기준으로 부족분만 채운다. 카피 수와
      서로 다른 기기 수를 함께 집계해 결과에 담는다.
- [x] 3.4 미달 시 사유 로그(가능 위치 수·기기 수) + `pending`. 백업 사이클에서
      즉시 재시도하지 않고 heal에 맡긴다(재시도 백오프 정리).
- [x] 3.5 테스트: 기기 1대·소스 3개에서 서로 다른 소스 3곳에 놓이는지, 소스 1개면
      1카피 + 미달인지, 다른 기기 후보가 있으면 로컬을 쓰지 않는지.

## Phase 4: heal 이전 (Requirement 3)

- [x] 4.1 `_heal_chunk`가 로컬 위치를 우선 읽는다(현재는 항상 홀더에서 fetch —
      원본을 손에 들고도 원격 왕복을 한다).
- [x] 4.2 heal이 청크별 `distinct_devices`를 확인해 3 미만이고 새 후보 기기가 있으면
      `_relocate_copy`를 호출한다.
- [x] 4.3 `_relocate_copy` 구현: 저장 → 서버 등록 → 로컬 삭제. 각 단계 실패 시
      로컬 카피 유지.
- [x] 4.4 이전은 heal 주기(기본 1h)에만 수행한다. 백업 사이클에서는 하지 않는다.
- [x] 4.5 테스트: 이전 중 각 단계 실패 시 카피 수가 유지되는지(Property 4),
      이전 완료 후 총 카피 수가 3인지, 서버 등록 실패 시 4카피가 되었다가 다음
      주기에 정리되는지.

## Phase 4b: 축출 판정 기준 (Requirement 2b)

- [x] 4b.1 축출(`_eviction_loop`의 `_is_safe`)의 안전 판정을 총 카피 수에서 **서로
      다른 기기의 카피 수**로 변경. 카피가 모두 로컬인 청크는 축출 대상에서 뺀다
      (비우면 0). 현재는 `replication_health.min_online >= min_replicas`다.
- [x] 4b.2 테스트: 다른 기기 후보가 있으면 로컬 카피를 만들지 않는지, 카피가 전부
      로컬인 청크가 축출되지 않는지.

## Phase 5: 읽기 경로 (Requirement 1.4)

- [x] 5.1 `StoragePool`의 청크 읽기가 위치 목록을 순회한다: 로컬 소스 → 다른 기기
      소스(파일 op) → ParityStore(replica_fetch).
- [x] 5.2 도달 가능한 카피가 없으면 누락 위치를 명시한 에러.
- [x] 5.3 테스트: 로컬 카피를 지운 뒤 다른 기기 카피로 읽기 성공(Property 6),
      모두 도달 불가 시 에러 메시지에 청크 index가 담기는지.

## Phase 6: 문서·정리

- [x] 6.1 `docs/ARCHITECTURE.md`의 리플리케이션 절을 카피 모델로 재작성
      (원본/복제본 표현 제거, 3카피·위치·이전 규칙 반영).
- [x] 6.1b `docs/DISTRIBUTION_POLICY.md` 갱신: 3단의 "사본은 반드시 다른 기기에
      놓인다 / 소유자 자신의 기기는 홀더 후보에서 제외"를 새 규칙으로 바꾸고(다른
      기기 후보가 없을 때만 로컬 카피), 목표 사본 수를 `min_replicas` 기본 1에서
      `target_copies` 3으로, 4단 축출 판정을 서로 다른 기기 수 기준으로 고친다.
      요약 표와 "가용성에 대한 참고"도 함께 손본다.
- [x] 6.2 `replication-ownership-progress` 스펙에서 `min_replicas` 기반 서술을
      이 스펙 참조로 갱신.
- [x] 6.3 서버 `provided_bytes` 잔여 컬럼·코드 제거(DROP COLUMN 멱등 마이그레이션,
      미지원 SQLite에서는 컬럼을 남기고 읽지 않는다).

## 검증

- 각 Phase 종료 시 `pytest` 전체 통과(구현 후 기준: 클라이언트 835 passed / 1 skipped,
  서버 120 passed).
- 실환경: 기기 1대에서 3카피가 서로 다른 소스에 놓이는지 → 기기 추가 후 heal이
  옮겨 기기 수 3이 되는지 → 로컬 카피를 지워도 읽기가 되는지.
- 용량 확인: 기기 1대일 때 실사용이 원본의 3배가 되는지(의도된 동작), 소스 용량
  부족 시 미달로 남는지.
