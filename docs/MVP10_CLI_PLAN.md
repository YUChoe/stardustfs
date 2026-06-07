# MVP10: CLI 가상 파일서버 개발 계획

접근 계층을 WebDAV 실시간 마운트에서 FTP 유사 단발 서브커맨드 CLI로 전환한다.
저장·동기화·전송 엔진(stardustlib)은 그대로 재사용하고, 그 위의 접근 방식만
교체한다. 전략적 맥락은 [ROADMAP.md](./ROADMAP.md) 참조.

## 1. 범위

### 포함
- `stardust <subcommand>` 형태의 단발 CLI 엔트리포인트
- 업로드/다운로드/목록/삭제/이동/디바이스 조회/동기화/초기화 명령
- WebDAV(webdav_provider, wsgidav/cheroot 의존) 완전 제거
- 엔트리포인트(stardustfs.py)와 개발 스크립트(run-dev.sh)의 CLI 전환

### 비포함 (차후/보류)
- GUI 파일탐색기 (CLI 코어 API 재사용, 별도 단계)
- 리플리케이션/플랫폼 (보류, 재정의 예정)
- 실시간 마운트/동시 편집 (폐기)

## 2. 재사용하는 코어 API (JBODManager)

CLI는 신규 저장 로직을 만들지 않고 아래 기존 메서드를 호출한다.

| CLI 동작 | 코어 메서드 |
|----------|-------------|
| 목록 | `list_directory(virtual_path) -> list[EntryInfo]` |
| 파일 정보 | `get_file_info(virtual_path) -> FileInfo \| None`, `file_exists` |
| 다운로드 | `read_file(virtual_path) -> bytes` (로컬/원격 분기 내장) |
| 업로드 | `write_file(virtual_path, data)` (암호화·소스 선택 내장) |
| 삭제 | `delete_file`, `delete_directory` |
| 이동 | `move_file`, `move_directory` |
| 복사 | `copy_file` |
| 디렉토리 생성 | `create_directory` |
| 용량 | `get_total_space`, `get_available_space` |

`read_file`은 device_id로 로컬/원격을 분기하고, 원격은 P2P 직접 연결 → 실패 시
릴레이로 fetch한다. 즉 다운로드의 "소유 device에서 가져오기"는 이미 구현돼 있다.

## 3. 명령어 설계 (git/aws 스타일)

```
stardust login                      # 계정 로그인, 토큰 저장
stardust init [--config PATH]       # 설정/디바이스 등록/key·metadata 복원
stardust ls [PATH]                  # 가상 경로 목록 (크기, 소유 device, online 여부)
stardust put LOCAL [REMOTE]         # 업로드 (로컬 파일 → 가상 경로)
stardust get REMOTE [LOCAL]         # 다운로드 (가상 경로 → 로컬 파일)
stardust rm PATH [-r]               # 파일/디렉토리 삭제
stardust mkdir PATH                 # 디렉토리 생성
stardust mv SRC DST                 # 이동/이름변경
stardust cp SRC DST                 # 복사
stardust devices                    # 내 디바이스 목록 + online/offline
stardust df                         # 총/가용 용량
stardust sync [--once]              # 메타데이터 동기화 (1회 또는 데몬)
stardust status                     # 동기화 상태, 보류 변경 수
```

- 출력은 사람이 읽기 쉬운 표 + `--json` 옵션(스크립팅/향후 GUI용).
- 종료 코드: 성공 0, 사용자 오류 2, 규격 에러(오프라인/용량부족 등) 비0 고정 코드.

## 4. 단계별 태스크

### Phase 0 — 코어 API 경계 확정 (분석 완료, 8절 참조)
- [x] JBODManager 공개 메서드를 CLI가 호출할 "코어 파사드"로 확정(추가 구현 최소화)
- [x] 비동기 경계 확인: `read_file`/`write_file`의 원격 경로가 async 이벤트 루프를
      필요로 하는지 점검하고, CLI에서의 실행 모델(asyncio.run 진입점) 결정
