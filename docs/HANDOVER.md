# StardustFS 핸드오버 가이드

다른 AI 에이전트가 작업을 이어받기 위한 문서다. 프로젝트의 현재 상태, 아키텍처,
작업 규칙, 다음 단계를 정리한다.

## 1. 프로젝트 개요

StardustFS는 분산 암호화 파일시스템이다. 사용자는 여러 디바이스(PC)에서 같은 계정으로
CLI와 GUI 파일탐색기를 통해 단일 가상 파일공간에 접근한다(WebDAV 실시간 마운트는
MVP10에서 폐기했다). 파일은 클라이언트에서 4 MiB 암호문 청크로 나뉘어 청크별로
AES-256-GCM 암호화되어 로컬 스토리지에 저장되고, 메타데이터는 중앙 서버를 통해
디바이스 간 동기화된다. 디바이스 간 파일 전송은 직접 TCP → 홀펀칭 UDP → 서버 릴레이의
캐스케이드로 이뤄진다(상세 [TRANSPORT.md](./TRANSPORT.md)).

핵심 보안 원칙: zero-knowledge. 서버는 파일 내용과 메타데이터 내용을 보지 못한다.
서버가 다루는 것은 암호화된 불투명 blob과 정수 version뿐이다.

## 2. 저장소 구조 (두 개의 별도 git 저장소)

- 클라이언트: `c:\Users\yonguk.choe\src\stardustfs`
  - `stardustfs.py`: 엔트리포인트(초기화 순서, daemon/CLI/GUI 서브커맨드)
  - `stardustlib/`: 핵심 라이브러리
  - `tests/`: pytest 테스트
  - `.kiro/specs/`: 기능별 스펙(requirements/design/tasks)
- 서버: `c:\Users\yonguk.choe\src\stardustfs-server` (FastAPI)
  - `app/routers/`: 엔드포인트
  - `app/services/`: 비즈니스 로직
  - `tests/`: pytest 테스트
  - `data/DATABASE_SCHEMA.md`: DB 스키마 (반드시 최신 유지, 추측 금지)

두 저장소 모두 현재 브랜치는 `mvp13-partial-metadata-sync`이다.

## 3. 핵심 컴포넌트 (클라이언트 stardustlib/)

- `storage_pool.py`: 스토리지 풀 통합. 파일 읽기/쓰기 라우팅의 중심.
  - `write_file`: 평문을 4 MiB 청크로 나눠 청크별 암호화 → 청크마다 보관처 선택
    (`_place_chunks`, 로컬 우선·부족하면 원격 스필오버) → 전부 성공 시 매니페스트 커밋
  - `read_file`/`read_range`: 청크 매니페스트를 보고 청크의 device_id로 로컬/원격 분기해
    결합. `read_range`는 범위를 덮는 청크만 가져온다
  - `read_chunks`/`write_chunks`: at-rest 청크를 그대로 주고받는 복제용 경로
  - `migrate_to_chunks`: 레거시 통짜 blob을 청크 표현으로 무손실 전환
  - 원격 소유 파일 수정은 `_takeover_write`(소유권 이전)
  - `gc_orphan_files`/`gc_orphan_files_if_needed`: orphan 물리 파일 정리(청크 경로 포함)
  - `select_source`/용량 집계: `is_remote` 소스는 제외(로컬 전용)
- `chunker.py`: 청크 split/join/hash + `chunk_range`(범위→인덱스),
  `shard_prefix`(해시 앞 2hex 하위 디렉터리), `shard_depth_for`(소스 용량별 샤딩 깊이).
- `metadata_store.py`: SQLite 메타데이터(SQLCipher 가능 시 암호화, 아니면 평문 폴백).
  files 테이블에 `deleted`(tombstone), `device_id`, `version`, `sync_status`, `chunked`
  컬럼. `file_chunks` 테이블이 파일별 청크 배치(chunk_index/chunk_ref/source_id/
  device_id/size/hash)를 보관하며 청크 파일의 위치 정본이다(files의 source_id/
  physical_path는 첫 청크를 가리키는 레거시 호환 컬럼).
