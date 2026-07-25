# Design Document

## Overview

청크 등록 시 암호문 내용 해시(`SHA-256` hex)를 함께 기록하고, 복구·재복제에서 받은
청크를 그 해시로 검증한다. 검증 실패는 그 홀더만 배제하고 다음 홀더로 넘어간다.
`chunk_id`(위치 식별자)의 정의와 기존 전송 캐스케이드는 바꾸지 않는다.

## Architecture

```
replicate: 암호문 청크 ─ SHA-256 ─▶ POST /replication/chunks {chunk_id, .., hash}
                                                    │
                                            chunks.hash (TEXT NULL)
                                                    ▼
recover:  GET /replication/chunks/{file_ref} → [{chunk_id, idx, size, hash}]
          홀더에서 fetch → SHA-256 비교 → 불일치면 다음 홀더 → join → decrypt

heal:     온라인 홀더에서 fetch → SHA-256 비교 → 통과분만 새 홀더로 복사
```

검증은 소유자 클라이언트에서만 수행한다. 서버는 해시를 불투명 문자열로 저장·전달한다.

## Components and Interfaces

### 클라이언트: chunker (stardustlib/chunker.py)
- `chunk_hash(data: bytes) -> str` — 암호문 청크의 `SHA-256` hex. 순수 함수.

### 클라이언트: ReplicationManager (stardustlib/replication_manager.py)
- `_register_chunk(...)`에 `chunk_hash` 인자 추가 → 서버 등록 payload에 포함.
- `_fetch_from_any_holder(token, chunk_id, expected_hash)` — 받은 데이터가 해시와
  다르면 그 홀더를 건너뛰고 다음 홀더 시도. `expected_hash`가 None이면 검증 생략.
- `_recover_chunks` — `_list_chunks` 응답의 `hash`를 위 함수에 전달.
- `_heal_chunk` — 온라인 홀더에서 받은 데이터를 검증한 뒤에만 복사 소스로 사용.

### 서버: ReplicationService (app/services/replication_service.py)
- `register_chunk(..., chunk_hash: str | None)` — chunks.hash 저장.
  기존 행이 있고 hash가 NULL이면 채운다(백필).
- `list_chunks(...)` — 반환 dict에 `hash` 포함.

### 서버: 스키마/라우터
- `ChunkRegisterRequest`에 `hash: str | None = None`(선택, 하위 호환).
- `ChunkInfo`에 `hash: str | None = None`.

## Data Models

### 서버 스키마 변경

```sql
-- chunks 테이블에 내용 해시 컬럼 추가(기존 행은 NULL)
ALTER TABLE chunks ADD COLUMN hash TEXT;
```

`app/database.py`의 `SCHEMA_SQL` CREATE에 컬럼을 추가하고, 기존 DB를 위해 `_migrate`
에 멱등 `ADD COLUMN`(PRAGMA table_info 확인 후)을 넣는다. `device_sources.state`와
동일한 패턴이다.

### 해시 정의
- 대상: 청크 암호문 바이트(`chunker.split` 결과의 각 조각). 평문이 아니다.
- 알고리즘: `hashlib.sha256(data).hexdigest()` — 64자 hex.

### API 페이로드
등록 요청:
```json
{"chunk_id": "ab..", "file_ref": "cd..", "idx": 0, "size": 4194304, "hash": "ef.."}
```
목록 응답:
```json
[{"chunk_id": "ab..", "idx": 0, "size": 4194304, "hash": "ef.."}]
```

## Correctness Properties

Property 1: 해시 결정성 — 같은 바이트열은 항상 같은 chunk_hash를 낳고, 1비트라도
다르면 다른 값을 낳는다.
**Validates: Requirements 1.1**

Property 2: 복구 정합성 — 등록된 해시와 일치하는 청크만 결합에 사용되므로, 복구된
blob은 복제 시점의 암호문과 바이트 단위로 동일하다.
**Validates: Requirements 2.1, 2.2**

Property 3: 오염 격리 — 손상된 사본을 가진 홀더가 있어도, 유효한 사본을 가진 홀더가
하나라도 도달 가능하면 복구가 성공한다.
**Validates: Requirements 2.2, 3.2**

Property 4: 하위 호환 — chunk_hash가 없으면(레거시/구버전 서버) 검증을 생략하고
기존 동작과 동일한 결과를 낸다.
**Validates: Requirements 4.2**

## Error Handling

- 해시 불일치: 경고 로그(chunk_id, 홀더 device_id) 후 다음 홀더 시도. 조용히 성공
  처리하지 않는다.
- 모든 홀더 실패: 기존 `RecoveryError`에 누락 chunk_id를 담아 전파(동작 유지).
- heal에서 유효 소스 없음: 해당 청크를 `unrecoverable`로 보고(기존 경로 재사용).
- 서버가 hash 미반환: `None`으로 취급해 검증 생략(예외 없음).

## Testing Strategy

클라이언트:
- `chunk_hash` 결정성·차이 감지(PBT 가능).
- 홀더가 손상 바이트를 반환하면 다음 홀더로 넘어가 복구 성공.
- 모든 홀더가 손상이면 `RecoveryError`에 해당 chunk_id 포함.
- heal이 손상 소스를 배제하고 유효 소스만 복사, 유효 소스 없으면 unrecoverable.
- hash가 None이면 검증 생략(레거시 경로).

서버:
- 등록 시 hash 저장·목록 반환, hash 없이 등록 시 NULL 허용, 기존 행 백필,
  마이그레이션 멱등성.

## 마이그레이션

- 서버 기동 시 `_migrate`가 `chunks.hash`를 없으면 추가(기존 행 NULL).
- 기존 청크는 다음 `replicate` 때 해시가 채워진다(register가 NULL이면 갱신).
- 강제 백필은 하지 않는다. 홀더의 청크 바이트를 서버가 알 수 없으므로 소유자
  클라이언트만 계산할 수 있다.
