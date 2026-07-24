# Design Document

## Overview

서버 저장 모델을 "user당 전체 DB 암호문 blob 1개"에서 "user당 레코드 단위 암호문
집합"으로 확장한다. 클라이언트는 변경된 레코드만 업로드하고, `since` version 이후
변경분만 다운로드한다. 병합 로직(version 비교, 충돌, tombstone, 소유권 이전)은
기존 `_merge_record`를 재사용한다.

## Architecture

```
클라이언트 sync_client                        서버 /sync/metadata/records
  pending files ──직렬화(JSON)──▶ AES-GCM ──▶ PUT (base_version, records, purge_ids)
                                               │  CAS: UPDATE metadata_version
                                               │       WHERE current_version=base_version
                                               ▼
                                           metadata_records (record_id, blob, version)
  _merge_record ◀── AES-GCM 복호화 ◀── GET ?since=last_synced_version
```

- 서버는 암호문과 정수 version만 저장·전달한다(zero-knowledge 유지).
- CAS는 `metadata_version` 카운터 행의 조건부 UPDATE로 직렬화한다.
- 레코드 미지원(구버전 서버)은 404로 신호하고 클라이언트는 전체 blob 경로로 폴백한다.

## Components and Interfaces

### 서버: SyncService (app/services/sync_service.py)
- `download_records(user_id, since) -> tuple[int, list[dict]]`
  record_version > since 레코드와 current_version 반환.
- `upload_records(user_id, base_version, records, purge_ids) -> int`
  단일 트랜잭션 CAS 업서트+purge, 새 version 반환. 충돌 시
  `MetadataVersionConflictError`.
- `records_supported(user_id) -> bool`
  metadata_records/metadata_version이 비어 있고 기존 blob만 있으면 False(404 신호).

### 서버: 라우터 (app/routers/sync.py)
- `GET /sync/metadata/records?since={int}`
- `PUT /sync/metadata/records`
- 업로드 성공 시 `VersionNotifier.notify(user_id)`로 롱폴러를 깨운다.

### 클라이언트: SyncClient (stardustlib/sync_client.py)
- `_record_mode: bool` — 레코드 엔드포인트 사용 여부(404 시 False).
- `_download_records()` — 증분 다운로드 후 `_merge_record` 재사용.
- `_upload_records()` — pending 배치 업로드, 409 CAS 재시도.
- record_id 파생 헬퍼(신규 소함수).

## Data Models

### 서버 저장 스키마

기존 `metadata_backups`(전체 blob)는 하위 호환용으로 유지하고, 레코드 저장용
테이블을 추가한다.

```sql
CREATE TABLE IF NOT EXISTS metadata_records (
    user_id          TEXT    NOT NULL,
    record_id        TEXT    NOT NULL,   -- HMAC(virtual_path) hex, 불투명
    encrypted_record BLOB    NOT NULL,   -- AES-256-GCM(FileMetadata JSON)
    record_version   INTEGER NOT NULL,   -- 마지막 변경 시점의 글로벌 version
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_metadata_records_version
    ON metadata_records(user_id, record_version);

CREATE TABLE IF NOT EXISTS metadata_version (
    user_id         TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL DEFAULT 0
);
```

`current_version`은 기존 `metadata_backups.version`과 같은 의미의 단조 증가값이다.

### record_id 파생 (클라이언트)
- subkey = `HKDF(encryption_key, info="stardustfs-record-id")` (32바이트)
- record_id = `HMAC-SHA256(subkey, virtual_path.encode("utf-8")).hexdigest()`
- 결정적이므로 같은 경로는 항상 같은 record_id → upsert/삭제 가능.

### 레코드 페이로드
- 평문: `FileMetadata`를 JSON 직렬화(모든 필드, deleted 플래그 포함).
- 패딩: 암호화 전에 `[4바이트 길이(big-endian uint32)] + [JSON] + [0x00 패딩]`으로
  구성해 전체 길이를 256바이트 배수로 만든다. 복호화 후 길이 프리픽스로 JSON을
  잘라 복원한다. 이로써 암호문 크기가 256B 단위로 양자화되어 경로 길이 추정을 막는다.
- 암호화: 기존 blob 암호화와 동일한 AES-256-GCM 경로 재사용(nonce 레코드별 랜덤).

### API 페이로드
GET records 응답:
```json
{"current_version": 42,
 "records": [{"record_id": "ab..", "record_version": 41,
              "encrypted_record": "<base64>"}]}
```
PUT records 요청:
```json
{"base_version": 41,
 "records": [{"record_id": "ab..", "encrypted_record": "<base64>"}],
 "purge_ids": ["cd.."]}
```
base_version != current_version → 409 `{"current_version": N}`.
성공 → `{"version": N}`.

## Correctness Properties

Property 1: 단조성 — current_version은 성공한 업로드마다 정확히 1 증가한다.
**Validates: Requirements 1.3, 3.3**

Property 2: CAS 안전성 — 동시 업로드 중 최대 하나만 성공하고 나머지는 409를 받는다
(조건부 UPDATE 영향 행 0 → 충돌).
**Validates: Requirements 3.1, 3.2**

Property 3: 병합 등가성 — 증분으로 받은 레코드 집합에 `_merge_record`를 적용한 결과는
동일 레코드를 전체 blob으로 받아 병합한 결과와 같다.
**Validates: Requirements 5.1**

Property 4: 결정성 — 같은 (encryption_key, virtual_path)는 항상 같은 record_id를 낳는다.
**Validates: Requirements 2.1, 4.2**

Property 5: 패딩 왕복 — 패딩→암호화→복호화→언패딩을 거친 평문은 원본과 바이트 단위로
동일하고, 패딩 후 길이는 256의 배수이다.
**Validates: Requirements 8.1, 8.2, 8.3**

## Error Handling

- base_version 불일치 → 409, 클라이언트는 증분 다운로드·재병합 후 재시도
  (최대 `_MAX_CAS_RETRIES`).
- 레코드 엔드포인트 404 → 클라이언트 `_record_mode=False`, 전체 blob 경로 사용.
- 복호화 실패 → 기존 `KeyMismatchError` 경로 재사용(키 불일치 안내).
- 네트워크 오류 → 기존 오프라인 우선 정책(로컬 DB 유지) 유지.
- 부분 실패 없음: 업로드는 단일 트랜잭션(업서트+purge+카운터)으로 원자적.

## Testing Strategy

서버:
- records 업로드/다운로드 왕복, since 필터, CAS 충돌 409, purge 제거,
  빈 레코드+기존 blob 존재 시 404.

클라이언트:
- 증분 다운로드가 변경 레코드만 병합, 404 시 전체 blob fallback,
  업로드 409 재시도, purge_ids에 만료 tombstone 포함, record_id 결정성.

## 마이그레이션

- 신규 테이블은 기동 시 `CREATE TABLE IF NOT EXISTS`로 생성.
- 기존 blob만 있는 user: GET records는 `records_supported`가 False면 404를 반환해
  클라이언트가 전체 blob 경로로 초기화하도록 한다. 이후 클라이언트가 레코드 업로드를
  시작하면 레코드 모드로 승격된다.
