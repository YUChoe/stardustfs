# MVP10 · MVP11 개발 리뷰

dev-mvp10, dev-mvp11 두 단계의 개발 내용을 정리한 문서다. 로드맵 맥락에서
무엇을 왜 했는지, 어떤 스펙·커밋으로 구현됐는지 추적할 수 있게 한다.

작성 기준: 2026-06-07. 관련 문서: [ROADMAP.md](./ROADMAP.md),
[HANDOVER.md](./HANDOVER.md), [ARCHITECTURE.md](./ARCHITECTURE.md),
[TRANSPORT.md](./TRANSPORT.md).

## 1. 로드맵 맥락 요약

StardustFS의 핵심 취지는 "여러 디바이스의 스토리지를 원격으로 확장하여 하나의
가상 파일서버로 묶는 것"이다. 2026-06 개정으로 접근 계층(access layer)을
WebDAV 실시간 마운트에서 FTP 유사 CLI 업로드/다운로드로 전환했다. 저장·동기화·
전송 엔진은 유지하고 그 위의 접근 방식만 바꾸는 변경이다.

```
[완료된 기반]                     [접근 계층 피벗]        [엔진 구현]        [보류]
MVP1 로컬 암호화 저장      →  MVP10 CLI 가상 파일서버 →  MVP3 리플리케이션 →  서비스 플랫폼
MVP2 멀티디바이스 동기화         + GUI 파일탐색기                            (MVP4 등급/과금)
   + 원격 소스 / P2P / 릴레이
```

불변 원칙(유지): 파일은 각 디바이스 sources에 AES-256-GCM 암호화 저장, 메타데이터는
중앙 서버 경유 디바이스 간 동기화, zero-knowledge(서버는 암호문 blob + version 정수만),
같은 유저 디바이스 간에만 P2P/릴레이 허용, 실패 시 graceful 건너뛰기 금지(규격 에러 반환).

## 2. 브랜치 계보

두 단계는 선형이 아니라 "기능 브랜치 → 병합 → 메인 라인 연속"의 형태다.

```
dev-mvp1 (8dcf97e)
   └─ dev-mvp10  (기능 브랜치, 74커밋, 8dcf97e..4045c0e)
        └─ PR #1 병합(8d288bf) ── 메인 라인 ──▶ dev-mvp11 (38커밋, ..73a4a9c)
                                                      └─▶ mvp12 (현재 진행)
```

- dev-mvp10: MVP2 동기화 기반부터 MVP10 CLI 피벗·토큰 인증·Tkinter GUI·MVP3
  리플리케이션 엔진·운영 활성화까지를 담은 대형 기능 브랜치. 변경 규모 약
  +23,700 / -1,490 라인(124파일). PR #1로 메인 라인에 병합됐다.
- dev-mvp11: 병합 이후 메인 라인이 이어간 38커밋. GUI 현대화, 스토리지 attach/detach,
  디바이스 소스 레지스트리, 리모트 스필오버/티어링, daemon 상시 감독·무중단
  리로드, 교차 사용자 복제본 릴레이, 홀펀칭 전송 캐스케이드, 데몬 전송 위임.
  변경 규모 약 +5,700 / -780 라인(63파일).
- mvp12(현재): 원격 청크 전송(remote-chunked-transfer)과 업로드 다이얼로그. 본 문서
  말미에서 간략히만 다룬다.

## 3. dev-mvp10 — CLI 가상 파일서버 피벗 + 멀티디바이스 기반 + MVP3 엔진

dev-mvp10 브랜치는 성격이 다른 세 묶음을 포함한다: (A) WebDAV 시절에 쌓인 멀티디바이스
동기화·원격 접근 기반, (B) MVP10 CLI 피벗과 토큰 인증·GUI, (C) MVP3 암호화
리플리케이션 엔진과 운영 활성화.

### 3.A 멀티디바이스 동기화 · 원격 접근 기반 (피벗 이전)

WebDAV 실시간 마운트를 전제로 쌓였으나, 피벗 이후에도 전송·동기화 엔진으로 그대로
재사용되는 토대다.

- 같은 유저 디바이스 간 전송 연결: remote 소스 통합 마운트(783964e), 파일
  device_id 기록 + 디바이스 목록 조회(85d7e68), 내 다른 디바이스 자동 remote
  마운트(27ba791).