- `storage_source.py`: `StorageSource` 추상 + `DirectorySource`/`LoopbackSource`.
  - LoopbackSource는 `<path>`를 고정 크기 FAT 이미지(pyfatfs)로 포맷해 파일을 이미지
    내부에 저장한다(동반 디렉터리 폐지). 청크는 `<hh>/<hex32>_cNNNN` 경로에 둔다
  - `list_physical_files`는 샤드 하위 디렉터리까지 재귀 스캔한다(orphan GC용)
  - `is_remote` 속성: 로컬은 False, RemoteSource는 True
- `remote_source.py`: 원격 디바이스 프록시. 직접 TCP → 홀펀칭 UDP → 릴레이 캐스케이드.
  - `is_online=False`면 비활성 마운트, `refresh()`로 재네고시에이션(30초 throttle)
- 홀펀칭 전송: `rudp.py`/`p2p_udp.py`/`holepunch_service.py`, GUI/CLI 위임은
  `daemon_control.py` (상세 [TRANSPORT.md](./TRANSPORT.md))
- `relay_client.py`: 요청자 측 릴레이(직접 연결 실패 시 서버 경유)
- `relay_worker.py`: 대상 측 릴레이 워커(`/relay/poll` 루프 → P2PServer.dispatch)
- `p2p_server.py`: aiohttp P2P 서버. `handle_*`는 인증 후 `_op_*` 호출, `dispatch(op, payload)`는 릴레이 워커용
- `sync_client.py`: 메타데이터 동기화. 주기 폴링 + 버전 롱폴링(즉시 동기화) + CAS + orphan GC 트리거
- `device_manager.py`: 디바이스 등록/heartbeat. 광고 주소는 LAN 주소(UPnP·reflexive
  공인 IP 보정 모두 폐지 — 사용자 포트포워딩을 전제하지 않음). 직접 TCP는 같은 LAN
  전용이고, 다른 네트워크의 직접 연결은 홀펀칭이 담당
- `webdav_provider.py`: wsgidav 기반 WebDAV. 오프라인 원격 파일은 `.offline` placeholder

## 4. 완료된 작업 (시간순, 최신이 아래)

### MVP2 (멀티디바이스 동기화) — origin/dev-mvp2
- tombstone 기반 삭제 동기화, CAS 낙관적 잠금, P2P 실환경 검증, 새 디바이스 key 복원
- tombstone GC + 장기 오프라인 재조정, PBT 8종

### dev-mvp5-share-demo 브랜치 (현재)
순서대로:
1. MVP5 파일 공유 데모(평문) → **폐기 결정**. 교차 계정 P2P는 암호화 복제만 허용
2. remote 소스 통합 마운트(783964e): 같은 유저 디바이스 간 전송 연결
3. 파일 device_id 기록 버그 수정 + 디바이스 목록 조회(85d7e68)
4. 내 다른 디바이스 자동 remote 마운트(27ba791)
5. 크로스 디바이스 파일 자동 라우팅(5d833d0): source_id 기반, device_id로 로컬/원격 분기
6. 오프라인 원격 소스가 WebDAV PROPFIND를 500으로 깨뜨리던 버그 수정(b5591de): `is_remote` 도입
7. 이중 NAT reflexive 공인 IP 보정(e14fb11, ebada5c): 서버 STUN 등가 + 즉시 heartbeat 반영
8. 오프라인 비활성 마운트 + 동적 재네고시에이션(0855cea)
9. P2P 릴레이 fallback(bd1cd1b 클라 / 2c4bfb6 서버 / d587805 reflexive):
   이중 NAT/CGNAT로 직접 연결 불가 시 서버 long-polling 릴레이로 우회
10. LoopbackSource 동반 디렉토리 경로 버그 수정(6f5e0d9): dispatch가 항상 404 나던 문제
11. 원격 파일 수정 시 로컬 소유권 이전 3a + orphan GC(9a0222d)
12. 메타데이터 version 롱폴링 즉시 동기화(1bbe6f6 클라 / a92bb61 서버)

현재 상태: 클라이언트 427 passed(+2 skip), 서버 72 passed.

