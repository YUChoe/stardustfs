---
inclusion: manual
---

# 토큰 기반 디바이스 인증 전환 — Tasks

## Phase A: 자격증명 저장소
- [ ] A1. `stardustlib/credential_store.py` 작성: CredentialStore(load/save/clear/
      exists/path), tmp+replace 원자적 기록, 소유자 전용 권한(POSIX 0600 / Windows
      icacls best-effort). (Property 3·4)
- [ ] A2. 파일 락 헬퍼(`{credentials}.lock`, O_CREAT|O_EXCL 스핀+타임아웃).
- [ ] A3. 단위 테스트: 라운드트립, 원자성(tmp 잔존 없음), 권한, clear, 손상 처리.

## Phase B: AuthClient 저장소 연동
- [ ] B1. AuthClient에 credential_store 주입 + load_from_store(). 하위호환(None이면
      메모리 전용).
- [ ] B2. login() 성공 시 저장소 save. refresh 성공 시 회전 토큰 save.
- [ ] B3. `_refresh_with_lock()`: 락 → 저장소 재로딩 → 필요 시에만 refresh → save.
      (Property 2)
- [ ] B4. refresh 401 시 저장소 토큰 무효화 + AuthenticationError.
- [ ] B5. 단위 테스트: 저장소 기록/로딩/회전, 동시 갱신 1회 호출(mock 카운트).

## Phase C: login / logout 명령
- [ ] C1. `login` 명령: email/password 수집(flag > env > getpass 대화형) → login →
      저장소 기록. key_password 동일 방식 수집·저장(선택).
- [ ] C2. `logout` 명령: 저장소 refresh_token으로 `POST /auth/logout` 호출(서버 취소)
      → CredentialStore.clear(). 서버 실패 시 경고 후 로컬 삭제는 수행.
- [ ] C3. dispatcher 등록(login/logout은 자체 처리; login은 토큰 없이도 동작).
- [ ] C4. 비밀번호 로그 미출력 확인(Property 1).

## Phase D: 기존 경로 전환 (.env 제거)
- [ ] D1. `CLISession.open_online()`에서 STARDUST_EMAIL/PASSWORD 제거 → 저장소 토큰
      로딩. 토큰 없으면 온라인 명령 "login 필요" 규격 에러.
- [ ] D2. `stardustfs.py startup_v2` 및 `online_recovery.py`의 env 로그인 → 저장소
      토큰. 없으면 오프라인 강등(Requirement 9).
- [ ] D3. key 백업/복원(`_backup_key_to_server`/`_restore_key_from_server`)의
      STARDUST_KEY_PASSWORD → 저장소 key_password 우선, 없으면 대화형.
- [ ] D4. 마이그레이션 부트스트랩: 저장소 없음 + .env 자격증명 시 login이 일회성으로
      사용(Requirement 8).

## Phase S: 서버 로그아웃 엔드포인트 (`../stardustfs-server`)
- [ ] S1. `TokenService.revoke_refresh_token(user_id, refresh_token)`: `_hash_token`
      해시로 `refresh_tokens`에서 본인 소유 행을 is_revoked=1 갱신(멱등).
- [ ] S2. `POST /auth/logout` 라우트: access_token 인증 + 본문 refresh_token →
      revoke. 항상 200(멱등, 정보 노출 방지). 스키마 변경 없음.
- [ ] S3. 서버 테스트: 취소 후 해당 refresh로 /auth/refresh 401, 멱등성, 타 사용자
      토큰 취소 거부. 서버 pytest 그린 유지.
- [ ] S4. 배포 주의: 미배포 서버에서는 클라이언트 logout이 404를 받으므로 로컬
      삭제로 폴백(하위호환).

## Phase E: 검증·문서
- [ ] E1. 마이그레이션 테스트: .env 부트스트랩 → 저장소 생성, master.key/metadata
      미변경 단언(Property 5).
- [ ] E2. E2E(로컬 서버): login → devices/put/get(.env 미사용) → 만료 후 자동 갱신 →
      logout → "login 필요".
- [ ] E3. 양쪽 pytest 회귀 그린 유지.
- [ ] E4. 문서 갱신: HANDOVER/CONFIGURATION/run-dev, `.env`에서 EMAIL/PASSWORD/
      KEY_PASSWORD 제거 안내, login/logout 사용법.

## 비범위 (이번 전환 제외)
- 서버 device-bound 토큰 신설(디바이스별 발급/취소). 향후 필요 시 refresh 행에
  device_id 컬럼 추가로 확장(하이브리드 옵션).
- GUI 로그인 화면(코어는 동일 CredentialStore/AuthClient 재사용).
- OS 키체인 저장(현재는 권한 제한 평문 파일).
