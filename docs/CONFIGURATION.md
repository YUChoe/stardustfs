# 설정 가이드

StardustFS 클라이언트는 JSON 설정 파일 하나(`--config`로 지정)와 별도의 마스터 키
파일로 구성된다. 스키마 버전은 2이며, v1(WebDAV 시절)은 daemon이 거부한다.

로더·검증 구현: [stardustlib/config_loader.py](../stardustlib/config_loader.py).
값을 추측하지 말고 이 파일의 상수·검증 함수를 확인할 것.

## 전체 예시

```json
{
  "version": 2,
  "server": {
    "url": "https://stardustfs.noizze.net",
    "device_name": "desktop-01"
  },
  "sources": [
    {
      "type": "loopback",
      "id": "loopback-2ce2ec",
      "path": "C:/Users/me/StardustFS/storage_001.img",
      "size": 1048576000
    }
  ],
  "metadata_db": "C:/Users/me/StardustFS/metadata.db",
  "key_file": "C:/Users/me/StardustFS/master.key",
  "sync": {
    "interval_seconds": 30,
    "conflict_strategy": "copy"
  },
  "p2p": {
    "port": 9090,
    "enabled": true
  }
}
```

최상위 필수 필드와 v2 필수 섹션(`server`, `sync`, `p2p`)만 있으면 동작한다. 나머지
섹션(`replication`, `eviction`)과 각 섹션의 선택 키는 생략 시 아래 표의 기본값이
적용된다.

## 최상위 필드

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `version` | int | 예 | `2`. `1`도 로드는 되지만 daemon이 `v2 필요`로 종료한다 |
| `sources` | array | 예 | 스토리지 소스 1개 이상 |
| `metadata_db` | string | 예 | 메타데이터 SQLite 파일 경로. 없으면 생성 |
| `key_file` | string \| null | 예(널 허용) | 32바이트 마스터 키 파일 경로. `null`이면 환경변수 `STARDUST_KEY` 사용 |

`metadata_db` 경로를 접두사로 부속 파일이 만들어진다.

| 파일 | 용도 |
|------|------|
| `<metadata_db>.credentials.json` | 액세스/리프레시 토큰 + 마스터키 백업 암호(소유자 전용 권한) |
| `<metadata_db>.daemon.json` | daemon 제어 파일(pid·heartbeat) |
| `<metadata_db>.daemon.ctl.json` | 전송 위임 제어 채널(포트·토큰, 0600) |
| `<metadata_db>.daemon.log` | GUI가 기동한 daemon의 표준출력/오류 |

`key_file`이 존재하지 않아도 `server.url`이 설정돼 있으면 검증을 통과한다 — daemon이
서버 백업(`GET /sync/key`)에서 복원한다. 상세는
[NEW_DEVICE_SETUP.md](./NEW_DEVICE_SETUP.md).

## `server`

| 필드 | 타입 | 기본 | 설명 |
|------|------|------|------|
| `url` | string \| null | — | 중앙 서버 주소. `https://` 스킴 + 호스트명 필수. `null`이면 오프라인 전용(동기화·P2P·복제 없음) |
| `device_name` | string | — | 이 기기의 이름(1~64자). 서버는 `(user, name, os)`로 device를 식별하므로 기기마다 다르게 지정한다 |

## `sources`

소스는 3종이다. `directory`/`loopback`은 로컬, `remote`는 내 다른 기기를 가리킨다.

### `directory`

| 필드 | 타입 | 설명 |
|------|------|------|
| `type` | `"directory"` | — |
| `id` | string | 소스 고유 ID |
| `path` | string | 절대 경로. 기동 시 존재 + 읽기/쓰기 권한이 있어야 한다 |

신규 추가(attach)는 GUI에서 `loopback`만 허용한다. 기존 `directory` 소스는 계속
로드된다(하위 호환).

### `loopback`

| 필드 | 타입 | 설명 |
|------|------|------|
| `type` | `"loopback"` | — |
| `id` | string | 소스 고유 ID |
| `path` | string | 이미지 파일 절대 경로 |
| `size` | int | 이미지 크기(바이트). 10 MiB(10485760) ~ 2 TiB(2199023255552) |

`path`를 고정 크기 FAT 이미지(pyfatfs)로 포맷해 파일을 이미지 내부에 저장한다. 이미
FAT면 재포맷하지 않고 마운트한다. 쓰기는 daemon 단독이고, GUI/CLI 조회 세션은 같은
이미지를 읽기 전용으로 연다.

### `remote`

| 필드 | 타입 | 설명 |
|------|------|------|
| `type` | `"remote"` | — |
| `id` | string | 소스 고유 ID |
| `device_id` | string | 대상 device의 RFC 4122 UUID |

명시하지 않아도 `p2p.auto_mount_devices`(기본 true)로 내 다른 device가 자동
마운트되므로, 보통 이 타입을 직접 쓸 필요는 없다.

## `sync` (v2 필수)

| 필드 | 타입 | 설명 |
|------|------|------|
| `interval_seconds` | int | 주기 동기화 간격. 10~3600 |
| `conflict_strategy` | string | `"copy"`만 허용(충돌 시 사본 생성) |

daemon은 이 주기 폴링과 함께 version 롱폴로 변경을 즉시 감지한다.

## `p2p` (v2 필수)