- [x] init/login/sync 배선이 현재 stardustfs.py 초기화 순서의 어느 부분을 재사용할지 식별

### Phase 1 — CLI 골격 (진행 중)
- [x] `stardustlib/cli/` 패키지 생성(dispatcher/session/commands/format), argparse 서브커맨드
- [x] `stardustfs.py` main()을 서브커맨드 디스패처로 전환(`daemon` + 단발 명령),
      서브커맨드 없음 → daemon 으로 기존 동작 호환 유지
- [x] `_initialize_local_storage` → `_build_core`(WebDAV 비의존) 분리, CLI가 코어 재사용
- [x] 출력 포매터(표/JSON) 공통 모듈(UTF-8 직접 출력으로 Windows 콘솔 한국어 보존)
- [x] 첫 명령 `df`/`ls` 동작 검증 (전체 테스트 429 passed 유지)
- [ ] 설정 로딩은 연결됨. 토큰/auth 로딩은 Phase 2(온라인 setup)에서
- [ ] 콘솔 엔트리포인트(`stardust` 스크립트) 등록은 후순위. 현재는 `python
      stardustfs.py <cmd>`로 실행

주의(Git Bash): 인자로 `/`를 넘기면 MSYS 경로 변환이 일어나 Windows 경로로 바뀐다
(`ls /` ≠ 가상 루트). 가상 루트는 인자 생략(기본값 `/`) 또는 `MSYS_NO_PATHCONV=1`
사용. CLI 코드 자체는 정상이며 이는 셸 동작이다.

### Phase 2 — 조회 계열 명령 (진행 중)
- [x] 온라인 setup: `CLISession.open_online()` — 로그인 → device 등록/조회 →
      remote 마운트 → (선택) 1회 동기화. 단일 asyncio.run setup→op→teardown.
      서버 미설정/인증 실패 시 오프라인 강등.
- [x] `ls`: 크기 + 소유 device_id 컬럼(로컬 메타데이터 기준, self는 'this').
      online 여부 매핑(device 이름)은 devices에서 제공
- [x] `devices`: device_manager.list_devices() + online/offline + self 표시(온라인)
- [x] `df`: get_total_space/get_available_space (Phase 1 완료)
- [x] `status`: 보류 변경 수(get_pending_files) + 루트 엔트리 수 + online
- [ ] 검증 메모: df/ls/status는 오프라인 스모크 통과(429 passed 유지). `devices`는
      서버 접속·device 등록 부작용이 있어 prod 자동 실행 보류 — 로컬 서버
      (STARDUST_TEST_SERVER_URL) 또는 의도적 prod 실행으로 검증 필요

### Phase 3 — 전송 계열 명령 (진행 중)
- [x] `put`: 로컬 파일 읽기 → write_file(암호화·소스 선택·메타데이터) → upload_metadata 전파
- [x] `get`: read_file(로컬/원격 fetch, 자가 브리지) → 복호화본 로컬 저장
- [x] `rm`(-r)/`mkdir`/`mv`/`cp`: 코어 메서드 연결 + 전파
- [x] 오프라인 라운드트립 검증: put→ls→get 바이트 일치(IDENTICAL), rm 후 목록 빔.
      암호화 왕복 정상, 종료 코드 0
- [ ] 대용량 스트리밍/진행률은 후순위(현재 전체 바이트 메모리 적재). 부분 전송도
      zero-knowledge 유지 설계 필요

### Phase 4 — 오프라인/에러 규격 (부분 완료)
- [x] 규격 종료 코드 매핑: 0 성공 / 2 사용법 / 3 없음·로컬 I/O / 4 원격 오프라인·
      도달 불가(OSError) / 5 용량 부족(InsufficientStorageError). graceful skip 금지
- [x] 오프라인 세션(서버/인증 실패)에서 쓰기는 로컬 저장 + 전파 보류 안내(daemon
      동기화 대기). 콘솔 출력은 UTF-8 직접 기록(format.echo)으로 cp949 인코딩
      오류(em-dash 등) 회피