- 크로스 디바이스 파일 자동 라우팅(5d833d0): source_id/device_id 기반으로 read/write를
  로컬·원격으로 분기. 스펙 `.kiro/specs/cross-device-file-routing/`.
- 오프라인 내성: 오프라인 원격 소스가 루트 PROPFIND를 500으로 깨뜨리던 버그
  수정(b5591de, `is_remote` 속성 도입), 오프라인 비활성 마운트 + 동적
  재네고시에이션(0855cea).
- NAT 트래버설: 이중 NAT에서 reflexive 공인 IP로 connection_address 보정(e14fb11,
  ebada5c).
- P2P 릴레이 fallback(bd1cd1b): 직접 연결 불가 시 서버 long-polling 릴레이로 우회.
  스펙 `.kiro/specs/p2p-relay-fallback/`.
- 원격 파일 수정 시 로컬 소유권 이전 + orphan GC(9a0222d). 스펙
  `.kiro/specs/remote-write-takeover/`.
- 메타데이터 version 롱폴링 즉시 동기화(1bbe6f6): 30초 폴링 대비 ~1.4초 내 전파.
  스펙 `.kiro/specs/sync-longpoll-events/`.
- MVP5 평문 파일 공유 데모(5e5dc07) → 폐기 결정(d40cb55). 교차 계정 데이터는 항상
  소유자 키로 암호화된 복제본만 허용하고 평문 직접 공유는 두지 않는다. MVP5의
  share_token 인가 인프라는 MVP3 복제 노드 접근 인가의 토대가 됐다.

### 3.B MVP10 CLI 피벗 · 토큰 인증 · 데스크톱 GUI

- CLI 골격 + 로드맵 재작성(62c83bb): git/aws 스타일 단발 서브커맨드로 접근 모델 확정.
- 온라인 setup + devices/status/ls 소유자 컬럼(2c28ac2), device 재등록 제거로
  connection_address 보존(5e20a54).
- 전송 계열 CLI(aab1c7b): `put`/`get`/`rm`/`mkdir`/`mv`/`cp` + 로컬 서버 E2E.
- WebDAV 완전 제거(f134db0, f7a95cc, 2ae5bbd): daemon에서 WebDAV 제거 +
  라이프사이클(start/status/stop)로 대체, wsgidav/cheroot 서빙 스택·의존·레거시 v1
  삭제, config의 레거시 webdav 섹션 제거. 계획 문서 [MVP10_CLI_PLAN.md](./MVP10_CLI_PLAN.md).
- 토큰 인증 전환(bb3f5a6, 7cba07c, 2953aee): `.env` 재로그인 방식에서 자격증명
  저장소(`{metadata_db}.credentials.json`) 기반 access/refresh 토큰 영속화로 전환.
  로그인 비밀번호는 저장하지 않고, daemon/CLI 동시 갱신은 파일 락으로 직렬화.
  스펙 `.kiro/specs/token-auth-transition/`.
- Tkinter 데스크톱 GUI(83347b2): 파일 탐색기 + daemon 제어 + 로그인. 후속으로 초기
  설정 생성(f0321c5, 닭-달걀 문제 해소), 데몬 자동시작·스토리지 소스 관리(427c8b5),
  i18n(ko/en) + 시스템 트레이 최소화(260b35e), 버튼 상태 토글·종료 시 데몬
  정지(cd789bf), 로그인 게이팅 + 오프라인 세션 캐시(e16723b).

### 3.C MVP3 암호화 리플리케이션 엔진 (Phase 1~7)

암호화 청크를 ≥3개 홀더에 복제하고, 중앙 서버가 위치 레지스트리·배치·건강성·호혜
회계를 담당한다. 홀더는 자기 디바이스 + 타 사용자 디바이스를 포함하는 상호(reciprocal)
피어 네트워크이며, 호스트는 키가 없어 복호화할 수 없다. 스펙
`.kiro/specs/replication-parity/`.

- Phase 1(e40abce): `chunker.py`(암호문 4MiB 청크 split/join) + 메타데이터
  replication_status(메타 v4).
- Phase 3(2e54c27): `parity_store.py`(타 사용자 청크 암호문 보관·쿼터·소유자 인가) +
  P2P `/p2p/replica_{store,fetch,delete}` op(교차 사용자 토큰 검증, 소유자=요청자).
