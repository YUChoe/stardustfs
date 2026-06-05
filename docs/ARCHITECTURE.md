# StardustFS 아키텍처

## 개요

StardustFS는 여러 디바이스의 스토리지를 하나의 가상 파일서버로 묶는 암호화 분산
파일시스템이다. 사용자는 같은 계정의 여러 PC/Linux에 분산된 스토리지를 단일
네임스페이스로 업로드/다운로드한다.

접근 계층은 FTP 유사 CLI다(MVP10 피벗). 과거의 WebDAV 실시간 마운트는 제거되었다.
파일은 클라이언트에서 AES-256-GCM으로 암호화되어 각 디바이스의 스토리지에
저장되고, 메타데이터는 중앙 서버를 통해 디바이스 간 동기화된다. 디바이스 간 파일
전송은 P2P(직접 연결 또는 서버 릴레이)로 이뤄진다.

핵심 보안 원칙(zero-knowledge): 서버는 파일 내용과 메타데이터 내용을 보지 못한다.
서버가 다루는 것은 암호화된 불투명 blob과 정수 version, 그리고 라우팅/인증에 필요한
최소 정보뿐이다.

## 프로세스 구조 (daemon + CLI)

같은 설정(config + metadata_db)을 공유하는 두 종류의 프로세스가 있다.

```
┌─────────────────────────┐     ┌─────────────────────────┐
│  stardustfs daemon       │     │  stardustfs <cmd>        │
│  (상주, 온라인 피어)      │     │  (단발 CLI)              │
│  - device 등록/heartbeat │     │  - ls/df/status         │
│  - P2P 서버 + 릴레이 워커 │     │  - put/get/rm/mkdir/... │
│  - 주기/롱폴 메타 동기화  │     │  - devices/login/logout │
└───────────┬─────────────┘     └───────────┬─────────────┘
            │   메타데이터 SQLite (WAL, 동시 접근)   │
            └───────────────┬───────────────────────┘
                            ▼
                  {metadata_db} (.credentials/.daemon/.syncstate)
```

- daemon(`stardustfs.py daemon`): 이 device를 온라인 피어로 유지한다. device 등록,
  P2P 서버, 릴레이 워커, heartbeat, 주기/version 롱폴 동기화를 수행한다. 생존·종료는
  제어 파일 기반 라이프사이클(start/status/stop)로 관리한다(POSIX 시그널 비의존).
- CLI(`stardustfs.py <cmd>`): 단발 명령. 로컬 명령(ls/df/status)은 서버 없이 동작하고,
  온라인 명령(devices/get/put 등)은 저장된 토큰으로 서버에 접근한다. CLI는 device를
  재등록하지 않고(daemon이 소유), 메타데이터 DB를 WAL로 daemon과 공유한다.

## 클라이언트 핵심 컴포넌트 (stardustlib/)

- `jbod_manager.py`: JBOD 스토리지 통합. 파일 읽기/쓰기 라우팅의 중심.
  `read_file`은 metadata의 device_id로 로컬/원격을 분기한다(원격은 P2P/릴레이 fetch
  후 로컬 복호화). `write_file`은 로컬 소유는 덮어쓰기, 원격 소유는 소유권 이전
  (takeover) + orphan GC.
- `metadata_store.py`: SQLite 메타데이터(SQLCipher 가능 시 암호화, 아니면 평문 폴백).
  WAL 모드. files 테이블에 device_id/version/sync_status/deleted(tombstone).
- `storage_source.py`: `DirectorySource`/`LoopbackSource`(로컬) + `is_remote` 속성.
  LoopbackSource는 `<path>.d/` 동반 디렉토리에 `<hex32>_<name>`으로 저장. 신규 소스
  추가(attach)는 loopback만 허용(디렉터리 타입 폐지, 기존 설정은 하위호환 로드).