- [ ] 사용자 메시지 영국 영어화는 후속(현재 한국어 안내)

주의(Git Bash): 가상 경로 인자(`/foo`)는 MSYS 경로 변환으로 `C:/Program Files/
Git/foo`가 되어 실패한다. 검증·사용 시 `MSYS_NO_PATHCONV=1` 필요. 후속 개선
후보: CLI가 선행 슬래시 없는 상대 가상경로를 허용하고 내부에서 `/`를 보정.

### Phase 5a — daemon에서 WebDAV 제거 + 라이프사이클 (진행 중)
- [x] daemon 실행 경로에서 WebDAV 제거: startup_v2의 3개 분기(no-server/offline/
      online) + key 불일치 재초기화 + 등록 실패 분기를 `_build_core`(WebDAV 비의존)로
      전환. `_initialize_local_storage`/`_start_webdav` 제거(create_webdav_app 미import)
- [x] WebDAV 스레드 기반 keepalive 제거 → `stardustlib/daemon.py`의 제어 파일 기반
      생존 루프로 대체(크로스플랫폼, POSIX 시그널 비의존)
- [x] 라이프사이클 명령: `daemon`(start) / `daemon status` / `daemon stop`.
      제어 파일 `{metadata_db}.daemon.json`(pid+heartbeat) + 정지 센티넬
      `{metadata_db}.daemon.stop`. SIGINT/SIGTERM(가능 시) 핸들러도 graceful 경로.
      중복 시작 거부(제어 파일 기준)
- [x] `--config`를 서브커맨드 뒤에서도 받도록 공통 parent 적용(`daemon stop
      --config X` 등). 전역 --config와 충돌 없게 default=SUPPRESS
- [x] run-dev.sh를 `daemon` 실행으로 갱신(WebDAV/WebClient BasicAuthLevel 블록 제거)
- [x] 테스트: tests/test_daemon_lifecycle.py 5종(status 전이/stale/stop/serve
      통합). 오프라인 config로 start→status→stop→중복거부 실측 통과

### Phase 5b — WebDAV 모듈/의존 삭제 (진행 중)
- [x] webdav_provider.py 삭제 + 관련 테스트(test_webdav_provider/app/placeholder) 제거
- [x] initializer.py 삭제 + test_initializer.py 제거 (webdav_provider 전용 의존이었음)
- [x] 레거시 v1 제거: `_startup_v1` 삭제, `_run_daemon`은 v2 전용(v1 설정은 에러),
      `initialize_system` import 제거, wsgidav 로거 억제 제거
- [x] requirements.txt에서 WsgiDAV/cheroot 제거
- [x] config의 webdav 섹션/스키마/검증 제거: models의 WebDAVConfig + StardustConfig/
      StardustConfigV2의 webdav 필드 삭제, config_loader의 host 강제·webdav 포트 검증
      제거, migrate_v1_to_v2가 레거시 webdav를 pop(드롭), dev-config.json에서 webdav
      섹션 제거. config 테스트 3종(loader/pbt/migration) 갱신(34 passed)

### Phase 6 — 문서/스펙
- [ ] `.kiro/specs/cli-virtual-fileserver/`에 requirements/design/tasks 작성
      (스펙 작성 가이드 준수: Correctness Properties, EARS 등)
- [ ] ARCHITECTURE.md 갱신(현재 구버전 FUSE Passthrough 설명을 현행 구조로 교체)
- [ ] API_REFERENCE.md에 CLI 명령/코어 파사드 반영

## 5. 검증 전략

- 각 Phase 변경 후 클라이언트/서버 양쪽 `pytest` 회귀 확인(핸드오버 8절).
- 전송 계열은 E2E(별도 테스트 계정 `e2e-test@example.com`)로 로컬↔원격 다운로드,
  오프라인 device 에러, 릴레이 경유를 검증.
- WebDAV 제거 전후로 "업로드 → 다른 device에서 다운로드 → 복호화 일치" 시나리오를
  골든 테스트로 고정.

