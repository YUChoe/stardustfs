# Implementation Plan

## Overview

design.md의 5단계를 따른다. Phase 1(저장 계층)은 실행 가능한 코딩 단위로 세분화한다.
Phase 2~5는 진입 시 선행 단계의 실측·결정을 반영해 세부 태스크로 확정한다(상위 태스크만
기재). 각 태스크는 완료 시 pytest로 검증한다(이 프로젝트의 유일한 검증 게이트).

## Tasks

### Phase 1 — 저장 계층(로컬 단일 기기, 레거시 blob 공존, 부분 읽기)

- [x] 1. chunker에 청크 배치·샤딩 헬퍼 추가
  - `chunk_range(offset, length, size) -> list[int]`: 바이트 범위를 덮는 chunk_index 목록
  - `shard_prefix(chunk_hash: str, hex_len: int = 2) -> str`: 청크 해시 앞 hex_len자 반환
  - `chunk_ref(index: int) -> str`: `<uuid32>_c<zero-padded index>` 생성(uuid는 청크마다 신규)
  - 기존 `split`/`join`/`chunk_hash`/`chunk_count`는 불변
  - _Requirements: 1.1, 4.1_

- [x] 2. metadata_store에 file_chunks 테이블과 chunked 컬럼 마이그레이션(v7) 추가
  - `_SCHEMA_SQL`에 `file_chunks` 테이블 정의(PK: virtual_path+chunk_index,
    컬럼: chunk_ref, source_id, device_id, size, hash) + `idx_file_chunks_source`
  - `_migrate_to_v7()`: `PRAGMA table_info(files)`로 멱등 검사 후 `files`에
    `chunked INTEGER NOT NULL DEFAULT 0` 추가, file_chunks 테이블 생성,
    schema_version을 7로 갱신. `initialize()`에서 호출
  - _Requirements: 2.1, 2.2, 5.1, 6.3_

- [x] 3. metadata_store에 청크 매니페스트 CRUD 추가
  - `put_chunks(virtual_path, chunks: list[ChunkRef])`: 파일 매니페스트 원자적 교체
    (기존 행 삭제 후 삽입, 단일 트랜잭션)
  - `get_chunks(virtual_path) -> list[ChunkRef]`: chunk_index 순 반환
  - `update_chunk_location(virtual_path, chunk_index, source_id, device_id)`:
    청크 단위 위치 갱신(파일 전체 재기록 금지 — Requirement 2.3)
  - `ChunkRef` 데이터클래스를 models.py에 정의
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 4. storage_pool.write_file을 청크 분할 저장으로 전환
  - 평문을 CHUNK_SIZE(4 MiB)로 split → 청크별 `EncryptionEngine.encrypt`
  - 청크별 `chunk_hash` 계산 → `shard_prefix`로 물리 경로 `<hh>/<chunk_ref>` 결정
  - 로컬 소스 선택 후 `source.write(physical_path, cipherchunk)`
  - 모든 청크 기록 성공 시에만 `put_chunks` + `files.chunked=1` 커밋
  - 일부 실패 시 기록한 청크 정리(베스트에포트) 후 규격 에러(메타데이터 미커밋 —
    반쯤 저장 금지). "graceful 건너뛰기" 금지
  - _Requirements: 1.1, 1.2, 1.3, 6.2_

- [x] 5. storage_pool.read_file / read_ciphertext를 청크 결합으로 전환
  - `chunked=1`: `get_chunks`로 매니페스트 조회 → 청크별 로컬 라우팅으로 암호문 확보
    → 청크 해시 검증(chunk-integrity-hash 경로 재사용) → decrypt → 순서대로 join
  - `chunked=0`(레거시): 기존 단일 blob 경로 그대로
  - 청크 누락/불일치 시 어느 chunk_index인지 명시한 규격 에러
  - _Requirements: 1.4, 3.2, 5.1, 6.2_

- [x] 6. storage_pool.read_range(vpath, offset, length) 부분 읽기 추가
  - `chunk_range`로 범위를 덮는 chunk_index만 계산 → 해당 청크만 가져와 복호화
  - 경계 걸침·마지막 부분 청크 처리, 요청 범위로 정확히 잘라 반환
  - _Requirements: 4.1_

- [x] 7. orphan GC를 샤드 디렉토리 재귀 스캔으로 수정
  - `LoopbackSource.list_physical_files`가 루트만 훑던 것을 샤드 서브디렉토리까지
    재귀하도록 변경(`walk.files()` 활용), 청크 파일명 `<hh>/<hex32>_cNNNN`을
    관리 파일로 인식
  - _Requirements: 2.1_

