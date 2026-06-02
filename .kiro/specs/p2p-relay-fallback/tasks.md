# Implementation Plan: P2P 릴레이 Fallback (long-polling)

## Overview

직접 P2P 연결이 불가능한 이중 NAT/CGNAT 환경을 위해 중앙 서버 경유 long-polling
릴레이를 추가한다. 서버는 암호문 payload를 불투명 중계만 한다. 클라이언트는 직접
연결 실패 시 릴레이로 fallback한다.

## Tasks

- [x] 1. 서버 RelayHub + 엔드포인트
  - [x] 1.1 RelayHub (메모리 큐/Future) 구현
    - app/services/relay_hub.py: submit/poll/deliver/wait_response
    - device별 asyncio.Queue inbox, request_id별 asyncio.Future pending
    - app.state에 단일 인스턴스 보관
    - _Requirements: 1.1, 1.2, 2.1, 2.3, 3.3_
  - [x] 1.2 relay 라우터 4개 엔드포인트
    - POST /relay/request, GET /relay/response/{id}, GET /relay/poll,
      POST /relay/response/{id}
    - 인증(get_current_user), 인가(user_id 일치/디바이스 소유 검증)
    - 타임아웃: poll 25s(204), response 30s(504)
    - app/schemas.py에 Relay 모델 추가, main.py에 라우터 등록
    - _Requirements: 1.1-1.5, 2.1-2.5, 3.1, 3.2, 3.4_
  - [x] 1.3 서버 릴레이 단위 테스트
    - 왕복 중계, user_id 불일치 403, 디바이스 없음 404, 타임아웃 504/204
    - _Requirements: 1.1-1.5, 2.1-2.5_

- [x] 2. P2PServer 핸들러 로직 분리 (dispatch)
  - [x] 2.1 handle_* → _op_*(payload) -> (status, result) 추출
    - read/exists/list/write/delete/mkdir/rmdir/space
    - handle_*는 인증 후 _op_* 호출해 web.Response로 감싸기 (기존 동작 보존)
    - dispatch(op, payload) -> (status, result) 추가
    - _Requirements: 5.2_
  - [x] 2.2 dispatch 단위 테스트 (기존 핸들러 회귀 없음 확인)
    - _Requirements: 5.2_

- [x] 3. 클라이언트 RelayClient + Fallback
  - [x] 3.1 RelayClient 구현
    - stardustlib/relay_client.py: request(op, payload) -> result
    - POST /relay/request → request_id, GET /relay/response/{id} long-poll
    - status!=200이면 OSError, _EventLoopThread 공유로 동기 인터페이스
    - _Requirements: 4.2, 4.4_
  - [x] 3.2 RemoteSource 직접연결 → 릴레이 fallback
    - _do_p2p_request에서 ConnectError/Timeout 시 RelayClient로 전환
    - endpoint→op 매핑, result는 직접연결 body와 동일 형식
    - 릴레이도 실패 시 OSError
    - _Requirements: 4.1-4.5_
  - [x] 3.3 RemoteSource fallback 단위 테스트
    - 직접 성공 시 릴레이 미사용, 직접 실패 시 릴레이 사용, 둘 다 실패 OSError
    - _Requirements: 4.1-4.5_

- [x] 4. 대상 측 RelayWorker
  - [x] 4.1 RelayWorker 구현
    - stardustlib/relay_worker.py: start/stop, _loop(poll→dispatch→response)
    - P2PServer.dispatch 재사용, 폴링 실패 재시도
    - _Requirements: 5.1-5.5_
  - [x] 4.2 stardustfs.py 통합
    - P2P 활성 시 RelayWorker 시작, 종료 시 정리
    - _Requirements: 5.1, 5.4_

- [x] 5. 통합 검증
  - [x] 5.1 릴레이 왕복 E2E (mock 중앙 서버 또는 로컬 서버)
    - 직접연결 차단 상황에서 릴레이로 read 성공, 바이트 동일성
    - 오프라인 대상 시 규격 오류
    - _Requirements: 1.1-2.5, 4.1-4.5, 5.1-5.3_
  - [ ]* 5.2 Property 테스트 (왕복 동일성/인가/불투명성)
    - _Requirements: 3.1-3.4_

- [x] 6. Final Checkpoint
  - 클라이언트/서버 전체 테스트 통과 + 회귀 없음.

## Notes

- 단일 uvicorn 워커 가정(메모리 큐). 다중 워커는 범위 밖.
- 직접연결 우선, 릴레이는 fallback.
- 서버는 payload/result 불투명 중계, 영속화 안 함.
- 같은 user_id 디바이스 간만 허용.
- `*` 표시는 선택적 태스크.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.1"] },
    { "id": 2, "tasks": ["1.3", "3.2", "4.1"] },
    { "id": 3, "tasks": ["3.3", "4.2"] },
    { "id": 4, "tasks": ["5.1", "5.2"] },
    { "id": 5, "tasks": ["6"] }
  ]
}
```