## 6. 위험과 결정 필요 지점

- 엔트리포인트 구조: stardustfs.py를 CLI로 흡수할지, sync 데몬과 CLI를 분리할지
  (Phase 0에서 확정).
- read/write의 async 경계가 CLI 단발 실행과 맞물리는 방식(이벤트 루프 수명).
- WebDAV 제거 시 회귀 테스트 손실 → CLI 테스트로 사전 이전 후 제거.

## 7. 비범위 재확인

이번 단계는 접근 계층 교체에 한정한다. 리플리케이션/플랫폼/GUI는 MVP10 안정화
이후 별도 단계로 다룬다.

## 8. Phase 0 결과 (코드 분석 완료, 2026-06)

### 8.1 코어 파사드 = JBODManager (전부 동기)
파일 작업은 모두 동기 메서드다(`read_file`/`write_file`/`delete_file`/`move_file`/
`copy_file`/`create_directory`/`list_directory`/`get_file_info`/`get_total_space`/
`get_available_space`). 별도 파사드 계층을 만들 필요가 없다. CLI는 JBODManager를
직접 호출한다.

### 8.2 동기↔비동기 브리지는 이미 해결됨 (핵심)
원격 I/O는 `remote_source.py`의 `_EventLoopThread` 싱글턴(전용 백그라운드 이벤트
루프 스레드)에서 실행되고, `run_coroutine_threadsafe(...).result()`로 동기 반환된다.
즉 `JBODManager.read_file`/`write_file`는 호출자의 이벤트 루프와 무관하게 동작하며,
원격 다운로드의 "소유 device에서 P2P/릴레이 fetch"가 동기 메서드 안에 캡슐화돼 있다.
따라서 **CLI 파일 명령은 자체 이벤트 루프를 소유하지 않아도 된다.**

### 8.3 async 표면은 setup/teardown에만
async가 필요한 곳: `auth_client`(login/refresh/get_valid_token), `device_manager`
(register/list_devices/heartbeat/upnp), `sync_client`(initial_sync/reconcile/
periodic), `p2p_server`(start/stop), `relay_worker`(start/stop). 파일 전송 자체는
async가 아니다.

주의: `auth_client`는 `__init__`에서 단일 `httpx.AsyncClient`를 생성한다. httpx는
처음 await되는 루프에 바인딩되므로, CLI에서 `asyncio.run()`을 여러 번 열고 닫으면
바인딩이 깨질 수 있다. → CLI 한 번 실행은 단일 `asyncio.run()` 안에서 setup→op→
teardown을 모두 처리한다.

### 8.4 권장 CLI 실행 모델
명령 1회 = 단일 `asyncio.run(_run(args))`:
1. setup(async): 로그인 → 로컬 스토리지 초기화(동기) → device 등록/조회 →
   remote 소스 마운트(동기, 내부 자가 브리지) → 1회 메타데이터 동기화(initial_sync)
2. 파일 op: 동기 스토리지 호출. 메인 루프 블로킹을 피하려면
   `await loop.run_in_executor(None, lambda: jbod.read_file(path))`
3. teardown(async): `auth_client.close()` + 시작한 컴포넌트 stop
- 로컬 전용 명령(`df`, 로컬 소유 파일 `ls`)은 서버 없이도 동작 가능하나, 신선한
  메타데이터가 필요하면 1회 동기화를 포함한다.
- 단발 client 명령에는 **자신의 P2P 서버/릴레이 워커/heartbeat/주기 동기화를 띄우지
  않는다**(우리는 fetch하는 client). 단, 8.5의 daemon 문제 참조.

### 8.5 재사용 가능한 stardustfs.py 배선
`startup_v2`의 8단계 중 (1)설정검증 (2)인증 (3)key복원 (4)로컬스토리지
(`_initialize_local_storage`) (5)device등록+remote마운트(`_mount_remote_sources`)
(6)메타데이터 동기화 는 그대로 재사용한다. (7)P2P/릴레이 (8)WebDAV 스레드만
CLI 모델에서 분리/제거 대상이다. `_initialize_local_storage`는 현재 마지막에
`create_webdav_app`을 호출하므로, WebDAV 의존을 떼어내도록 분리해야 한다.

