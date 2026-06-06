---
inclusion: manual
---

# 교차 사용자 복제본 릴레이 — Tasks

## Phase 1: 서버 릴레이 허용
- [x] 1.1 relay.py: REPLICA_OPS 상수 + submit_request에서 복제본 op는 소유자 검사 생략
      (대상 device 존재만 확인), 그 외는 owner==current_user 유지.
- [x] 1.2 서버 테스트: 복제본 op 타 사용자 릴레이 큐잉 성공, 비복제 op 타 사용자 403,
      대상 미존재 404.

## Phase 2: 클라이언트 홀더 요청자 도출
- [x] 2.1 p2p_server._resolve_token_user(token) + async dispatch_async(op, payload):
      복제본 op는 payload 토큰으로 요청자 도출(없음/무효 401), 그 외는 동기 dispatch 위임.
- [x] 2.2 relay_worker: await dispatch_async 사용.
- [x] 2.3 클라이언트 테스트: dispatch_async 복제본 요청자 도출(소유자=요청자 200,
      불일치 403, 토큰 없음 401), 파일 op 위임(미지원 op 400).

## Phase 3: 릴레이 payload 토큰 포함
- [x] 3.1 replication_manager _holder_store/_holder_fetch 릴레이 fallback이 payload에
      auth_token 포함.
- [x] 3.2 클라이언트 테스트: 릴레이 fallback payload에 auth_token 존재(_relay_op 모킹).

## Phase 4: 검증/문서
- [x] 4.1 양쪽 회귀 테스트 그린(클라이언트 513/1 skip, 서버 95).
- [x] 4.2 replication-parity tasks.md의 이관 항목(4.3·6.78)에 본 스펙으로 해소 표기,
      ROADMAP/ARCHITECTURE의 "교차 사용자 릴레이 별도 스펙" 문구 갱신.

## 비범위
- 릴레이 허브 다중 워커 확장(외부 큐). 데이터 채널 UDP 전환.