- Phase 4(a8c52ac): `replication_manager.py` — replicate(암호화→청킹→배치→push→≥3 시
  replicated, 아니면 pending), recover(스웜 fetch→결합→복호화). CLI `backup`/`restore`.
- Phase 5(703fcbe): UDP 홀펀칭(순수 파이썬 동시 오픈).
- Phase 6(15976a9): 건강성 기반 재복제(ensure_replicas) + CLI `heal`.
- Phase 7(8b276df): 다중 홀더 E2E + 문서 갱신. 상호 백업 모델로
  ARCHITECTURE/ROADMAP 정리(319656b).

운영 활성화(스펙 `.kiro/specs/replication-activation/`):

- daemon 자동 호스팅/백업/재복제(7d9c665): `replication.enabled` 시 제공 용량
  신고(report_hosting) + ParityStore(provided*0.5) + replication_scheduler 백그라운드 루프.
- 재복제 유예(grace) 정책(40c83b0): degraded가 `heal_grace_seconds`(기본 24h) 이상
  지속될 때만 재복제하여 일시 오프라인 churn 방지.
- GUI 백업 상태 표시(133a2f5) + 카운트 조회 성능 수정(b7f58b8, 0126b51).
- 기본 자동 백업 동작화(c566995, 8227b3e): provided 기본=로컬 용량, 목표 복제본 2,
  pending 즉시 재시도, 리플리케이션 정책 다운로드 + 기본 활성(비율 0.5).
- 2-device 백업 동작(010ec48): 복제본 전송 릴레이 fallback + 자기 device 제외 + 기본 min 1.
- 자동 백업 병렬화 + 홀더 직접 연결 타임아웃 단축(4045c0e, 브랜치 tip).

## 4. dev-mvp11 — 스토리지 운영 · 전송 인프라 · daemon 상시화

dev-mvp10 병합 이후 메인 라인이 이어간 단계다. 핵심은 (A) 스토리지 attach/detach와
스토리지 티어링, (B) daemon 상시 감독·무중단 리로드, (C) UDP 홀펀칭 전송 캐스케이드,
(D) 데몬 전송 위임, (E) GUI 현대화·배포다.

### 4.A 스토리지 attach/detach · 디바이스 소스 레지스트리 · 스토리지 티어링

- 스토리지 attach/detach(0ea37c5): Directory 타입 폐지, detach 시 evacuate 후 원자적
  분리. 리모트 분산 evacuate(2860ac7). 스펙 `.kiro/specs/storage-detach/`.
- 디바이스 소스 레지스트리(b57e5fa): 리모트 스토리지 통합 목록. 리모트 행은
  제거(detach) 버튼 비활성화(42d11cc). 스펙 `.kiro/specs/device-source-registry/`.
- 디바이스 소스 인벤토리 주기 재신고(0710bee): 리모트 용량 최신화.
- 리모트 스필오버 + 콜드 축출(c4429e8): 로컬 용량 부족 시 리모트로 스필오버,
  콜드 데이터 티어링. 스펙 `.kiro/specs/jbod-spillover-eviction/`.

### 4.B daemon 상시 감독 · 무중단 config 리로드

- daemon 항상 온라인 감독(55a79a1): 실패/중단 시 자동 재시작.
- daemon config 리로드(9998016): 무중단 remount + 소스 신고 주기 60s·변동 즉시.
  스펙 `.kiro/specs/daemon-config-reload/`.
- 디바이스 카운트 일치·주기 갱신, 백업 요약 소유 스코프, 소스 변경 후 daemon
  재기동(d8d8ebe).

### 4.C 홀펀칭 전송 캐스케이드 (UDP) — UPnP 폐지

직접 TCP → 홀펀칭 UDP → 서버 릴레이의 캐스케이드로 디바이스 간/복제 전송을 수행한다.
상세 [TRANSPORT.md](./TRANSPORT.md). 스펙 `.kiro/specs/holepunch-transport/`.

- UPnP 폐지 + 홀펀칭 전송 스펙(9605024).
- `rudp.py`(fb6a856): 신뢰성 UDP 메시지 채널. 이후 송신 윈도우 추가로 대용량 hang
  방지(1ae91de).