- 스토리지 detach(분리)는 "evacuate 후 분리": `jbod.evacuate_source`가 그 소스의 활성
  파일을 남은 로컬 소스로 at-rest 암호문 그대로 이동(대상 기록 성공 후 원본 삭제=무손실)
  하고, 모든 파일이 이동된 경우에만 설정에서 소스를 제거한다(원자적). 로컬 용량 부족분은
  온라인 리모트 디바이스(같은 사용자)로 분산 이동한다(`_evacuate_to_remote`: 암호문
  블록을 /p2p/write로 push → 메타 device_id/source_id 갱신 → 원본 삭제). 도달 가능한
  대상이 없으면 미이동으로 남겨 detach 보류(타 사용자 리모트는 후속).
- `remote_source.py`: 원격 디바이스 프록시. 전용 백그라운드 이벤트 루프에서 P2P
  직접 연결 + 실패 시 릴레이 fallback. 동기 메서드로 자가 브리지.
- `p2p_server.py` / `relay_client.py` / `relay_worker.py`: P2P 서버와 서버 경유 릴레이.
- `sync_client.py`: 메타데이터 동기화(주기 폴링 + version 롱폴 + CAS + orphan GC).
- `device_manager.py`: device 등록/heartbeat/UPnP/reflexive 공인 IP 보정.
- `auth_client.py` + `credential_store.py`: 토큰 인증. 토큰을 자격증명 저장소
  (`{metadata_db}.credentials.json`, 소유자 전용)에 영속화하고 만료 전 자동 갱신.
- `daemon.py`: daemon 라이프사이클(제어 파일 + 정지 센티넬 + heartbeat).
- `cli/`: 단발 명령 디스패처/세션/명령/출력 포매터.
- 리플리케이션(MVP3): `chunker.py`(암호문 4MiB 청크 split/join), `parity_store.py`
  (호스트 역할 — 타 사용자 청크 암호문 보관·쿼터·소유자 인가), `replication_manager.py`
  (replicate/recover/ensure_replicas), `holepunch.py`(UDP 동시 오픈). P2P
  `/p2p/replica_{store,fetch,delete}`는 교차 사용자 토큰을 검증하되 소유자=요청자
  인가는 ParityStore가 청크 단위로 집행.

## 중앙 서버 (별도 저장소 ../stardustfs-server, FastAPI)

- 인증: `/auth/register`·`/auth/login`(access 15분 + refresh 30일 회전)·`/auth/refresh`·
  `/auth/logout`(refresh 취소)·`/auth/verify`(P2P 토큰 위임 검증).
- device: `/devices` 등록/목록(같은 (user, name, os)면 재사용).
- 메타데이터 백업/동기화: 암호화 blob + version(CAS 낙관적 잠금, version 롱폴).
- key 백업: 마스터키를 key_password로 암호화한 blob 보관(zero-knowledge).
- 라우팅/릴레이: 디바이스 접속 주소 조회, 직접 연결 불가 시 HTTP 롱폴 릴레이.
- 리플리케이션 제어 평면: `/replication/*`(청크 레지스트리·배치·복제본·건강성),
  chunks/replicas/hosting 테이블 + 호혜 회계(provided·hosted, 0.5 호혜 쿼터).
  위치/크기/회계 메타데이터만 저장(내용·키 미저장). UDP 랑데부(`rendezvous.py`,
  옵트인)로 홀펀칭 보조.

## 데이터 흐름

### 업로드 (put)
1. CLI가 로컬 파일을 읽는다.
2. `write_file` → AES-256-GCM 암호화 → 소스 선택 후 저장 → 메타데이터 등록(pending).
3. `upload_metadata`로 암호화된 메타데이터 blob을 서버에 업로드(CAS).

### 다운로드 (get)
1. `read_file`가 metadata의 device_id로 소유 device 판정.
2. 로컬 소유면 로컬 소스에서 읽고, 원격 소유면 remote_source가 P2P(실패 시 릴레이)로
   암호문을 fetch.
3. 로컬 encryption_engine으로 복호화(같은 계정 = 같은 master_key) 후 로컬 파일 저장.

### 동기화
- daemon이 version 롱폴로 변경을 즉시 감지해 다운로드·병합하고, 주기 폴링을
  안전망으로 둔다. 삭제는 tombstone으로 전파되고 만료 tombstone은 GC된다.

