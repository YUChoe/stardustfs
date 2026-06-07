---
inclusion: manual
---

# 리모트 대용량 파일 청크 전송 — 설계

## 개요
리모트 파일 쓰기/읽기를 4 MiB 청크로 분할해 rudp(~78.6MB)·홀더(100MB)·릴레이 한계를
넘는 대용량 파일도 스필오버/evacuate/read-back이 동작하게 한다. 메타데이터 모델은
그대로(리모트 파일 1개 항목)이고 청크화는 RemoteSource ↔ 홀더 전송 계층에만 존재한다.

## Components and Interfaces

### storage_source.py (홀더 측 저장)
- `StorageSource.write_chunk(physical_path, data, offset, total_size)` 추가:
  - offset=0: total_size로 용량 검사 후 파일을 새로 만들고(truncate) 0에 기록.
    used_bytes는 total_size로 1회 예약(중복 합산 방지).
  - offset>0: `r+b`로 열어 seek(offset) 후 기록.
  - LoopbackSource·DirectorySource에 구현(공통 로직은 헬퍼로).
- `StorageSource.read_chunk(physical_path, offset, length) -> bytes` 추가:
  `rb`로 열어 seek(offset) 후 length 바이트 읽기.

### p2p_server.py (홀더 측 op)
- `_op_write_chunk(body)`: body={physical_path, data(b64), offset, total_size,
  source_id?}. offset=0이고 source_id 없으면 `select_source(total_size)`(없으면 507),
  offset>0이면 직전 응답의 source_id로 소스 확정. `source.write_chunk(...)`. 200에
  source_id 반환. data > MAX_WRITE_SIZE면 413(청크가 4 MiB라 정상 경로는 안 걸림).
- `_op_read_chunk(body)`: body={physical_path, offset, length, source_id?}.
  `source.read_chunk(...)`를 b64로 반환. 파일 없으면 404. 응답에 total_size 포함.
- 라우트(`/p2p/write_chunk`,`/p2p/read_chunk`) + handle_* + dispatch_async 등록
  (인가는 기존 파일 op와 동일 — 같은 user_id).

### remote_source.py (owner 측)
- `push_blob`/`write`: len(data) ≤ REMOTE_CHUNK_SIZE면 기존 `/p2p/write`(하위호환).
  초과면 4 MiB 청크 루프: 첫 청크 offset=0+total_size(→source_id 획득), 이후 offset
  누적. 중간 실패 시 `/p2p/delete`로 부분 파일 정리 후 OSError.
- `read_from_source`: 먼저 크기를 안다(첫 read_chunk 응답의 total_size). total_size ≤
  REMOTE_CHUNK_SIZE면 단일 `/p2p/read`(하위호환). 초과면 offset/length 범위 루프로
  이어붙인다. 모든 청크 전송은 기존 `_fallback`(직접 TCP→홀펀칭 UDP→릴레이) 캐스케이드와
  auth_token 인가를 그대로 탄다.

### jbod_manager.py / daemon_control.py
- 변경 없음. `_write_to_remote`/`_evacuate_to_remote`는 push_blob을 그대로 호출하고,
  read_file는 read_from_source를 그대로 호출한다(청크화는 내부적으로 투명).

## Data Models
- 변경 없음. files 테이블·replication 테이블 불변. 청크 상태는 전송 중에만 존재(영속화
  안 함). DB 스키마 변경 없음 → 마이그레이션·롤백 불요.

## Correctness Properties

### Property 1: 라운드트립 항등
*임의의* 바이트열 m에 대해, push_blob(m) 후 read_from_source가 m을 그대로 돌려준다
(크기·청크 경계 무관, 4 MiB 미만/배수/비배수 포함).

### Property 2: 전송 한계 독립성
*임의의* 크기 m(예: 713 MiB)에 대해, 리모트에 충분한 여유가 있으면 push가 성공한다
(rudp 78.6MB·홀더 100MB 단일 한계에 걸리지 않음).

### Property 3: 실패 원자성
*임의의* 중간 청크 실패에 대해, 홀더에 부분 파일이 남지 않고(rollback) 호출자는 명시적
OSError를 받는다(용량 부족 507과 전송 실패를 구분).

## Error Handling
- 홀더 용량 부족: `select_source(total_size)` 실패 → 507 → owner는 InsufficientStorage로
  전파(전송 크기 한계가 아닌 진짜 용량 부족).
- 청크 중간 실패: owner가 `/p2p/delete`로 부분 파일 삭제 시도 후 OSError 재발생.
- 읽기 중 청크 누락/오류: 명시적 OSError(누락 offset 표시).
- 하위호환: 구버전 홀더가 `/p2p/write_chunk` 미지원(404)이면 owner는 단일 write로
  폴백하되 크기 한계 초과 시 명시적 오류.

## Testing Strategy
- storage_source: write_chunk/read_chunk 단위(offset=0 생성, offset>0 seek, 범위 읽기,
  용량 부족 OSError).
- p2p_server: `_op_write_chunk`/`_op_read_chunk` (source_id 선택·507·404·라운드트립).
- remote_source: 가짜 홀더로 4 MiB 미만/초과 라운드트립, 중간 실패 rollback.
- 통합(회귀): 로컬 만석 + 대용량(>78.6MB) 스필오버→리모트 기록→read-back 바이트 일치.
- 하위호환: 4 MiB 이하 파일은 기존 단일 경로 사용(청크 op 미호출).
