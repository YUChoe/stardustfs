---
inclusion: manual
---

# 리모트 대용량 파일 청크 전송 — Requirements

## 배경
스필오버(`_write_to_remote`)와 evacuate(`_evacuate_to_remote`)는 암호문 파일 전체를
단일 `/p2p/write` blob으로 리모트 홀더에 push한다. 리모트 파일 읽기(`read_from_source`)도
단일 `/p2p/read`로 전체를 받는다. 이 단일 전송은 다음 한계를 넘으면 실패한다:
- rudp 단일 메시지 상한: frag_count(uint16) × 1200B ≈ 78.6MB (초과 시 ValueError
  "메시지가 너무 큼").
- 홀더 `MAX_WRITE_SIZE` = 100MB (초과 시 413).
- 릴레이(nginx) 본문 한계 (대용량 시 413).

실제 사례: 713 MiB(747,601,920 B) 업로드가 로컬 만석 → 리모트 스필오버 → rudp
"830670 프래그먼트 > 65535" → 릴레이 413 → `_write_to_remote` False → 원래
InsufficientStorageError 재발생으로 "여유 공간 부족"으로 오표시(리모트엔 여유 있음).

## Requirements

### Requirement 1: 청크 분할 리모트 쓰기
*임의의* 크기의 암호문을 리모트 홀더에 기록할 수 있어야 한다.
- WHEN 전송할 데이터가 청크 임계값(`REMOTE_CHUNK_SIZE`, 4 MiB)을 초과하면 THE
  RemoteSource SHALL 데이터를 4 MiB 청크로 나눠 순차 전송한다.
- WHEN 첫 청크(offset=0)를 전송하면 THE 홀더 SHALL 전체 크기(total_size) 이상의
  여유가 있는 소스를 선택하고 파일을 새로 만들며 사용한 source_id를 응답한다.
- WHEN 후속 청크(offset>0)를 전송하면 THE 홀더 SHALL 같은 소스·경로의 해당 offset에
  기록한다.
- IF 데이터가 임계값 이하이면 THE RemoteSource SHALL 기존 단일 `/p2p/write` 경로를
  사용한다(하위호환).

### Requirement 2: 범위 분할 리모트 읽기
*임의의* 크기의 리모트 파일을 읽을 수 있어야 한다.
- WHEN 리모트 파일 크기가 `REMOTE_CHUNK_SIZE`를 초과하면 THE RemoteSource SHALL
  offset/length 범위로 나눠 순차로 읽어 이어붙인다.
- IF 파일 크기가 임계값 이하이면 THE RemoteSource SHALL 기존 단일 `/p2p/read`
  경로를 사용한다(하위호환).

### Requirement 3: 정확한 실패 분류
- IF 리모트에 total_size 이상의 여유가 있는 소스가 없으면 THE 홀더 SHALL HTTP 507을
  반환하고 owner는 이를 용량 부족으로 전파한다(전송 크기 한계와 구분).
- IF 청크 전송 중 한 청크가 실패하면 THE RemoteSource SHALL 부분 파일을 홀더에서
  삭제(rollback)하고 명시적 OSError를 발생시킨다(조용한 손실 없음).

### Requirement 4: 메타데이터 모델 불변
- THE 스필오버/evacuate SHALL 리모트 파일을 여전히 단일 메타데이터 항목(device_id=
  홀더, source_id, physical_path, file_size)으로 등록한다. 청크화는 전송 계층에만
  존재하고 메타데이터·읽기 라우팅 의미를 바꾸지 않는다. (DB 스키마 변경 없음 →
  마이그레이션 불요.)

## 비기능
- 청크 크기 4 MiB: rudp(≈3500 프래그먼트 < 65535)·홀더 100MB 한계 내. 홀펀칭 UDP가
  1차 경로. 릴레이는 fallback이며 nginx `client_max_body_size`가 4 MiB 이상이어야
  릴레이 경유 청크가 통과(인프라, 별도).
- zero-knowledge 유지: 청크는 이미 at-rest 암호문의 부분 바이트이며 재암호화하지 않는다.
