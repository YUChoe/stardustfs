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

## Phase 3: 데몬 전송 위임 채널 — 완료
- [x] 3.1 daemon_control.DaemonControlServer: 127.0.0.1 임의 포트 aiohttp + 제어 파일
      {port, token}(소유자 전용 0o600). POST /ctl/put, /ctl/get (X-Ctl-Token 인증).
- [x] 3.2 put: 로컬 파일 읽기→jbod.write_file(스필오버 udp)→sync.upload_metadata.
      get: jbod.read_file(원격 udp)→로컬 저장. to_thread로 이벤트 루프 비블로킹.
- [x] 3.3 daemon_control.transfer_via_daemon + actions._delegate: 데몬 실행 중이면 위임,
      제어 파일 없으면 None→직접 수행 fallback. put_file/get_file 적용.
- [x] 3.4 stardustfs.py가 제어 서버 기동(_cleanup에서 정지). 테스트: put/get 왕복,
      fallback(데몬 없음→None), 토큰 불일치 403, 없는 파일 500.

## 비범위
- 위임 채널을 통한 대용량 진행률 스트리밍(초기엔 동기 완료 응답).
