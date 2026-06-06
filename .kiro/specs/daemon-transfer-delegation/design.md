---
inclusion: manual
---

# 데몬 전송 위임 — 설계

## 개요
데몬이 항상-온라인 + 홀펀칭 + 릴레이 정책을 보유하므로 전송을 데몬이 수행한다.
선행으로 UDP/릴레이 파일 op 인증(보안)과 RemoteSource 홀펀칭 UDP 전송을 갖춘 뒤,
데몬 로컬 제어 채널로 put/get을 위임한다.

## Phase 1: UDP/릴레이 파일 op 인증 (보안 전제)
- p2p_server.dispatch_async: 복제본 op는 기존(payload 토큰 same_user=False). 그 외
  파일 op는 payload의 auth_token을 _resolve_token_user로 검증하되 user_id 일치까지
  요구(same_user=True): 토큰의 user_id == 로컬 user_id. 불일치/없음 → 401/403.
  검증 후 기존 동기 dispatch에 위임.
- _resolve_token_user에 same_user 옵션 추가(기본 False) → 파일 op는 True로 호출.
- 클라이언트: RemoteSource 릴레이/UDP 파일 op payload에 auth_token 포함
  (_relay_fallback이 auth_token 포함하도록, UDP send도 포함).

## Phase 2: RemoteSource 홀펀칭 UDP 전송
- RemoteSource.set_udp_transport(fn): async (device_id, op, payload) -> (status, result).
- _do_p2p_request: 직접 TCP 실패(타임아웃/연결) 시 _relay_fallback 전에 udp 시도.
  _ENDPOINT_OP로 endpoint→op 매핑. 성공 시 result 반환, 실패 시 릴레이.
- 데몬이 _mount_remote_sources 후 각 RemoteSource에 holepunch.send_op 주입.

## Phase 3: 데몬 전송 위임 채널
- 데몬 로컬 제어: aiohttp 127.0.0.1:0(임의 포트) + 제어 파일
  {metadata_db}.daemon.ctl.json {port, token}(소유자 전용). 라우트: POST /ctl/put,
  /ctl/get (token 헤더 검증).
- put: 로컬 파일 읽기 → jbod.write_file(스필오버는 holepunch udp) → sync upload.
- get: jbod.read_file(원격은 holepunch udp) → 로컬 저장.
- GUI/CLI: 데몬 status running이면 제어 채널로 위임, 아니면 기존 직접 수행.

## Correctness Properties

### Property 1: 비-게이트 경로 인증
*임의의* 파일 op가 서버 게이트(릴레이 owner 검사) 없이 UDP로 도달하면, auth_token의
user_id가 홀더 user_id와 일치할 때만 처리된다(임의 피어의 read/write 차단).

### Property 2: 전송 우선순위 보존
*임의의* RemoteSource op에 대해 직접 TCP → 직접 UDP(홀펀칭) → 릴레이(정책) 순.

### Property 3: 위임 일관성
*임의의* put/get에 대해, 데몬 실행 중이면 데몬이 수행하고 미실행이면 세션이 직접
수행하되, 두 경로의 결과(저장 위치/복호화 내용)는 동일하다.

## Error Handling
- UDP/릴레이 파일 op 토큰 검증 실패: 401(없음/무효)/403(타 사용자).
- 데몬 제어 채널 미가용: GUI/CLI가 직접 수행으로 fallback.
- 스필오버 전송 전 실패: InsufficientStorageError(무손실).

## Testing Strategy
- Phase 1: dispatch_async 파일 op 토큰 검증(같은 user 200, 타 user 403, 없음 401),
  복제본 op 기존 유지. RemoteSource 릴레이/UDP payload에 auth_token 포함.
- Phase 2: RemoteSource 캐스케이드(직접→udp→relay) fake 단위.
- Phase 3: 데몬 제어 put/get 위임 + fallback(로컬 통합 테스트).

## 단계
Phase 1(보안) → Phase 2(RemoteSource UDP) → Phase 3(위임 채널 + GUI/CLI 클라이언트).