| 필드 | 타입 | 기본 | 설명 |
|------|------|------|------|
| `port` | int | — | P2P TCP 포트. 1024~65535 |
| `enabled` | bool | — | 피어 서빙 활성. 서버 정책이 금지하면 설정과 무관하게 비활성 |
| `relay_enabled` | bool | true | 릴레이 워커(직접 연결 불가 환경의 수신 경로) |
| `rendezvous_port` | int | 9091 | 홀펀칭 랑데부 UDP 포트(호스트는 `server.url`에서 도출) |
| `auto_mount_devices` | bool | true | 내 다른 device를 remote 소스로 자동 마운트 |

## `replication` (선택)

암호화 복제(백업 카피). 섹션을 생략하면 아래 기본값으로 활성이다.

| 필드 | 타입 | 기본 | 설명 |
|------|------|------|------|
| `enabled` | bool | true | 복제·호스팅 기능 전체 |
| `target_copies` | int | 3 | 목표 카피 수. 서버 정책(`GET /replication/policy`)이 내려주면 그 값이 우선 |
| `backup_interval_seconds` | int | 300 | 미복제·미달 파일 스캔 주기 |
| `heal_interval_seconds` | int | 3600 | heal(카피 보충·기기 분산) 주기 |
| `heal_grace_seconds` | int | 86400 | replicated가 degraded로 떨어진 뒤 재복제까지 유예 |
| `max_files_per_cycle` | int | 20 | 한 주기에 처리할 최대 파일 수 |
| `backup_concurrency` | int | 4 | 동시 백업 파일 수 |
| `policy_interval_seconds` | int | 3600 | 서버 복제 정책 재조회 주기 |

이 기기가 남의 청크를 보관하는 상한(`hosting_quota_bytes`)은 서버가 정한다. 설정으로
지정하지 않으며, 정책을 한 번도 받지 못하면 0으로 두어 타 사용자 청크를 받지 않는다.

## `eviction` (선택, 기본 꺼짐)

로컬 여유가 부족할 때 카피가 충분한 파일의 로컬 청크를 비운다(`replication.enabled`가
true일 때만 동작).

| 필드 | 타입 | 기본 | 설명 |
|------|------|------|------|
| `enabled` | bool | false | 축출 루프 활성 |
| `interval_seconds` | int | 300 | 여유 공간 점검 주기 |
| `low_watermark_bytes` | int | 209715200 (200 MiB) | 이 값 미만이면 축출 시작 |
| `high_watermark_bytes` | int | 524288000 (500 MiB) | 이 값까지 회복하도록 필요분만 축출 |

삭제 직전에 서로 다른 기기의 카피 수를 실측해 `target_copies` 미만이면 보존한다.
정책 상세는 [DISTRIBUTION_POLICY.md](./DISTRIBUTION_POLICY.md).

## 암호화 키

- 정확히 32바이트(256비트). 다른 길이면 `InvalidKeyError`.
- 로드 우선순위: `key_file` 경로 → (`key_file`이 `null`일 때) 환경변수 `STARDUST_KEY`.
  `key_file`을 지정했는데 파일이 없으면 `KeyNotFoundError`(daemon은 서버 복원을 먼저
  시도한다).
- 메타데이터 DB 키는 마스터 키에서 HKDF-SHA256으로 파생한다
  (`salt=stardustfs-metadata-db`).
- 첫 기기에서 생성:

  ```bash
  python -c "import os; open('master.key','wb').write(os.urandom(32))"
  ```

## 기동 시 검증

`ConfigLoader.validate()`가 아래를 검사하고, 실패 항목을 모두 로그에 남긴 뒤 종료한다.

1. 설정 파일 존재 + JSON 파싱
2. `version`이 1 또는 2 (daemon은 2만 허용)
3. v2 필수 섹션(`server`/`sync`/`p2p`) 존재
4. `server.url`이 `https://` + 호스트명, `server.device_name` 1~64자
5. `sync.interval_seconds` 10~3600, `sync.conflict_strategy == "copy"`
6. `p2p.port` 1024~65535, `p2p.enabled`가 boolean
7. `sources` 1개 이상, 타입별 규칙(디렉터리 존재·권한, 루프백 절대경로·크기 범위,
   remote `device_id` UUID 형식)
8. `key_file` 존재(단, `server.url`이 있으면 미존재 허용 — 서버 복원)
9. 마스터 키 32바이트 + 메타데이터 DB 연결

## v1 → v2 마이그레이션

v1 설정은 `ConfigLoader.migrate_v1_to_v2()`로 변환한다.

- 원본을 `<원본>.v1.bak`으로 백업한다(이미 있으면 `.v1.bak.1`, `.2`, … 순번).
- 레거시 `webdav` 섹션을 제거하고 `server`(url=null)/`sync`/`p2p` 기본값을 추가한다.
- 백업 또는 저장이 실패하면 `ConfigMigrationError`로 중단한다(원본 보존).

`server.url`이 `null`로 들어가므로, 서버를 쓰려면 변환 후 URL과 `device_name`을 채운다.

## 개발용 설정

저장소 루트의 `dev-config.json`이 개발 예시다(`./run-dev.sh`가 사용). 루프백 이미지를
`.dev-storage/`에 두고 개발 서버를 가리킨다.