- [x] 8. Phase 1 검증 테스트 작성
  - 청크 라운드트립: 경계 정렬·부분 청크·빈 파일에서 write→read 바이트 동일
    (Property 1)
  - 청크 독립 복호화: 각 청크가 단독 복호화(청크별 nonce/tag) (Property 2)
  - 부분 읽기: 단일 청크 내·청크 경계 걸침·전체 범위 (Property 3)
  - 레거시 호환: chunked=0 blob이 기존 경로로 계속 읽힘 (Property 5)
  - 샤드 분산: 청크가 chunk_hash 앞 2hex로 분산 배치되는지, orphan GC가 샤드
    디렉토리를 인식하는지
  - _Requirements: 1.1, 1.2, 1.4, 4.1, 5.1_

### Phase 2 — 동기화(파일 레코드에 청크 매니페스트 실기)

- [x] 9. metadata_records 페이로드에 chunks 매니페스트 포함·병합
  - 파일 레코드 JSON에 `chunks: [{index, chunk_ref, source_id, device_id, size,
    hash}]` 추가(chunked=0이면 생략). record_id·CAS·롱폴 프로토콜 불변
  - 병합 시 매니페스트를 통째로 채택, 레거시(chunks 없음) 레코드 호환
  - 검증 테스트 포함
  - _Requirements: 2.1, 5.1_

### Phase 3 — 분산(청크 단위 스필오버·축출·원격 라우팅)

- [x] 10. 스필오버/evacuate/detach/evict를 청크 단위로 수행
  - 이동 단위를 파일에서 청크로, `update_chunk_location`으로 청크별 위치 갱신
  - _Requirements: 1.3, 2.2, 2.3_

- [x] 11. read 경로의 청크별 원격 라우팅
  - 청크가 여러 기기에 분산됐을 때 청크별 로컬/원격 소스 선택 후 결합
  - _Requirements: 1.3, 3.1, 3.3_

### Phase 4 — 복제 정합(at-rest 청크 재사용)

- [ ] 12. replication_manager가 at-rest 청크를 재분할 없이 재사용
  - 파일이 청크 표현이면 각 at-rest 청크를 복제 청크로 그대로 사용, recover는 받은
    청크를 소스에 되돌려 기록. orphan GC 파일명 규칙 정합
  - _Requirements: 6.1_

### Phase 5 — 마이그레이션(blob→청크 전환, FAT 샤딩 확정)

- [ ] 13. 레거시 blob의 청크 전환(수정/선택 시)을 무손실로 수행
  - 대상 청크 기록 성공 후에만 원본 blob 삭제(중단돼도 원본 온전)
  - 2단계 샤딩 심화 진입 임계치 확정(볼륨 크기·예상 청크 수 기준)
  - 무손실 검증 테스트(전환 중단 주입) 포함
  - _Requirements: 5.2, 5.3_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "3"] },
    { "wave": 2, "tasks": ["4", "7"] },
    { "wave": 3, "tasks": ["5"] },
    { "wave": 4, "tasks": ["6"] },
    { "wave": 5, "tasks": ["8"] },
    { "wave": 6, "tasks": ["9"] },
    { "wave": 7, "tasks": ["10"] },
    { "wave": 8, "tasks": ["11"] },
    { "wave": 9, "tasks": ["12"] },
    { "wave": 10, "tasks": ["13"] }
  ]
}
```

```
1(chunker 헬퍼) ─┬─> 4(write_file) ─┬─> 5(read_file) ──> 6(read_range) ──> 8(테스트)
2(스키마 v7) ────┤                  │
3(매니페스트 CRUD)┘                  └─> 7(orphan GC)

8(Phase 1 테스트) ──> 9(동기화) ──> 10(분산 이동) ──> 11(원격 라우팅)
                                                        │
                                    12(복제 재사용) <───┘
                                    13(마이그레이션) ── 선행: 4,5 (전환 대상 경로)
```

- 1·2·3은 상호 독립(병렬 가능). 4는 1·2·3 완료 후.
- 5는 4 완료 후, 6은 5 완료 후, 7은 2 완료 후(4와 병렬 가능).
- 8은 4·5·6·7 완료 후 Phase 1 게이트.
- Phase 2~5는 각 진입 시 세부 태스크 확정.

## Notes

- 검증 게이트는 pytest뿐(ruff/mypy 미설치). 클라이언트는 `PYTHONPATH=. python -m pytest -q`.
- 신규 파일은 `from __future__ import annotations`, LF+UTF8, 프로덕션 Python 3.9 호환.
- 샤드 키·깊이 결정 근거는 design.md "FAT 디렉토리 샤딩(실측으로 확정)" 참조.
- "graceful 건너뛰기" 금지: 용량 부족 등은 규격 에러로 반환.
- 용어: JBOD("Just a Bunch Of Disks")는 이 컴포넌트의 책임(원격 기기 라우팅·스필오버·
  축출·복구)과 맞지 않아 폐기했다. `jbod_manager.py`/`JBODManager` →
  `storage_pool.py`/`StoragePool`, 변수·속성은 `storage_pool`. 신규 코드에서 jbod를
  다시 쓰지 않는다. 완료된 기존 스펙 문서와 knowledge-graph는 이력이므로 원문 유지.
