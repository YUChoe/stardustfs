# Design Document

## Overview

파일을 저장 시점부터 고정 크기 청크로 나눠, 각 청크를 독립 암호화해 소스에 보관한다.
파일 하나의 청크들은 서로 다른 소스·기기에 분산될 수 있다. at-rest·전송·복제가 모두
같은 청크 표현을 공유하므로, 내 기기와 다른 계정 기기는 데이터 취급 관점에서 동일한
"암호문 청크 보관소"가 된다.

핵심 결정 두 가지로 범위를 억제한다.

1. 동기화 단위는 여전히 파일이다. 파일 메타데이터 레코드 안에 청크 매니페스트(청크
   목록)를 담는다. 그러면 파셜 메타데이터 동기화(record_id = HMAC(virtual_path))의
   레코드 구조·프로토콜을 바꾸지 않는다. 서버는 여전히 파일 단위 암호문 레코드만 본다.
2. 레거시 통짜 blob은 "청크 1개인 파일"의 특수 형태로 공존시킨다. 읽기는 형식으로
   구분하고, 쓰기·마이그레이션 시 청크 표현으로 전환한다.

## Architecture

```
write_file(vpath, data)
  → split(data, CHUNK_SIZE)                     평문 청크들
  → 각 청크 EncryptionEngine.encrypt            청크별 헤더+GCM(독립 nonce/tag)
  → placement: 청크별 소스 선택(로컬 우선, 만석 시 리모트 스필오버)
  → source.write(chunk_ref, cipherchunk)         소스에 저장
  → metadata: file_chunks 행들 + files 행(청크 매니페스트)

read_file(vpath)                                 read_range(vpath, 0, size)의 특수형
  → file_chunks 조회 → 필요한 chunk_index만
  → 청크별 로컬/원격 라우팅으로 암호문 확보 → decrypt → 이어붙임
```

## Components and Interfaces

### chunker (stardustlib/chunker.py) — 기존 재사용/확장
- `split`/`join`/`chunk_hash`는 그대로. 청크 경계 계산 헬퍼
  `chunk_range(offset, length, chunk_size) -> list[int]`(범위를 덮는 인덱스) 추가.

### MetadataStore (stardustlib/metadata_store.py)
- `file_chunks` 테이블 CRUD:
  - `put_chunks(virtual_path, chunks: list[ChunkRef])` — 파일의 청크 매니페스트 교체
  - `get_chunks(virtual_path) -> list[ChunkRef]` — chunk_index 순
  - `chunk` 단위 소유/위치 갱신(스필오버·evacuate·축출용)
- `files` 행은 파일 단위 속성만 유지(size/version/sync_status/deleted/replication_status).
  단일 blob 레거시 파일은 `chunked=0`, 청크 파일은 `chunked=1`로 구분.

### StoragePool (stardustlib/storage_pool.py)
- `write_file` → 청크 분할·배치. 청크별 `select_source`(로컬)·`_write_to_remote`(스필오버).
- `read_file`/`read_ciphertext` → 청크별 라우팅 후 결합. 레거시(chunked=0)는 기존 단일
  경로.
- `read_range(vpath, offset, length)` — 범위를 덮는 청크만 가져와 복호화(부분 읽기).
- orphan GC: 청크 파일명(`<hex32>_cNNNN`)도 관리 파일로 인식.
- 스필오버/evacuate/detach/evict를 청크 단위로 수행.

### ReplicationManager (stardustlib/replication_manager.py)
- 이미 at-rest 암호문을 청킹한다. 파일이 이미 청크 표현이면 각 at-rest 청크를 그대로
  복제 청크로 재사용(재분할 없음). recover는 청크를 소스에 되돌려 기록.

### 동기화 (sync_client + metadata_records)
- 파일 레코드 페이로드(JSON)에 `chunks: [{index, chunk_ref, source_id, device_id,
  size, hash}]`를 포함. record_id·CAS·롱폴은 불변. 병합은 매니페스트를 통째로 채택.

## Data Models

### 로컬 스키마(SQLite) 변경