- `p2p_udp.py`(0903cac): rudp 위 P2P op 송수신.
- `holepunch_service.py`(30982a8): 랑데부 등록 + 펀치 + rudp 직접 전송.
- 복제 전송을 직접 TCP→홀펀칭 UDP→릴레이 캐스케이드로(e8b88cf).
- 랑데부 안정화: 호스트명을 IPv4로 해석 후 전송하여 등록 타임아웃 수정(b43d0dc),
  데몬 `_write_control` 크래시 내성 + 랑데부 error 응답 처리(35a3720).
- 홀펀칭 전송 캐스케이드 문서화 + e2e 검증 반영(73a4a9c, 브랜치 tip).

### 4.D 데몬 전송 위임 · 교차 사용자 복제본 릴레이

- 교차 사용자 복제본 릴레이 fallback(89b1cc0): replica_* op는 교차 사용자 릴레이를
  허용하고, 홀더가 payload 소유자 토큰으로 요청자=소유자를 도출해 ParityStore 인가에
  사용한다. 파일 데이터 op는 같은 user_id만 릴레이. 스펙
  `.kiro/specs/cross-user-replica-relay/`.
- UDP/릴레이 파일 op 인증 + RemoteSource 홀펀칭 UDP 전송(e8856be, 전송 위임 Phase 1/2).
- 데몬 전송 위임 채널(519f31c, Phase 3): GUI/CLI의 put/get을 daemon이 대행한다
  (`daemon_control.py`). 스펙 `.kiro/specs/daemon-transfer-delegation/`.

### 4.E GUI 현대화 · 배포

- GUI 백업 UX(8369cdd): 수동 백업/복제 점검 버튼 + 백업 현황 표시.
- 현대적 테마 sv-ttk 적용 + 라이트/다크 토글(7245ef1), Windows 11 탐색기 풍 레이아웃 +
  맑은 고딕(ead2bb0).
- exe 실행 시 콘솔 창 숨김 + daemon 콘솔 억제(ae856ee), 더블클릭 시 즉시 종료 →
  GUI 실행 + 프로즌 daemon 호출 수정(a5fa378), Windows 콘솔(cp1252) --help
  UnicodeEncodeError 방지(f382196).
- CI: 태그(v*) push 시 release 자동 등록 + 빌드 트리거 한정(47d4618, 9222d78).
- 복제 안정화: replication_status 전역 동기화(c32cb56) + 백필 마이그레이션
  v6(c8b54c7), 백업 실패(pending) 시 짧은 백오프 재시도로 5분 고정 대기 제거(d2f73fe).

## 5. 현재(mvp12) 진행분

dev-mvp11 이후 mvp12에서 이어지는 작업이다(본 리뷰 범위 밖, 참고용).

- 원격 청크 전송(remote-chunked-transfer): rudp 송신 윈도우, 원격 쓰기 소스 선택을
  페이로드 크기 기준으로 변경(홀더 용량 부족 500 해결), 리모트 스필오버/evacuate 전
  비활성 RemoteSource 재활성화. 스펙 `.kiro/specs/remote-chunked-transfer/`.
- 업로드 다이얼로그(dfa3353): 다중 파일/진행바/상태 로그 + 같은 경로 덮어쓰기 가드.

## 6. 스펙 ↔ 단계 매핑

| 스펙 디렉토리 | 단계 |
|---|---|
| `cross-device-file-routing` | dev-mvp10 (3.A) |
| `p2p-relay-fallback` | dev-mvp10 (3.A) |
| `remote-write-takeover` | dev-mvp10 (3.A) |
| `sync-longpoll-events` | dev-mvp10 (3.A) |
| `mvp5-file-sharing-demo` | dev-mvp10 (3.A, 폐기) |
| `token-auth-transition` | dev-mvp10 (3.B) |
| `replication-parity` | dev-mvp10 (3.C) |
| `replication-activation` | dev-mvp10 (3.C) |
| `storage-detach` | dev-mvp11 (4.A) |
| `device-source-registry` | dev-mvp11 (4.A) |
| `jbod-spillover-eviction` | dev-mvp11 (4.A) |
| `daemon-config-reload` | dev-mvp11 (4.B) |
| `holepunch-transport` | dev-mvp11 (4.C) |
| `daemon-transfer-delegation` | dev-mvp11 (4.D) |
| `cross-user-replica-relay` | dev-mvp11 (4.D) |
| `remote-chunked-transfer` | mvp12 (5) |