### 리플리케이션 (backup/restore/heal)
호스트(홀더)는 자기 디바이스뿐 아니라 다른 사용자의 디바이스도 포함하는 상호
(reciprocal) 피어 네트워크다. 각 사용자는 호혜 비율(무료 0.5·provided)만큼 타인의
암호문을 보관해 주고, 그 대가로 자신의 암호문이 타인 기기에 보관된다. 호스트는
키가 없어 보관 중인 청크를 복호화할 수 없다.

- backup: 평문을 AES-256-GCM으로 자체 포함 암호문 blob으로 암호화 → 4MiB 청크 분할
  → 청크 등록 + 서버 배치(placement: 용량·온라인·호혜 균형, 소유자 자신 device는 제외)
  → 각 청크를 홀더의 ParityStore에 push → 레지스트리 확정. 모든 청크가 목표
  복제본 수(`min_replicas`, 기본 1=원본 외 1부)를 확보하면 replicated, 아니면
  pending(경고). file_ref/chunk_id는 가상경로 SHA-256(서버에 경로 비노출).
- 복제본 전송: 홀더로 직접 HTTP push/fetch를 짧은 타임아웃(`DIRECT_HOLDER_TIMEOUT`,
  3s)으로 시도하고, 직접 연결 실패(NAT·이중 NAT) 시 같은 사용자 릴레이로 fallback한다
  (RemoteSource와 동일 원리). 직접 비-200(쿼터 등)은 릴레이하지 않는다.
- restore: 서버에서 청크 목록 조회 → 청크별 온라인·도달 가능한 홀더에서 fetch(스웜,
  직접→릴레이) → 결합 → 복호화 → 로컬 복원. 도달 불가 청크가 있으면 누락 chunk_id
  명시 에러.
- heal: 청크별 online 복제 수가 부족하면 온라인 홀더에서 받아(불변 청크) 새 홀더로
  복사. 호스트는 키가 없어 청크를 복호화할 수 없다.
- 운영 활성화(`replication.enabled`, 기본 활성): daemon이 시작 시 GET
  `/replication/policy`로 호혜 비율·목표 복제본 수를 내려받아 적용하고(주기 갱신),
  `provided_bytes`(미설정 시 로컬 총 용량)를 서버에 신고하며 ParityStore를
  `provided*비율`로 켠다. `replication_scheduler`가 백그라운드로 미복제(none)·미달
  (pending) 로컬 파일을 자동 backup하고(제한된 동시성 `backup_concurrency`로 병렬,
  한 파일이 연결 타임아웃을 기다리는 동안 다른 파일 진행), replicated가 degraded로
  떨어진 채 유예(`heal_grace_seconds`, 기본 24h) 이상 지속되면 heal한다. 파일 단위
  실패 격리, 로컬에 없는 파일(타 device 소유)은 건너뛰어 캐시. MetadataStore는
  스레드별 sqlite 연결 + WAL이라 병렬 백업이 안전하다. CLI `backup`/`restore`/`heal`은
  수동 경로로 항상 사용 가능.

## 설계 결정 (요약)

- zero-knowledge 유지: 서버는 암호문 + version 정수만, 호스트는 복호화 불가능한
  청크 암호문만.
- P2P/릴레이 전송과 메타데이터 접근은 같은 사용자 디바이스 간만 허용. 교차 사용자는
  암호화 복제 청크의 상호 호스팅만 허용(소유자=요청자 인가는 ParityStore가 청크
  단위 집행). 평문 사용자 간 공유는 폐기.
- 클라이언트 구동 중에만 파일 접근 가능(오프라인 device는 변경 불가) → 소유권 이전
  충돌 없음.
- 실패 시 graceful 건너뛰기 금지: 오프라인/용량부족 등은 규격 에러/종료 코드로 반환.

상세 현황·작업 규칙은 [HANDOVER.md](./HANDOVER.md), 제품 방향은 [ROADMAP.md](./ROADMAP.md)
참조.
