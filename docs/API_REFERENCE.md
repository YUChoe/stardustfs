# API 레퍼런스

`stardustlib/`의 주요 공개 진입점을 모듈별로 정리한다. 전체 목록이 아니라 다른
모듈에서 호출하는 인터페이스 위주이며, 상세 동작은 각 파일의 독스트링을 본다.
아키텍처 맥락은 [ARCHITECTURE.md](./ARCHITECTURE.md).

## 엔트리포인트 — `stardustfs.py`

| 함수 | 설명 |
|------|------|
| `main()` | argparse 진입점. 서브커맨드 없으면 GUI(인자 없음) 또는 daemon(`--config` 지정) |
| `startup_v2(config, config_path)` | daemon 초기화 시퀀스: 인증 → 키 복원 → 코어 조립 → 키 백업 → device 등록 → 복제 정책 → 동기화 → P2P/홀펀칭/릴레이 → 스케줄러 → 제어 채널 |
| `_build_core(config, *, read_only=False)` | `(storage_pool, metadata_store, encryption_engine, db_key)` 조립. 실패 시 `sys.exit(1)` |
| `_build_local_sources(config, *, read_only=False)` | 설정의 로컬 소스(directory/loopback) 생성·initialize |
| `_mount_remote_sources(...)` | 설정의 remote 소스 + 내 다른 device 자동 마운트 |
| `_restore_key_from_server(auth_client, key_file_path, logger)` | `GET /sync/key` blob을 백업 암호로 복호화해 키 파일 생성 |
| `_backup_key_to_server(auth_client, key_file_path, logger)` | 키를 백업 암호로 암호화해 업로드(서버에 이미 있으면 유지) |

`_build_core`/`_mount_remote_sources`/`_restore_key_from_server`는 CLI 세션과 GUI 백엔드도
호출하는 내부 공유 함수다.

## 스토리지

### `storage_pool.StoragePool`

파일 읽기/쓰기 라우팅의 중심. 가상 경로 → 청크 → (소스, 기기) 배치를 담당한다.

| 메서드 | 설명 |
|--------|------|
| `write_file(virtual_path, data)` | 4 MiB 청크 분할 → 청크별 암호화·배치 → 전부 성공 시 매니페스트 커밋 |
| `read_file(virtual_path) -> bytes` | 청크 매니페스트를 따라 로컬/원격에서 모아 복호화 |
| `read_range(virtual_path, offset, length)` | 범위를 덮는 청크만 가져와 부분 반환 |
| `read_chunks(...)` / `write_chunks(...)` | at-rest 암호문 청크를 그대로 주고받는 복제용 경로 |
| `read_ciphertext` / `write_ciphertext` | 파일 단위 암호문 입출력(레거시 blob 경로) |
| `store_chunk_copy(...)` / `read_chunk_copy(...)` / `delete_chunk_copy(...)` | 복제 카피 단위 조작 |
| `migrate_to_chunks(virtual_path) -> bool` | 레거시 통짜 blob을 청크 표현으로 무손실 전환 |
| `delete_file` / `move_file` / `copy_file` / `file_exists` / `get_file_info` | 파일 연산 |
| `create_directory` / `delete_directory` / `move_directory` / `list_directory` | 디렉터리 연산 |
| `select_source(size, ...)` | 로컬 활성 소스 중 여유 최대 선택(원격 제외). 부족하면 `InsufficientStorageError` |
| `add_source` / `replace_local_sources` / `deactivate_source` / `close_local_sources` | 소스 라이프사이클(설정 리로드) |
| `evacuate_source(source_id) -> dict` | 소스의 활성 파일을 남은 로컬/원격으로 이동(detach 전 단계) |
| `evict_cold(is_safe, bytes_to_free) -> dict` | 카피가 충분한 콜드 파일의 로컬 청크 비움 |
| `gc_orphan_files()` / `gc_orphan_files_if_needed()` | orphan 물리 파일 정리(관리 파일만) |
| `get_total_space()` / `get_available_space()` | 용량 집계(로컬 소스) |
| `register_remote_device(device_id, remote)` | 원격 기기 프록시 등록 |

### `storage_source`

`StorageSource` 추상 클래스와 구현. 공통 인터페이스: `initialize`, `read`, `write`,
`write_chunk`, `read_chunk`, `delete`, `exists`, `list_physical_files`,
`get_total_space`, `get_available_space`, `is_remote`.

