---
inclusion: manual
---

# 데몬 전송 위임 — Tasks

## Phase 1: UDP/릴레이 파일 op 인증 (보안 전제) — 완료
- [x] 1.2 dispatch_async: 파일 op는 payload 토큰을 검증해 user_id==로컬 user일 때만
      dispatch 위임(없음 401, 타 user 403). 복제본 op는 기존(same_user=False + ParityStore).
- [x] 1.3 RemoteSource: 직접 실패 시 _fallback에 auth_token 포함 payload(request_body)
      전달 → 릴레이/UDP 파일 op가 토큰을 운반.
- [x] 1.4 테스트: 파일 op 같은 user 200 / 타 user 403 / 토큰 없음 401, 미지원 op 400,
      복제본 op 유지.

## Phase 2: RemoteSource 홀펀칭 UDP 전송 — 완료
- [x] 2.1 RemoteSource.set_udp_transport + _fallback 캐스케이드(직접 TCP→UDP→릴레이).
      UDP 200=결과, 비-200=확정 오류(릴레이 안 함), 전송 예외=릴레이.
- [x] 2.2 데몬이 홀펀칭 시작 후 마운트된 RemoteSource에 holepunch.send_op 주입.
- [x] 2.3 테스트: UDP 성공→릴레이 안 함, UDP 비-200→오류, UDP 예외→릴레이.

## Phase 3: 데몬 전송 위임 채널
- [ ] 3.1 데몬 로컬 제어 서버(127.0.0.1 + 제어 파일 토큰): POST /ctl/put, /ctl/get.
- [ ] 3.2 put/get 구현(write_file 스필오버 udp / read_file 원격 udp + 메타 업로드).
- [ ] 3.3 GUI/CLI: 데몬 실행 중이면 위임, 아니면 직접 수행 fallback.
- [ ] 3.4 통합 테스트: 위임 put/get + fallback.

## 비범위
- 위임 채널을 통한 대용량 진행률 스트리밍(초기엔 동기 완료 응답).