### 8.6 결정 필요 — daemon vs CLI-only (Phase 1 착수 전 확정)
핸드오버 6절: "StardustFS는 클라이언트 구동 중에만 파일 접근 가능. 오프라인
디바이스는 변경 불가." 즉 device가 다른 device에 파일을 **제공**하려면 그 device의
프로세스가 P2P 서버/릴레이 워커를 띄운 채 떠 있어야 한다. CLI 단발 실행만 있으면
이 device는 거의 항상 오프라인 피어가 되어, 다른 device가 이 device의 파일을
`get`할 수 없다.

따라서 다음 구조를 권장한다:
- `stardust daemon`(상주): startup_v2에서 WebDAV만 제거한 형태. device 등록 +
  P2P 서버 + 릴레이 워커 + heartbeat + (주기/롱폴) 동기화. 이 device를 온라인
  피어로 유지.
- `stardust <cmd>`(단발): client 작업. 로컬 SQLite 메타데이터는 WAL +
  `busy_timeout`으로 daemon과 동시 접근 가능.

확정(2026-06):
- 구조: **daemon + CLI 2-프로세스**. `stardust daemon`이 상주하며 device를 온라인
  피어로 유지하고, 단발 CLI는 별 프로세스로 실행한다. 메타데이터 SQLite는
  WAL + `busy_timeout`으로 두 프로세스가 동시 접근한다.
- 엔트리포인트: **`stardustfs.py` 하나로 통합**(서브커맨드). `stardustfs.py daemon
  --config ...`는 상주 데몬(현 startup_v2에서 WebDAV 제거), `stardustfs.py
  get/put/ls/...`는 단발 명령. 두 진입 파일로 나누지 않는다.

### 8.8 CLI 비등록 모델 (2026-06-03 확정)
CLI 온라인 명령은 `device_mgr.register()`(POST /devices)를 호출하지 않는다.

배경: 서버 `register_device`는 (user_id, name, os)로 중복을 제거하므로 device가
누적되진 않으나, 재등록 시 connection_address를 요청자 주소로 갱신한다. CLI는
UPnP/reflexive 보정을 하지 않아 LAN 주소(`_get_local_ip():p2p_port`)를 보낸다.
daemon이 이중 NAT 환경에서 공인 IP로 보정해 둔 주소를 CLI 실행이 LAN 주소로
덮어쓰면, 그 사이 다른 device의 인바운드 P2P가 도달 실패한다(heartbeat ≤60초 후
재보정되나 공백 발생).

해결: `CLISession.open_online()`은 `list_devices()` 결과에서 (name, os)로 자기
device를 식별해(`_identify_self`) device_id만 얻고 등록은 생략한다. 등록·주소
보정은 daemon이 소유한다. 자기 device가 목록에 없으면(daemon 미실행) 경고 후
원격 라우팅 없이 진행한다. connection_address는 CLI에서 전송하지 않는다.

### 8.7 Phase 1 착수 구조 (확정 반영)
1. `_initialize_local_storage`를 분리: 코어 빌드(jbod/metadata/encryption/db_key)와
   `create_webdav_app` 호출을 떼어내, CLI가 WebDAV 없이 코어만 조립하도록 한다.
2. `stardustfs.py` main()을 argparse 서브커맨드로 전환: `daemon`(=현 startup 흐름,
   WebDAV 스레드 제거) + 단발 명령들. 기존 `--config` 동작 호환 유지(회귀 방지).
3. `stardustlib/cli/` 패키지: dispatcher(서브커맨드) + session(단일 asyncio.run
   setup/op/teardown) + format(표/JSON). 첫 명령으로 `df`/`ls`를 붙여 모델 검증.
각 단계 후 클라이언트/서버 pytest 그린 유지(현재 baseline: 클라 429, 서버 72).