| 클래스 | 설명 |
|--------|------|
| `DirectorySource` | 디렉터리를 소스로 사용 |
| `LoopbackSource` | `<path>`를 고정 크기 FAT 이미지(pyfatfs)로 포맷해 내부에 저장. 쓰기는 daemon 단독, 조회는 `read_only` |

`remote_source.RemoteSource`는 같은 인터페이스를 구현하는 원격 프록시로,
`is_remote=True`이며 전용 이벤트 루프에서 직접 TCP → 홀펀칭 UDP → 릴레이 캐스케이드로
op를 보낸다(`set_udp_transport`, `refresh`, `push_blob`).

### `chunker`

| 함수 | 설명 |
|------|------|
| `split(blob, size=DEFAULT_CHUNK_SIZE) -> list[(index, bytes)]` | 청크 분할(기본 4 MiB) |
| `join(parts) -> bytes` | 인덱스 순서로 결합 |
| `chunk_count(total_size, size=...) -> int` | 청크 개수 |
| `chunk_range(offset, length, size=...)` | 범위를 덮는 청크 인덱스 |
| `chunk_hash(data) -> str` | 암호문 SHA-256 hex(무결성 검증용) |
| `chunk_ref(index) -> str` | 청크 참조 문자열 |
| `shard_prefix(...)` / `shard_depth_for(...)` | 해시 앞자리 샤딩 경로·깊이(FAT 엔트리 폭증 회피) |

### `encryption_engine.EncryptionEngine`

AES-256-GCM. `encrypt(plaintext)`, `decrypt(encrypted_data)`,
`encrypt_stream(in, out)`, `decrypt_stream(in, out)`, 정적 `generate_iv()`,
`validate_key(key)`.

## 메타데이터

### `metadata_store.MetadataStore`

SQLite(SQLCipher 가능 시 암호화, 아니면 표준 sqlite3 폴백) + WAL. 스레드별 연결.

주요 그룹:

- 초기화·트랜잭션: `initialize`(스키마 마이그레이션 v2~v8 포함), `begin_transaction`,
  `commit`, `rollback`, `close`
- 파일 레코드: `insert`, `update`, `delete`(tombstone), `lookup`, `lookup_any`,
  `rename_path`, `get_pending_files`, `list_files_in_source`
- 디렉터리: `insert_directory`, `lookup_directory`, `delete_directory_entry`,
  `list_entries`, `rename_directory`
- 청크 배치(`file_chunks`, 위치 정본): `put_chunks`, `get_chunks`,
  `get_chunk_locations`, `add_chunk_location`, `remove_chunk_location`,
  `update_chunk_location`, `delete_chunks`, `live_chunk_paths_for_device`,
  `list_chunked_paths_in_source`, `list_paths_with_local_chunks`
- 보관 청크(타 사용자, `hosted_chunks`): `put_hosted_chunk`, `get_hosted_chunk`,
  `delete_hosted_chunk`, `list_hosted_chunks`, `hosted_bytes`
- 상태·동기화: `increment_version`, `set_sync_status`, `set_replication_status`,
  `get_replication_status`, `list_virtual_paths_for_replication`
- 축출: `mark_evicted`, `list_eviction_candidates`
- tombstone GC: `list_expired_tombstones`, `purge_expired_tombstones`
- 스키마는 추측하지 말고 `CREATE TABLE`/`PRAGMA table_info`를 확인한다
  (`scripts/dump_schema.py`).

`metadata_records.py`는 레코드 단위 증분 동기화용 직렬화(record_id HMAC, 256B 패딩)를
담당한다.

### `sync_client.SyncClient`

| 메서드 | 설명 |
|--------|------|
| `initial_sync()` | 서버 메타데이터 다운로드·병합(1회) |
| `start_periodic_sync()` | 주기 폴링 + version 롱폴 루프 시작 |
| `upload_metadata()` | pending 변경을 CAS로 업로드 |
| `mark_dirty()` | 즉시 업로드가 필요한 변경 표시 |
| `reconcile_if_stale() -> bool` | 장기 오프라인 후 재조정 |
| `upload_key(blob)` / `download_key()` | 마스터키 백업 blob 업/다운로드 |
| `stop()` | 루프 정지 |

`conflict_resolver.ConflictResolver`가 충돌 시 사본 생성 전략(`copy`)을 수행한다.

## 인증·디바이스

### `auth_client.AuthClient`

`login(email, password)`, `refresh_token()`, `get_valid_token()`, `logout()`,
`load_from_store()`, `set_key_password(pw)`, 속성 `is_authenticated`, `is_offline`,
`user_id`, `key_password`, `close()`.

