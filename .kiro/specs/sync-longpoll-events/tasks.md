# Implementation Plan: 메타데이터 버전 롱폴링 이벤트

## Overview

서버 version 변경을 롱폴링으로 즉시 통지하여 동기화 지연을 제거한다. 메모리 기반
알림(단일 워커), zero-knowledge 유지, 기존 주기 폴링은 안전망으로 유지.

## Tasks

- [x] 1. 서버 VersionNotifier + wait 엔드포인트
  - [x] 1.1 VersionNotifier 구현
    - app/services/version_notifier.py: user_id별 asyncio.Event, notify/wait
    - app.state에 단일 인스턴스
    - _Requirements: 2.1, 2.3_
  - [x] 1.2 SyncService.get_current_version
    - metadata_backups MAX(version), 없으면 0
    - _Requirements: 1.6_
  - [x] 1.3 GET /sync/metadata/wait 엔드포인트
    - known_version 쿼리, version > known이면 즉시 반환, 아니면 대기(TICK으로
      재확인), 타임아웃 시 changed=false
    - 인증, 자기 user_id만
    - _Requirements: 1.1-1.6, 4.2_
  - [x] 1.4 upload_metadata 라우터에서 notify 호출
    - version 증가 성공 시 notifier.notify(user_id), CAS 충돌(409)이면 미호출
    - _Requirements: 2.1, 2.2_
  - [x]* 1.5 서버 롱폴링 단위 테스트
    - version>known 즉시 반환, notify로 대기자 깨움, 타임아웃 changed=false
    - _Requirements: 1.1-1.4, 2.1, 2.2_

- [x] 2. 클라이언트 버전 대기 루프
  - [x] 2.1 SyncClient._wait_for_version
    - GET /sync/metadata/wait?known_version=N → (changed, version)
    - 404면 WaitUnsupported(루프 비활성화 신호)
    - _Requirements: 3.2, 4.1_
  - [x] 2.2 SyncClient._version_wait_loop + 시작/종료 통합
    - start_periodic_sync에서 task 시작, stop에서 취소
    - changed면 _download_and_merge + orphan GC, known_version 갱신
    - 네트워크 오류 재시도, 404면 비활성화
    - _Requirements: 3.1-3.5, 4.1, 4.3_
  - [x]* 2.3 클라이언트 버전 대기 단위 테스트
    - changed 시 다운로드 트리거, 404 시 비활성화, 오류 시 재시도
    - _Requirements: 3.2, 3.3, 4.1_

- [x] 3. 통합 검증
  - [x] 3.1 롱폴링 즉시 동기화 E2E (로컬 서버)
    - PC-A 업로드 → PC-B wait 즉시 깨어남 → 다운로드 반영(폴링 대기 없이)
    - _Requirements: 1.1-1.4, 2.1, 3.1, 3.2_

- [x] 4. Final Checkpoint
  - 클라이언트/서버 전체 테스트 통과 + 회귀 없음.

## Notes

- 단일 uvicorn 워커 가정(메모리 알림). 다중 워커는 외부 pub/sub 필요(범위 밖).
- 이벤트는 신호, 정확성은 version 비교로 보장. 내부 TICK으로 알림 누락 보정.
- 기존 주기 폴링 유지(안전망). 404 시 자동 비활성화(하위 호환).
- 전송량 최적화(파셜)는 별도 범위.
- `*` 표시는 선택적 태스크.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "1.4", "2.1"] },
    { "id": 2, "tasks": ["1.5", "2.2"] },
    { "id": 3, "tasks": ["2.3", "3.1"] },
    { "id": 4, "tasks": ["4"] }
  ]
}
```