## 5. 실환경에서 검증된 동작 (2 PC, 이중 NAT)

- PC-A/PC-B가 서로 다른 하위 NAT에 격리(직접 P2P 도달 불가) → 릴레이로 read/write 동작
- reflexive 공인 IP 보정(과거): UPnP 외부 IP가 사설/CGNAT일 때 공인 IP로 보정했으나,
  상위 NAT 포트포워딩이 없으면 여전히 도달 불가라 폐지됨. 현재는 홀펀칭 UDP가 대체
  (실측: 서로 다른 NAT 뒤 12MiB 스필오버 성공)
- 소유권 이전 시나리오 정상. 메타데이터 version 롱폴링으로 ~1.4초 내 전파(폴링 30초 대비)

## 6. 핵심 설계 결정 (반드시 준수)

- zero-knowledge: 서버는 암호문 blob + version 정수만. 파셜 전송도 이 원칙 유지해야 함
- 같은 유저 디바이스 간만 P2P/릴레이 허용. 교차 사용자는 암호화 복제(MVP3)만, 평문 공유 폐기
- StardustFS는 클라이언트 구동 중에만 파일 접근 가능. 오프라인 디바이스는 변경 불가
  (→ 소유권 이전 시 충돌이 원천적으로 없음)
- "실패 시 graceful 건너뛰기" 금지. 용량부족 등은 규격 에러(InsufficientStorageError 등) 반환
- orphan GC는 관리 파일(`<hex32>_` 형식)만 대상. metadata DB/사용자 파일은 절대 미삭제.
  device_id가 None이면 GC 전체 건너뜀(전체 삭제 방지)
- 릴레이/롱폴링/랑데부는 모두 단일 uvicorn 워커 + 메모리 상태 가정. 다중 워커는 외부 큐 필요(범위 밖)

## 7. 개발 환경 및 규칙

- Windows + Git Bash. 가상환경 `.venv` (`source .venv/Scripts/activate`), `PYTHONPATH=.`
- 프로덕션 Python 3.9 (3.10+ 전용 API 금지. 예: `TemporaryDirectory(ignore_cleanup_errors=)` 사용 불가 → mkdtemp+rmtree)
- SQLite는 python 스크립트로만 조작(`sqlite3` CLI 없음). python -c는 단일 라인
- 파일은 LF + UTF-8
- 응답/주석 한국어. 영어 텍스트는 영국 영어
- git: `&&` 체이닝 금지(개별 실행), `git add *` 금지, 관련 파일만 선별 스테이징,
  커밋은 명시적 지시 시에만, force push 금지
- git commit 시 knowledge graph(StardustFS 엔티티)에 기록
- E2E는 별도 테스트 계정(`e2e-test@example.com`), 실제 사용자 계정 오염 금지

## 7-1. 인증 (토큰 기반, 2026-06 전환)

`.env`의 `STARDUST_EMAIL`/`STARDUST_PASSWORD`로 매 실행 재로그인하던 방식에서,
토큰을 자격증명 저장소에 영속화하는 방식으로 전환했다(스펙
`.kiro/specs/token-auth-transition/`).

- 자격증명 저장소: `{metadata_db}.credentials.json`(소유자 전용 권한). access/refresh
  토큰 + key_password 보관. 로그인 비밀번호는 저장하지 않는다.
- CLI:
  - `stardustfs.py login --config <cfg> [--email .. --password .. --key-password ..]`
    — 미지정 시 환경변수/대화형(getpass). 성공 시 토큰 저장.
  - `stardustfs.py logout --config <cfg>` — 서버 `POST /auth/logout`(refresh 취소,
    best-effort) 후 저장소 삭제.
- 온라인 명령(devices/get/put 등)·daemon·복구는 저장소 토큰을 사용한다. 토큰이
  없거나 만료/무효면 CLI 온라인 명령은 "login 필요"(비0), daemon은 오프라인 강등.
- 토큰 갱신: access 만료 임박 시 refresh로 자동 갱신, 회전 토큰을 저장소에 기록.
  daemon/CLI 동시 갱신은 파일 락(`{credentials}.lock`)으로 직렬화.
