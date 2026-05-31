# Implementation Plan: MVP5 파일 공유 데모 (최소 슬라이스)

> ⚠️ 방향 결정(2026-05): 평문 사용자 간 공유로서의 MVP5는 폐기됨(상세는
> requirements.md / ROADMAP.md). 아래 태스크는 PoC로 완료된 상태이며,
> share_token 인가 인프라는 MVP3 암호화 리플리케이션으로 승계된다.

## Overview

기존 MVP2 P2P 인프라 위에 교차 사용자 읽기 전용 공유 레이어를 최소한으로 추가한다. 서버에 shares 테이블 + 3개 엔드포인트, 클라이언트 P2PServer에 share_token 인가 경로를 추가하고, 사용자 A 발급 → 사용자 B 읽기 흐름을 통합 테스트로 검증한다.

## Tasks

- [x] 1. 중앙 서버: 공유 토큰 저장소 및 서비스
  - [x] 1.1 shares 테이블 스키마 추가
    - app/database.py SCHEMA_SQL에 shares 테이블 + 인덱스 추가
    - token(PK), owner_user_id(FK), device_id, physical_path, created_at, expires_at
    - data/DATABASE_SCHEMA.md 갱신
    - _Requirements: 1.3_
  - [x] 1.2 ShareService 구현 (app/services/share_service.py)
    - create_share: device_id 소유권 검증 후 secrets.token_urlsafe(32) 토큰 생성·저장
    - get_share: 토큰 조회, 만료 판정 (404/410)
    - verify_share: 토큰 유효성 + physical_path 일치 검증
    - ShareExpiredError(410) 예외 추가 (app/exceptions.py)
    - _Requirements: 1.1, 1.2, 1.3, 2.2, 2.3, 3.2, 3.3_

- [x] 2. 중앙 서버: 공유 라우터 및 라우팅 확장
  - [x] 2.1 /shares 라우터 구현 (app/routers/shares.py)
    - POST /shares (발급, 인증), GET /shares/{token} (조회, 인증), POST /shares/{token}/verify (P2P 위임 검증)
    - ShareCreateRequest/Response, ShareInfoResponse, ShareVerifyRequest/Response 스키마 추가
    - main.py에 라우터 등록
    - _Requirements: 1.1, 1.4, 2.1, 2.2, 2.3, 3.2_
  - [x] 2.2 GET /routing/{device_id}에 share_token 우회 경로 추가
    - X-Share-Token 헤더가 유효하고 토큰의 device_id와 경로 파라미터 일치 시 소유권 검증 우회
    - _Requirements: 2.4_
  - [x]* 2.3 서버 단위 테스트
    - 발급 성공/소유권403/만료범위422, 조회 404/410, verify 경로일치/불일치, 만료 단조성
    - _Requirements: 1.1-1.4, 2.1-2.3, 3.2, 3.3_

- [x] 3. 클라이언트: P2PServer share_token 인가
  - [x] 3.1 _parse_and_verify에 share_token 경로 추가
    - 요청 body에 share_token 있으면 user_id 일치 검증 우회
    - 중앙 서버 POST /shares/{token}/verify로 physical_path 일치 검증
    - 무효/만료 401, 경로 불일치 403, path traversal 400(기존 유지)
    - read 핸들러에만 적용 (write/delete는 share_token 미허용)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_
  - [x]* 3.2 P2PServer share_token 단위 테스트 (통합 테스트로 커버)
    - 유효 토큰 read 허용, 경로 불일치 403, 만료 401, traversal 400, 없는 토큰 401
    - test_share_demo_integration.py에서 실제 P2PServer로 검증
    - _Requirements: 3.1-3.6_

- [x] 4. 데모 통합 테스트 및 Property 검증
  - [x] 4.1 사용자 A→B 공유 E2E 통합 테스트
    - 별도 테스트 계정 2개(A, B), A 발급 → B가 share_token으로 /p2p/read 성공
    - 만료 후 거부, 토큰에 안 묶인 경로 403 거부
    - 실제 P2PServer + 중앙 서버(로컬 또는 mock)로 HTTP 왕복
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x]* 4.2 Property 1, 2 PBT
    - **Property 1: 공유 토큰 경로 격리** — 묶인 경로만 허용, 그 외 모두 거부
    - **Property 2: 만료 단조성** — expires_at 전후 valid 전이, 한번 만료되면 회복 없음
    - _Requirements: 3.3, 2.3, 3.4_

- [x] 5. Final Checkpoint
  - 서버/클라이언트 전체 테스트 통과 확인, 데모 흐름 동작 확인.

## Notes

- `*` 표시 태스크는 선택적
- 읽기 전용 데모 슬라이스: write/delete 공유, 그룹 공유, TURN/STUN, 과금, 디렉토리 공유는 범위 밖
- 별도 테스트 계정 사용으로 실제 사용자 계정 오염 금지
- 공유되는 바이트는 소유자 키로 암호화된 상태 — 평문 복호화(키 공유)는 본 슬라이스 범위 밖

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1", "2.2"] },
    { "id": 3, "tasks": ["2.3", "3.1"] },
    { "id": 4, "tasks": ["3.2", "4.1"] },
    { "id": 5, "tasks": ["4.2"] },
    { "id": 6, "tasks": ["5"] }
  ]
}
```