토큰은 `credential_store.CredentialStore`(`<metadata_db>.credentials.json`,
소유자 전용)에 영속화된다: `load`, `save`, `clear`, `exists`, `path`, `lock_path`,
그리고 동시 갱신 직렬화용 `file_lock(lock_path, timeout)`.

### `device_manager.DeviceManager`

`register()`, `list_devices()`, `report_sources(sources)`, `list_all_sources()`,
`start_source_report(...)`, `start_heartbeat()`, `stop()`,
`get_connection_address()`/`set_connection_address(addr)`, 속성 `device_id`,
`is_offline`, `heartbeat_interval`, `consecutive_failures`.

### `online_recovery.OnlineRecoveryManager`

오프라인 모드에서 주기(기본 60초) 복구 시도: `start()`, `stop()`, 속성 `is_recovered`.
복구 순서는 인증 → device 등록 → pending 업로드 → 메타데이터 병합 → P2P 시작 →
heartbeat → 주기 동기화.

### `key_backup_engine.KeyBackupEngine`

`encrypt_for_backup(master_key, password) -> bytes`,
`decrypt_from_backup(blob, password) -> bytes`. PBKDF2-SHA256 600,000회 파생 +
AES-256-GCM. blob 구조는 `salt(16) + iv(12) + tag(16) + ciphertext`.

## 전송

| 모듈 | 공개 진입점 |
|------|-------------|
| `p2p_server.P2PServer` | `start`/`stop`, `handle_*`(HTTP 핸들러), `dispatch(op, payload)`·`dispatch_async`(릴레이·UDP 공용), `set_backup_announcer(fn)` |
| `relay_client` / `relay_worker` | 요청자 측 릴레이 전송 / 대상 측 `/relay/poll` 워커 |
| `rudp` | 순수 파이썬 신뢰성 UDP(프래그먼트·ACK·재전송·송신 윈도우) |
| `p2p_udp` | rudp 위 P2P op 요청/응답(REQ/RESP + JSON) |
| `holepunch_service.HolePunchService` | 공유 UDP 소켓에서 랑데부 제어 + rudp 다중화. `start`, `send_op(device_id, op, payload)`, 속성 `reflexive` |
| `holepunch` | UDP 동시 오픈(복제 경로) |
| `daemon_control.DaemonControlServer` | 127.0.0.1 제어 채널. `POST /ctl/put`·`/ctl/get`·`/ctl/announce`·`/ctl/progress`·`/ctl/storage` (모두 `X-Ctl-Token` 인증). GUI/CLI 전송을 daemon에 위임 |

상세 캐스케이드·인가 규칙은 [TRANSPORT.md](./TRANSPORT.md).

## 복제

### `replication_manager.ReplicationManager`

| 메서드 | 설명 |
|--------|------|
| `replicate(virtual_path) -> ReplicationResult` | 청크 등록 → 배치 → 홀더 push → 레지스트리 확정 |
| `recover(virtual_path) -> int` | 홀더에서 청크 fetch·검증 후 로컬 복원 |
| `ensure_replicas(virtual_path) -> HealReport` | 부족한 카피 보충·기기 분산 이전 |
| `replication_health(virtual_path) -> HealthSummary` | 카피 수·기기 수 요약 |
| `target_copies` / `set_target_copies(n)` | 목표 카피 수(서버 정책 반영) |
| `set_udp_transport(fn)` | 홀더 전송에 홀펀칭 UDP 주입 |
| `quota_blocked_devices` | 507(보관 한도 초과)로 일시 배제된 홀더 |

### 그 외

- `replication_scheduler.ReplicationScheduler`: 백업/heal/정책 루프. `start`, `stop`,
  `announce(vpath)`(저장·수정 직후 즉시 백업). 기본값은
  [CONFIGURATION.md](./CONFIGURATION.md)의 `replication` 표 참조.
- `parity_store.ParityStore`: 타 사용자 청크 보관소(쿼터·소유자 인가·레거시 디렉터리
  이관). `set_max_bytes`, `migrate_legacy_dir`.
- `replication_hosting.fetch_policy(auth, server_url, device_id=None)`: 서버 복제 정책
  조회(목표 카피 수·호스팅 상한·P2P/호스팅 허용).
- `replication_progress`: 백업 진행 추적. `ProgressTracker`(메모리, 단계·처리/전체 청크)와
  `ProgressSnapshot`.
- `chunk_location`: 청크 위치 표현. `ChunkLocation`, `distinct_devices(locations)`,
  `copies(locations)`, `has_location(...)`.

## 데몬

`daemon.py`는 제어 파일 기반 라이프사이클이다(POSIX 시그널 비의존).

