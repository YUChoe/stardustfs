# Implementation Plan: 파셜/증분 메타데이터 동기화

## Overview

서버에 레코드 단위 저장/증분 API를 추가하고, 클라이언트 동기화를 증분으로 전환한다.
병합 로직은 기존 `_merge_record`를 재사용하며, 구버전 서버에는 전체 blob 경로로
폴백한다.

## Tasks

- [x] 1. 서버 DB 스키마 추가: `metadata_records`, `metadata_version`
      (기동 시 CREATE TABLE IF NOT EXISTS). `data/DATABASE_SCHEMA.md` 갱신.
      - 요구사항: 1
- [x] 2. SyncService 레코드 메서드 추가
      - `download_records(user_id, since)`
      - `upload_records(user_id, base_version, records, purge_ids)` (단일 트랜잭션 CAS)
      - `records_supported(user_id)` (빈 레코드+기존 blob → False)
      - 요구사항: 1, 2, 3, 4
- [x] 3. 라우터 추가: `GET /sync/metadata/records`, `PUT /sync/metadata/records`.
      업로드 성공 시 VersionNotifier.notify. 미지원 시 404.
      - 요구사항: 2, 3, 6, 7
- [x] 4. 서버 테스트: 왕복/since 필터/CAS 409/purge/404 폴백.
      - 요구사항: 1, 2, 3, 4, 7
- [x] 5. 클라이언트 record_id 파생 헬퍼(HKDF subkey + HMAC) + 결정성 테스트.
      - 요구사항: 5
- [x] 6. 클라이언트 레코드 직렬화/역직렬화(FileMetadata ↔ JSON ↔ 256B 패딩 ↔ AES-GCM).
      패딩 왕복 단위 테스트.
      - 요구사항: 1, 5, 8
- [x] 7. SyncClient 증분 다운로드 `_download_records(since)` → `_merge_record`
      재사용. 404 시 `_record_mode=False` 폴백.
      - 요구사항: 2, 5, 7
- [x] 8. SyncClient 증분 업로드 `_upload_records`: pending 배치 + purge_ids,
      base_version CAS, 409 재시도 루프.
      - 요구사항: 3, 4
- [x] 9. 롱폴 통지 시 증분 다운로드 경로 사용하도록 연결.
      - 요구사항: 6
- [x] 10. 클라이언트 테스트: 증분 병합/404 폴백/409 재시도/purge/record_id.
      - 요구사항: 2, 3, 4, 5, 7
- [x] 11. 서버 106 passed, 클라이언트 619 passed/1 skip.
      - 요구사항: 8
- [x] 12. ROADMAP.md 상태 갱신.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": [1, 5]},
    {"wave": 2, "tasks": [2, 6]},
    {"wave": 3, "tasks": [3, 7, 8]},
    {"wave": 4, "tasks": [4, 9, 10]},
    {"wave": 5, "tasks": [11]},
    {"wave": 6, "tasks": [12]}
  ]
}
```

## Notes

- 서버 변경(1-4)과 클라이언트 record_id/직렬화(5-6)는 병행 가능.
- 클라이언트 통합(7-9)은 서버 API(3)와 직렬화(6)가 선행되어야 한다.
- 각 태스크는 해당 테스트가 통과해야 완료로 간주한다.
- 태스크 9(롱폴 연동)는 롱폴 통지 후 `_download_and_merge`가 호출되는데, 이 함수가
  레코드 모드에서 `_download_records`를 우선 사용하므로 별도 배선 없이 충족된다.