```sql
-- 파일의 청크 배치(파일당 N행). 레거시 단일 blob은 이 테이블에 1행(index=0)으로 표현.
CREATE TABLE IF NOT EXISTS file_chunks (
    virtual_path  TEXT    NOT NULL,
    chunk_index   INTEGER NOT NULL,
    chunk_ref     TEXT    NOT NULL,   -- 소스 내 물리 경로(<hex32>_cNNNN)
    source_id     TEXT    NOT NULL,
    device_id     TEXT,               -- 청크 보관 기기(NULL=로컬 레거시)
    size          INTEGER NOT NULL,   -- 청크 암호문 크기
    hash          TEXT,               -- 암호문 SHA-256 hex(무결성)
    PRIMARY KEY (virtual_path, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_file_chunks_source
    ON file_chunks(source_id);
```

`files` 테이블에 `chunked INTEGER NOT NULL DEFAULT 0` 추가(마이그레이션). 기존
source_id/physical_path/device_id 컬럼은 레거시(chunked=0) 파일에만 유효하고, 청크
파일은 file_chunks가 정본이다.

### ChunkRef (런타임)
`{index:int, chunk_ref:str, source_id:str, device_id:str|None, size:int, hash:str}`.

### 청크 파라미터
- CHUNK_SIZE: 평문 기준 4 MiB(전송 청크와 정합). 마지막 청크는 그 이하.
- 청크 암호문 = `EncryptionEngine.encrypt(청크 평문)`(38B 헤더 + GCM, 청크별 독립 nonce).
- chunk_ref = `<uuid32>_c<zero-padded index>` — orphan GC의 `<hex32>_` 규칙과 호환.

### FAT 디렉토리 샤딩(실측으로 확정)

pyfatfs는 순수 파이썬이라 파일 생성 시 부모 디렉토리 엔트리를 선형 스캔한다(이름
충돌 검사 + 빈 슬롯 탐색). 그래서 한 디렉토리에 파일을 채우면 O(n²)로 느려진다.
2 GiB FAT32 이미지에 64B 파일을 채운 실측(2026-07-25):

| 구간(파일) | 평면 배치(직전 500개) | 앞 2hex 샤딩(직전 500개) |
| --- | --- | --- |
| 1–500 | 13.3s | 13.1s |
| 501–1000 | 23.9s | 12.3s |
| 1001–1500 | 36.5s | 11.8s |
| 1501–2000 | 50.3s(누적 124s) | 11.9s |
| 2001–3000 | (급증) | 12.0s |

평면은 배치 시간이 계속 증가(O(n²)), 256개 서브디렉토리 샤딩은 배치당 ~12s로 평탄
(선형). FAT 하드 캡은 디렉토리당 엔트리 65,536개인데, chunk_ref 38자는 LFN을 유발해
파일당 4엔트리(LFN 3 + short 1)를 써 평면 하드 캡은 실질 약 16,384개다. 그러나 그
한계 전에 위 성능 절벽이 먼저 온다. 결론: 샤딩은 하드 캡과 성능을 모두 해소한다.

샤딩 규칙:
- 샤드 키는 파일 uuid 접두사가 아니라 청크 암호문 해시 앞 2hex(`chunk_hash[:2]`).
  파일당 uuid 하나를 접두사로 쓰면 한 파일의 모든 청크가 같은 디렉토리로 몰려(대용량
  파일 1개 = 청크 수천 개) O(n²)가 재현된다. 청크 해시는 청크마다 달라 균등 분산되고,
  chunk-integrity-hash가 이미 계산하므로 재사용한다. 경로는 `<hh>/<uuid32>_cNNNN`.
- 깊이는 볼륨 크기에 비례. 기본 1단계(256 디렉토리). 각 서브디렉토리는 최소 1클러스터를
  점유하므로 2단계(65,536 디렉토리)는 큰 볼륨에서 디렉토리 클러스터만 수 GiB를 낭비하고
  최소 10 MiB FAT16 볼륨은 서브디렉토리 여유가 없다. 소스가 256×임계치를 넘게 보관할
  것으로 예상될 때만 2단계로 심화.
- orphan GC의 `list_physical_files`는 현재 루트만 훑으므로 샤드 디렉토리로 재귀하도록
  변경한다(`_scan_used_space`는 `walk.files()`로 이미 재귀).

### 동기화 레코드 페이로드(확장)
기존 FileMetadata JSON에 `chunks` 배열 추가. `chunked=0`이면 `chunks` 생략(레거시).

