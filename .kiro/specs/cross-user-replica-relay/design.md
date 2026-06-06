---
inclusion: manual
---

# 교차 사용자 복제본 릴레이 — 설계

## 개요
복제본 op(replica_store/fetch/delete)에 한해 서버 릴레이 허브의 같은-user 제약을
완화하고, 홀더 측은 릴레이로 받은 복제본 op의 요청자를 payload 토큰으로 도출해
ParityStore 인가에 사용한다. 파일 데이터 op는 기존대로 같은-user만 릴레이한다.

## Components and Interfaces

### 서버 (../stardustfs-server)
- `app/routers/relay.py` submit_request:
  - 상수 `REPLICA_OPS = {"replica_store", "replica_fetch", "replica_delete"}`.
  - op가 REPLICA_OPS이면 대상 소유자=요청자 검사를 건너뛴다(대상 device 존재는 확인).
  - 그 외 op는 기존 owner==current_user(403) 유지.
  - poll/response 경로는 변경 없음(홀더는 자기 device inbox를 폴링 = 같은-user).

### 클라이언트 (stardustlib)
- `p2p_server.py`:
  - `_resolve_token_user(token) -> str | None`: `/auth/verify`로 검증해 valid면
    user_id 반환, 아니면 None(에러 Response 없이 단순 값).
  - `async dispatch_async(op, payload) -> (status, result)`: 릴레이 워커용.
    - op가 복제본 op이면 payload["auth_token"]을 _resolve_token_user로 검증해 요청자
      도출(없거나 무효면 401). 그 요청자로 _op_replica_* 호출.
    - 그 외 op이면 기존 동기 `dispatch(op, payload)`에 위임.
  - 동기 `dispatch`는 기존 시그니처·동작 유지(파일 op 테스트 보존).
- `relay_worker.py`: `status, result = await self._p2p_server.dispatch_async(op, payload)`.
- `replication_manager.py`: `_holder_store`/`_holder_fetch`의 릴레이 fallback에서
  `_relay_op(device_id, op, {**body, "auth_token": token})`로 소유자 토큰을 포함.

## Data Models
변경 없음(서버 device_sources/replication 스키마 불변). 릴레이 payload는 기존 복제본
op payload + auth_token(이미 직접 경로가 사용하던 필드).

## Correctness Properties

### Property 1: 복제본 op 화이트리스트
*임의의* 릴레이 요청에 대해, 대상 소유자≠요청자인 교차 사용자 릴레이는 op가 복제본
op(store/fetch/delete)일 때만 허용되고, 그 외 op는 거부(403)된다.

### Property 2: 요청자=소유자 인가
*임의의* 릴레이된 복제본 op에 대해, 요청자는 payload 토큰의 user_id로 결정되며,
ParityStore는 그 요청자와 chunk_id 소유자가 일치할 때만 store/fetch/delete를 허용한다.

### Property 3: 동기 dispatch 불변
*임의의* 파일 op(read/write/exists 등)에 대해, dispatch_async는 동기 dispatch에
위임하므로 기존 동작과 동일하다.

## Error Handling
- auth_token 없음/무효: 401 (dispatch_async).
- ParityStore 인가 실패: 403(소유자 불일치), 404(없음), 507(쿼터). 기존과 동일.
- 대상 device 미존재(서버): DeviceNotFoundError(404).
- /auth/verify 서버 도달 불가: _resolve_token_user None → 401.

## Testing Strategy
- 서버: 복제본 op는 타 사용자 device로 릴레이 큐잉 성공, 비복제 op는 타 사용자 403,
  대상 미존재 404(test_relay.py 확장).
- 클라이언트: dispatch_async가 복제본 op에서 payload 토큰으로 요청자를 도출하고
  ParityStore 인가에 사용(요청자=소유자 200, 불일치 403, 토큰 없음 401); 파일 op는
  동기 dispatch에 위임(read 200). _holder_store/_holder_fetch 릴레이 fallback이 payload에
  auth_token을 포함하는지(_relay_op 모킹으로 확인).
