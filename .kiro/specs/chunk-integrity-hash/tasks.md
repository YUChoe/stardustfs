# Implementation Plan: 청크 무결성 해시

## Overview

청크 암호문의 `SHA-256`을 등록·조회에 추가하고, 복구·재복제에서 검증해 손상된 홀더를
배제한다. 해시가 없으면(레거시/구버전 서버) 검증을 생략해 하위 호환을 유지한다.

## Tasks

- [x] 1. 서버 스키마: `chunks.hash TEXT` 추가. `app/database.py` SCHEMA_SQL CREATE에
      컬럼 추가 + `_migrate`에 멱등 ADD COLUMN(PRAGMA table_info 확인). `data/DATABASE_SCHEMA.md`
      갱신.
      - 요구사항: 1.2, 4.1
- [x] 2. 서버 스키마 모델: `ChunkRegisterRequest.hash`, `ChunkInfo.hash`를 선택 필드로
      추가(기본 None, 하위 호환).
      - 요구사항: 1.1, 1.3, 4.2
- [x] 3. 서버 서비스: `register_chunk(..., chunk_hash)`가 hash를 저장하고 기존 행의
      hash가 NULL이면 채운다. `list_chunks`가 응답에 hash를 포함한다.
      - 요구사항: 1.2, 1.3, 1.4, 4.3
- [x] 4. 서버 테스트: hash 저장·조회 왕복, hash 없이 등록 시 NULL, 기존 행 백필,
      마이그레이션 멱등성.
      - 요구사항: 1.2, 1.3, 4.1, 4.3
- [x] 5. 클라이언트 `chunker.chunk_hash(data) -> str` 추가 + 결정성/차이 감지 테스트.
      - 요구사항: 1.1
- [x] 6. 클라이언트 복제 경로: `_replicate_chunks`가 청크별 해시를 계산해
      `_register_chunk`로 전달(서버 payload에 hash 포함).
      - 요구사항: 1.1
- [x] 7. 클라이언트 복구 경로: `_fetch_from_any_holder(..., expected_hash)`가 받은
      데이터를 검증하고 불일치 시 다음 홀더 시도. `_recover_chunks`가 목록의 hash를
      전달. hash None이면 검증 생략.
      - 요구사항: 2.1, 2.2, 2.4, 4.2
- [x] 8. 클라이언트 재복제 경로: `_heal_chunk`가 복사 소스로 쓸 데이터를 검증하고,
      실패 시 다음 온라인 홀더 시도. 유효 소스 없으면 unrecoverable 보고.
      - 요구사항: 3.1, 3.2, 3.3
- [x] 9. 클라이언트 테스트: 손상 홀더 배제 후 복구 성공, 전 홀더 손상 시
      RecoveryError에 chunk_id 포함, heal 오염 전파 차단, hash None 레거시 경로.
      - 요구사항: 2.2, 2.3, 3.2, 3.3, 4.2
- [x] 10. 서버·클라이언트 전체 테스트 통과 확인.
      - 요구사항: 5.2, 5.3
- [x] 11. 문서 갱신: `docs/TRANSPORT.md` 또는 `docs/DISTRIBUTION_POLICY.md`에 청크
      무결성 검증 동작 반영.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": [1, 5]},
    {"wave": 2, "tasks": [2, 6]},
    {"wave": 3, "tasks": [3, 7, 8]},
    {"wave": 4, "tasks": [4, 9]},
    {"wave": 5, "tasks": [10]},
    {"wave": 6, "tasks": [11]}
  ]
}
```

## Notes

- 서버(1-4)와 클라이언트 해시 함수(5-6)는 병행 가능하다.
- 클라이언트 검증(7-8)은 서버가 hash를 반환(3)해야 실효가 있으나, hash None 경로가
  있어 독립적으로 구현·테스트할 수 있다.
- `chunk_id` 정의(위치 식별자)는 바꾸지 않는다. 내용 해시는 별도 필드다.
- 강제 백필은 없다. 기존 청크는 다음 복제 때 해시가 채워진다.
