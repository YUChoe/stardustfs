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

### Phase 2 — 조회 계열 명령
- [ ] `ls`: list_directory 결과를 크기·소유 device·online 여부와 함께 출력
- [ ] `devices`: device_manager 조회 + online/offline 표시
- [ ] `df`: get_total_space/get_available_space
- [ ] `status`: sync_client의 보류 변경/마지막 동기화 시각

### Phase 3 — 전송 계열 명령
- [ ] `put`: 로컬 파일 읽기 → write_file(암호화·소스 선택·메타데이터 등록) → 동기화 트리거
- [ ] `get`: get_file_info로 소유 device 확인 → read_file(로컬/원격 fetch) → 복호화본 로컬 저장
- [ ] 대용량 스트리밍/진행률 표시 검토(부분 전송도 zero-knowledge 유지)
- [ ] `rm`/`mkdir`/`mv`/`cp`: 해당 코어 메서드 연결

### Phase 4 — 오프라인/에러 규격
- [ ] 소유 device offline 시 graceful skip 금지 → 규격 에러(DeviceOfflineError 등) +
      고정 종료 코드, 사용자 메시지(영국 영어)
- [ ] 용량부족 InsufficientStorageError 등 기존 예외를 CLI 종료 코드로 매핑
- [ ] 네트워크 실패(P2P/릴레이 모두 불가) 시 명확한 진단 출력

### Phase 5 — WebDAV 제거
- [ ] webdav_provider.py 및 wsgidav/cheroot 의존 제거(requirements.txt 정리)
- [ ] stardustfs.py 엔트리포인트를 WebDAV 서버 기동 → CLI 디스패처로 전환
      (또는 stardustfs.py는 sync 데몬 전용으로 축소하고 CLI를 별 엔트리로 분리)
- [ ] run-dev.sh를 CLI 데모 흐름으로 갱신(루프백 소스 3개 유지)
- [ ] WebDAV 전용 테스트 제거/대체, CLI 테스트로 회귀 커버리지 이전

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
2. 파일 op: 동기 JBOD 호출. 메인 루프 블로킹을 피하려면
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

### 8.7 Phase 1 착수 구조 (확정 반영)
1. `_initialize_local_storage`를 분리: 코어 빌드(jbod/metadata/encryption/db_key)와
   `create_webdav_app` 호출을 떼어내, CLI가 WebDAV 없이 코어만 조립하도록 한다.
2. `stardustfs.py` main()을 argparse 서브커맨드로 전환: `daemon`(=현 startup 흐름,
   WebDAV 스레드 제거) + 단발 명령들. 기존 `--config` 동작 호환 유지(회귀 방지).
3. `stardustlib/cli/` 패키지: dispatcher(서브커맨드) + session(단일 asyncio.run
   setup/op/teardown) + format(표/JSON). 첫 명령으로 `df`/`ls`를 붙여 모델 검증.
각 단계 후 클라이언트/서버 pytest 그린 유지(현재 baseline: 클라 429, 서버 72).