- 서버: `POST /auth/logout`(user-scoped refresh 취소, 멱등). 미배포 서버는 404 →
  클라이언트가 로컬 삭제로 폴백.

### 기존 사용자 마이그레이션
1. `stardustfs.py login --config <cfg>` 1회 실행(저장소 없으면 `.env`의
   EMAIL/PASSWORD/KEY_PASSWORD를 일회성 입력으로 사용 가능).
2. `stardustfs.py devices --config <cfg>`로 토큰만으로 접근되는지 확인.
3. `.env`에서 `STARDUST_EMAIL`/`STARDUST_PASSWORD`/`STARDUST_KEY_PASSWORD` 제거.
- master.key·metadata_db·서버 device 레코드는 전환과 무관(변경하지 않음).
  롤백: credentials.json 삭제 + `.env` 복원.

## 8. 빌드/테스트/실행 명령

```bash
# 클라이언트 (c:\Users\yonguk.choe\src\stardustfs)
source .venv/Scripts/activate && PYTHONPATH=. python -m pytest -q        # 전체 테스트
./run-dev.sh                                                              # 개발 실행(WebDAV 8080)

# 서버 (c:\Users\yonguk.choe\src\stardustfs-server)
source .venv/Scripts/activate && python -m pytest -q                      # 전체 테스트
STARDUST_PORT=8000 python main.py                                         # 로컬 서버

# E2E (로컬 서버 필요)
STARDUST_TEST_SERVER_URL=http://127.0.0.1:8000 PYTHONPATH=. python -m pytest tests/test_relay_e2e.py tests/test_sync_longpoll_e2e.py -v
```

E2E 테스트는 롱폴/릴레이 미배포 서버에서는 자동 skip된다(엔드포인트 404 감지).

## 9. 배포 시 주의

- 서버 커밋(2c4bfb6 릴레이, a92bb61 롱폴링)을 운영 서버에 배포해야 해당 기능 동작
- 미배포 서버에서는 클라이언트가 404 감지 후 기존 방식으로 자동 폴백(하위 호환)
- DB 스키마 변경 없음(릴레이/롱폴링/랑데부 모두 메모리 상태 또는 기존 컬럼 사용)

## 10. 다음 단계 (미완료)

### B: 파셜/증분 메타데이터 전송 — 완료 (mvp13)
B-1(레코드 단위 암호화)으로 결정·구현했다. zero-knowledge를 유지하기 위해 B-2(서버가
평문 메타데이터를 봄)는 채택하지 않았다.

- 서버가 `metadata_records`(user_id+record_id PK) + `metadata_version`(글로벌 카운터)로
  레코드 단위 암호문을 보관한다. 클라이언트는 `since` 필터로 변경분만 받고
  base_version CAS로 올린다.
- record_id = HMAC-SHA256(HKDF(master_key), virtual_path) — 서버에 경로 비노출.
- 레코드 평문은 256B 배수로 패딩해 암호문 크기로 경로 길이를 추정하지 못하게 한다.
- 구버전 서버(레코드 미지원, 404)는 전체 blob 경로로 자동 폴백.
- 청크 네이티브 저장 도입 후에는 파일 레코드 페이로드에 청크 매니페스트(`chunks`)가
  함께 실린다. 동기화 단위는 여전히 파일이므로 record_id·CAS·롱폴 프로토콜은 불변이고
  서버 변경도 없었다.

스펙: `.kiro/specs/partial-metadata-sync/`, `.kiro/specs/chunk-native-storage/`.

수용한 트레이드오프: 레코드 방식은 전체 blob 대비 서버가 레코드 개수(파일 수 근사),
개별 암호문 크기(256B 패딩으로 완화), record_id별 변경 패턴을 추가 관측한다. 경로 평문과
파일 내용은 여전히 보이지 않는다.

### MVP3: 암호화 리플리케이션 — 엔진 구현 완료 (2026-06)
스펙 `.kiro/specs/replication-parity/`. Phase 1~7 완료.
- 청킹(`chunker.py`) + 메타 replication_status, 서버 chunks/replicas/hosting 레지스트리
  + ReplicationService(placement·건강성·호혜 0.5) + `/replication/*`.