## Correctness Properties

Property 1: 라운드트립 — write_file 후 read_file은 원본 평문과 바이트 단위로 동일하다
(청크 경계·마지막 부분 청크 포함).
**Validates: Requirements 1.1, 1.4**

Property 2: 청크 독립성 — 각 청크는 다른 청크 없이 단독으로 복호화된다(청크별 nonce·태그).
**Validates: Requirements 1.2**

Property 3: 부분 읽기 정확성 — read_range(offset, length)는 그 범위를 덮는 청크만
가져와도 해당 바이트를 정확히 반환한다.
**Validates: Requirements 4.1**

Property 4: 재개 — 이미 확보한 청크를 재사용하고 누락 청크만 다시 받아도 결과 평문은
전량 재수신과 동일하다.
**Validates: Requirements 4.2**

Property 5: 레거시 호환 — chunked=0 파일은 기존 단일 blob 경로로 계속 읽힌다.
**Validates: Requirements 5.1**

Property 6: 마이그레이션 무손실 — blob→청크 전환 중 실패해도 원본(blob 또는 청크)
중 하나는 온전히 남아 읽을 수 있다.
**Validates: Requirements 5.2, 5.3**

## Error Handling

- 청크 배치 중 일부 실패: 이미 기록한 청크를 정리하고(베스트에포트) 규격 에러.
  메타데이터는 커밋하지 않아 파일이 반쯤 저장된 상태로 남지 않는다.
- 읽기 중 청크 누락/도달 불가: 어느 chunk_index가 없는지 명시한 규격 에러
  (복제가 있으면 recover로 보충 후 재시도).
- 청크 해시 불일치: chunk-integrity-hash의 검증 경로 재사용(다음 소스/홀더 시도).
- 마이그레이션 실패: 무손실 규칙(대상 기록 성공 후에만 원본 삭제).

## Testing Strategy

- 청크 라운드트립(경계·부분 청크·빈 파일), 청크 독립 복호화.
- 부분 읽기: 단일 청크 내, 청크 경계 걸침, 전체.
- 스필오버로 청크가 여러 기기에 분산됐을 때 read가 청크별 라우팅으로 결합.
- 레거시 blob 읽기 유지, blob→청크 마이그레이션 무손실(중단 주입).
- 복제: 청크 파일의 at-rest 청크가 재분할 없이 복제/복구되는지.
- 동기화: chunks 매니페스트가 레코드에 실려 병합되는지, 레거시(chunks 없음) 호환.

## 단계별 구현(범위 억제)

큰 변경이므로 단계로 나눠, 각 단계가 독립적으로 통과·배포 가능하게 한다.

1. Phase 1 — 저장 계층: file_chunks 스키마 + StoragePool write/read를 청크 기반으로.
   단일 기기(로컬)만. 레거시 blob 공존. 부분 읽기 포함.
2. Phase 2 — 동기화: 레코드 페이로드에 chunks 매니페스트, 병합.
3. Phase 3 — 분산: 청크 단위 스필오버/evacuate/축출, 청크별 원격 라우팅.
4. Phase 4 — 복제 정합: at-rest 청크 재사용(재분할 제거), orphan GC 파일명 규칙.
5. Phase 5 — 마이그레이션: 수정/선택 시 blob→청크 전환, FAT 샤딩 확정.

각 단계의 tasks는 해당 단계 진입 시 별도로 확정한다(선행 단계의 실측 결과 반영).

## 미해결/실측 필요

- pyfatfs 디렉토리 한계·샤딩: 실측으로 확정(위 "FAT 디렉토리 샤딩" 참조). 남은 것은
  2단계 심화 진입 임계치의 구체값(볼륨 크기·예상 청크 수 기준)뿐.
- CHUNK_SIZE 확정: 작을수록 부분 읽기·병렬성 유리, 클수록 메타데이터·오버헤드 유리.
  전송 청크(4 MiB)와 정합해 4 MiB 잠정. 샤딩당 ~24ms 고정 오버헤드는 4 MiB 실데이터
  쓰기 시간에 묻히므로 문제되지 않음.
- 부분 쓰기(파일 일부만 수정): 현재는 파일 전체 재기록. 청크 단위 부분 쓰기는 후속.
