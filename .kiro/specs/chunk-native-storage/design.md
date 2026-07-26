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
- CHUNK_SIZE: 평문 기준 4 MiB. 마지막 청크는 그 이하. 근거는 아래 "CHUNK_SIZE 선택
  근거" 참조(측정으로 최적화한 값이 아니라 전송 계층 제약과 정합에서 나온 값이다).
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
  최소 10 MiB FAT16 볼륨은 서브디렉토리 여유가 없다.
- 심화 임계치(확정): 디렉토리당 청크 수 상한을 512로 둔다. 실측에서 평면 배치는 500개
  까지 기준선 속도를 유지했고 1000개 시점에 배치 시간이 2배가 됐으므로, 그 경계 아래로
  유지하는 값이다. 1단계 수용량 = 256 샤드 × 512 청크 × 4 MiB = 512 GiB. 소스 용량이
  이를 넘으면(예: 2 TiB) 2단계로 깊인다. `chunker.shard_depth_for(capacity)`가
  예상 청크 수를 샤드 수로 나눠 최소 깊이를 고르고, StoragePool이 청크 경로를 만들 때
  소스 용량으로 호출한다. 경로는 매니페스트에 그대로 저장되므로 깊이가 소스마다 달라도
  (또는 나중에 바뀌어도) 읽기에는 영향이 없다.
- orphan GC의 `list_physical_files`는 현재 루트만 훑으므로 샤드 디렉토리로 재귀하도록
  변경한다(`_scan_used_space`는 `walk.files()`로 이미 재귀).

### CHUNK_SIZE 선택 근거

4 MiB는 측정으로 최적화한 값이 아니다. 정직하게 적으면 근거는 둘이다.

1. 정합. `chunker.DEFAULT_CHUNK_SIZE`(MVP3 복제)와 `remote_source.REMOTE_CHUNK_SIZE`가
   이미 4 MiB다. 저장 청크가 같은 경계를 쓰면 at-rest 청크를 재분할 없이 그대로 복제
   청크로 쓸 수 있다(Phase 4). 경계가 다르면 그 재사용이 불가능하다.
2. 전송 계층 제약 안에서 안전한 쪽. 상한은 실재하지만 4 MiB를 특정하지는 않는다.

전송 계층 제약(rudp, 실측 아닌 코드 상수):

| 항목 | 값 | 4 MiB일 때 |
| --- | --- | --- |
| 프래그먼트 payload | 1200 B(+11 B 헤더) | 3,496 조각 |
| `frag_count` 폭 | uint16 → 65,535 조각 | 하드 캡 78.6 MB(20배 여유) |
| in-flight 윈도우 | 256 조각 ≈ 307 KB | RTT 50 ms에서 상한 ≈ 6 MB/s |
| 무진행 라운드 상한 | 10회 × 0.3 s ≈ 3 s | 전송 <1 s이라 노출 짧음 |
| 홀더 `MAX_WRITE_SIZE` | 100 MB | 여유 |

청크를 키우면 생기는 리스크(난이도가 아니라 실패 단위·노출 시간의 문제):

- 전부-또는-전무 재조립. `_on_data`는 `len(buf) < frag_count`면 반환하고 부분 전달이
  없다. 무진행 3초를 넘기면 TimeoutError로 청크 전체가 폐기되고 처음부터 다시 받는다.
  청크가 크면 전송이 길어져 그 창에 걸릴 확률이 전송 시간에 비례해 늘고, 한 번 걸릴 때
  버리는 작업량도 청크 크기에 비례한다.
- 속도 이득이 없다. 처리량은 윈도우/RTT로 묶여 있어 청크를 키워도 상한이 그대로다.
  64 MiB 청크는 최소 ~11초가 걸리고 그 내내 3초 스톨 한 번에 전량이 날아간다.
- 메모리 선형 증가. 수신 측은 1200 B 조각을 dict에 담아 청크 전체를 보유하고, 송신
  측도 조각 리스트를 보유한다. 완성 바이트 + HTTP/릴레이의 base64 사본(4/3배)까지
  더하면 전송 하나당 양쪽에서 청크 크기의 2~3배가 잡히며, 데몬 백업 병렬도(기본 4)를
  곱해야 한다.
- 미완성 버퍼에 만료·정리가 없다. `self._recv`는 완성 시에만 pop되므로 송신 측이 중간에
  죽으면 부분 버퍼가 프로세스 수명 동안 남는다. 청크가 크면 잔여물도 크다.
- 릴레이 본문 한계를 계속 밀어올려야 한다. 실제로 4 MiB 청크가 nginx 기본
  `client_max_body_size` 1 MiB에 걸려 413으로 거부돼 백업이 pending에 머문 이력이
  있고 16m으로 올려 해결했다.
- 64 MiB는 하드 캡까지 여유가 1.17배뿐이고 128 MiB는 ValueError로 전송 자체가 불가하다.

청크를 줄이면 반대 비용이 든다. 매니페스트 행 수, 청크당 38 B 헤더, FAT 디렉토리 엔트리
수, 요청 왕복이 늘어난다. 2 TiB 볼륨에서 4 MiB는 52만 청크지만 1 MiB로 줄이면 210만
청크가 되어 샤딩을 한 단계 더 깊게 해야 한다. 부분 읽기 낭비는 줄어든다(read_range가
청크 단위로 전량을 복호화하므로 10 B를 읽어도 4 MiB를 처리한다).

결론: 현재 전송 구조에서는 키우면 이득 없이 실패 비용만 커진다. 크기를 키우는 것이
의미를 갖으려면 먼저 윈도우를 키우거나 부분 전달·체크포인트 재개를 넣어야 한다. 최적값
실측은 워크로드(파일 크기 분포, 부분 읽기 비율, 실효 대역)를 확정한 뒤의 과제다.

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

## 미해결/후속

- pyfatfs 디렉토리 한계·샤딩: 실측으로 확정(위 "FAT 디렉토리 샤딩" 참조). 2단계 심화
  임계치도 디렉토리당 512청크(1단계 수용량 512 GiB)로 확정·구현 완료.
- CHUNK_SIZE: 4 MiB로 채택했으나 측정으로 최적화한 값은 아니다(근거·리스크는 위
  "CHUNK_SIZE 선택 근거"). 현재 전송 구조에서는 키울 이유가 없고, 키우려면 윈도우 확대
  또는 부분 전달·체크포인트 재개가 선행돼야 한다. 최적값 실측은 워크로드 확정 후 과제.
  참고로 샤딩당 ~24 ms 고정 오버헤드는 4 MiB 실데이터 쓰기 시간에 묻힌다.
- 부분 쓰기(파일 일부만 수정): 현재는 파일 전체 재기록. 청크 단위 부분 쓰기는 후속.
- 일괄 마이그레이션 스케줄러: `StoragePool.migrate_to_chunks(vpath)`로 파일 단위 전환은
  가능하고, 수정 시 자동 전환도 동작한다. 남은 레거시 파일을 데몬이 배경에서 훑어
  전환하는 스케줄러는 후속(정책·속도 제한 필요).
- 원격 청크 부분 읽기: `read_range`가 원격 청크도 라우팅하지만 청크 단위로 전량을
  받는다. 청크 내부 범위 전송(원격 read_chunk 활용)은 후속.