- 호스트 역할 `parity_store.py` + P2P `/p2p/replica_{store,fetch,delete}`(교차 사용자
  토큰 검증, 소유자=요청자 인가는 ParityStore가 집행, 호스트 비가독).
- `replication_manager.py`: replicate/recover/ensure_replicas, CLI `backup`/`restore`/
  `heal`. file_ref/chunk_id는 가상경로 SHA-256(경로 비노출).
- 청크 네이티브 저장 도입 후: 저장 단위가 이미 청크이므로 복제가 재분할·재암호화 없이
  at-rest 청크를 그대로 쓴다(`_chunks_to_replicate`). recover는 받은 조각이 청크
  표현이면 청크별로 복호화 검증 후 그대로 되돌려 기록하고(`write_chunks`), 레거시
  blob이면 이어붙여 단일 블록으로 기록한다. 어느 경우든 at-rest 바이트가 복제 시점과
  같아 등록된 청크 해시가 계속 유효하다. 그전에는 청크 암호문 연결을 다시 4 MiB로
  나눠 복제하고 복구 때 단일 GCM blob으로 복호화하려 해 다중 청크 파일에서 실패했다.
- UDP 홀펀칭(`holepunch.py` + 서버 `rendezvous.py` 옵트인). E2E `test_replication_e2e.py`.
- 운영 활성화(스펙 `.kiro/specs/replication-activation/`, Phase A1~A3): daemon이
  `replication.enabled` 시 제공 용량 신고(`report_hosting`) + ParityStore(provided*0.5)
  + `replication_scheduler`(자동 backup/heal 백그라운드 루프, heal은 degraded가
  `heal_grace_seconds`(기본 24h) 이상 지속 시에만 재복제 — 일시 오프라인 churn 방지).
  기본 비활성.

남은 항목(이관/후속):
- 교차 사용자 릴레이 fallback: RelayHub가 same-user만 중개 → 허브 인가 모델 재설계
  (보안 민감)가 필요해 별도 스펙으로 분리. 현재 도달성은 직접+스웜(≥3)+홀펀칭.
- (완료) 데이터 전송의 UDP 채널 전환 + daemon 자동 랑데부 등록 — 홀펀칭 전송
  캐스케이드(직접 TCP→홀펀칭 UDP→릴레이) e2e 검증(2026-06-07, [TRANSPORT.md](./TRANSPORT.md)).
- 등급별 정책(MVP4), erasure coding(저장 효율).

### 알려진 한계
- 이중 NAT에서 직접 P2P는 상위 NAT 포트포워딩 없으면 불가 → 릴레이로 우회(구현됨)
- 릴레이/롱폴링은 단일 워커 가정. 수평 확장 시 외부 pub/sub(Redis 등) 필요
- 청크 네이티브 저장은 단위·통합 테스트만 통과했고 실환경 스모크(기존 metadata DB의
  v7 마이그레이션, 2대 기기 간 매니페스트 동기화·청크별 라우팅)는 아직 수행하지 않았다.
- 청크가 여러 기기에 흩어지면 그 기기들이 모두 도달 가능해야 파일 전체를 읽을 수 있다
  (백업 사본이 이를 완화한다).
- 부분 쓰기는 미구현(파일 일부 수정도 전체 재기록). 원격 청크의 부분 읽기는 청크 단위로
  전량을 받는다. 남은 레거시 blob의 일괄 전환 스케줄러도 미구현.

## 11. 스펙 위치

`.kiro/specs/` 아래 기능별 requirements/design/tasks:
- `cross-device-file-routing/`: device_id 기반 자동 라우팅
- `p2p-relay-fallback/`: 서버 경유 long-polling 릴레이
- `remote-write-takeover/`: 소유권 이전 3a + orphan GC
- `sync-longpoll-events/`: 메타데이터 version 롱폴링
- `replication-parity/`: 암호화 리플리케이션(MVP3, Phase 1~7 완료)
- `mvp2-client-multidevice/`: MVP2 전체

작업 재개 시 해당 스펙의 tasks.md에서 미완료(`[ ]`) 항목을 확인할 것.