| 함수 | 설명 |
|------|------|
| `claim(metadata_db) -> bool` | 기동 즉시 제어 파일 선점(중복 실행 방지) |
| `release_claim(metadata_db)` | 선점 반납 |
| `serve(metadata_db, cleanup)` | 상주 루프(heartbeat 갱신 + 정지/리로드 센티넬 감시) |
| `read_status(metadata_db) -> dict` | `running`/`pid`/`heartbeat_age`/`stale` |
| `signal_stop` / `request_stop` / `signal_reload` | 정지 요청·확인, 설정 리로드 신호 |

`single_instance.py`는 GUI 단일 인스턴스 락을 제공한다.

## 설정·모델·예외

- `config_loader.ConfigLoader`: `load()`, `validate() -> list[str]`,
  `migrate_v1_to_v2()`, 정적 `load_encryption_key(key_file=None, env_var="STARDUST_KEY")`.
  검증 상수·규칙은 [CONFIGURATION.md](./CONFIGURATION.md).
- `models`: 설정 TypedDict(`StardustConfig`, `StardustConfigV2`, `ServerConfig`,
  `SyncConfig`, `P2PConfig`, `DirectorySourceConfig`, `LoopbackSourceConfig`,
  `RemoteSourceConfig`)와 데이터 클래스(`FileMetadata`, `ChunkRef`, `EntryInfo`,
  `FileInfo`, `EncryptedFileHeader`).
- `exceptions`: 모두 `StardustError` 파생 — `InsufficientStorageError`,
  `DecryptionError`, `IntegrityError`, `KeyNotFoundError`, `InvalidKeyError`,
  `KeyMismatchError`, `AuthenticationError`, `SyncError`, `DeviceRegistrationError`,
  `P2PConnectionError`, `ConfigMigrationError`. 실패는 규격 예외로 올린다
  ("graceful 건너뛰기" 금지).

## 접근 계층

### `cli/`

- `dispatcher.add_subcommands(subparsers, parent)`: 서브커맨드 등록.
- `dispatcher.is_cli_command(cmd)` / `run_cli(args) -> int`: 라우팅·실행.
- `session.CLISession.open(config_path, *, read_only=False)`: 오프라인 세션(로컬 코어만).
- `session.CLISession.open_online(config_path, *, sync=True)`: 인증·device 식별·remote
  마운트·(선택) 초기 동기화까지. 실패 시 오프라인 강등(`online=False`).
- `commands`: 명령 핸들러(`cmd_ls`, `cmd_df`, `cmd_status`, `cmd_devices`, `cmd_put`,
  `cmd_get`, `cmd_rm`, `cmd_mkdir`, `cmd_mv`, `cmd_cp`, `cmd_backup`, `cmd_restore`,
  `cmd_heal`, `cmd_login`, `cmd_logout`). `format`은 출력 포매터.

### `gui/`

Tkinter 데스크톱 파일탐색기. 백엔드 동작은 `actions`가 재노출한다.

| 모듈 | 역할 |
|------|------|
| `app` | 메인 창·메뉴·폴링 루프 |
| `session`, `panel_files`, `panel_mgmt`, `statusbar`, `file_ops`, `widgets/` | 화면 영역별 구성 |
| `act_core` | 세션 개방·캐시, 온라인 실행 래퍼, 목록 조회(`browse`, `invalidate`, `metadata_mtime`, `RemotePathExists`) |
| `act_auth` | `login(config, email, password, key_password=None)`, `logout`, `is_logged_in`, `account_email` |
| `act_storage` | `create_config(base_dir, server_url, device_name, generate_key=True, p2p_port=9090)`, `list_sources`, `add_source`, `create_storage_image`, `delete_storage_image`, `detach_source` |
| `act_files` | `put_file`, `get_file`, `mkdir`, `remove_many`, `move`, `copy` (전송은 daemon 제어 채널에 위임) |
| `act_daemon` | `daemon_status`, `daemon_start`, `daemon_signal_stop`, `daemon_signal_reload` |
| `act_replication` | `backup_paths`, `restore_paths`, `heal_paths`, `announce_paths`, `replica_counts`, `replication_progress` |
| `act_inventory` | `storage_overview`, `storage_and_devices` (스토리지·디바이스 인벤토리) |
| `theme`, `window_theme`, `tray`, `i18n`, `prefs`, `worker`, `format` | 테마·트레이·다국어·환경설정·워커 스레드 |

GUI는 daemon 생존을 주기적으로 확인하고 죽어 있으면 자동 재시작한다.
