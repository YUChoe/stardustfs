---
inclusion: manual
---

# 리모트 대용량 파일 청크 전송 — Tasks

## Phase 1: 홀더 측 저장 (storage_source)
- [x] 1.1 `StorageSource.write_chunk(path, data, offset, total_size)` + `read_chunk(
      path, offset, length)`. 베이스는 미구현, LoopbackSource·DirectorySource가 seek
      기반 오버라이드. offset=0은 용량 검사(total_size)+생성, offset>0은 seek 기록.
      Loopback used_bytes는 실제 파일 증분으로 추적(rollback 정합).
- [x] 1.2 단위 테스트: 생성/seek/범위읽기/용량부족 OSError(test_remote_chunked_transfer).

## Phase 2: 홀더 측 op (p2p_server)
- [x] 2.1 `_op_write_chunk`/`_op_read_chunk` + 라우트 `/p2p/write_chunk`·
      `/p2p/read_chunk` + handle_* + dispatch op_map(dispatch_async가 같은 user 검증 후
      dispatch 경유). offset=0 source 선택(507), offset>0 source_id 확정.
- [x] 2.2 테스트: source_id 선택·507·404·청크 라운드트립.

## Phase 3: owner 측 청크 루프 (remote_source)
- [x] 3.1 `REMOTE_CHUNK_SIZE`(4 MiB). push_blob/write: 임계값 초과 시 _push_chunked
      (첫 청크 total_size→source_id, 이후 offset), 중간 실패 rollback(delete)+OSError.
      이하면 단일 write(하위호환). _ENDPOINT_OP에 write_chunk/read_chunk 추가.
- [x] 3.2 read_from_source(file_size): 평문 file_size>임계값이면 _read_chunked(범위
      루프, 암호문 크기 모르므로 짧은 읽기=EOF), 이하면 단일 read(하위호환). jbod
      read_file이 metadata.file_size 전달. 모든 청크는 _fallback 캐스케이드+auth_token.
- [x] 3.3 테스트: 가짜 홀더(dispatch 직결) 4 MiB 미만/초과 라운드트립, 중간 실패 rollback.

## Phase 4: 통합/회귀
- [x] 4.1 통합: RemoteSource↔P2PServer(LoopbackSource) >4 MiB push→read-back 바이트
      일치, 507 구분, 소량은 단일 write 경로 확인.
- [x] 4.2 회귀 581 passed/1 skip. TRANSPORT.md에 청크 전송 반영.

## 비범위
- 청크 병렬 전송(초기엔 순차). 재개(resume) 가능한 부분 업로드.
- 릴레이 nginx client_max_body_size 상향(인프라).
- replication 청크 store 실패([0,0,0,1] pending) — 별도 건.
