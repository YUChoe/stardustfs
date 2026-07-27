---
inclusion: manual
---

# 복제 소유 모델 정정 및 진행 가시성 — Design

## 개요

복제 경로에서 device를 소유자처럼 쓰던 두 지점을 고친다.

1. 백업 대상 선정: `files.device_id`(레코드 관리 기기) 기준 필터 → 로컬 청크 보유
   기준으로 전환. 소유는 사용자 단위이고 device는 보관 위치이므로, 각 device는
   "자기 스토리지에 실제로 있는 청크"만 책임진다.
2. 홀더 배치: `exclude = [자기 device]` → `exclude = [자기 device] + [그 청크 원본
   보관 device] + [보관 한도 초과 device]`.

청크는 `chunk_id = SHA256(file_ref:idx)`로 식별되고 서버 레지스트리도 청크 단위라,
device별 분담에 파일 단위 조율(락·리더 선출)이 필요 없다. 각 device가 자기 청크를
올리면 서버에서 파일 전체가 자연히 채워진다.

진행 가시성은 (a) 읽기·전송 양쪽 단계의 로그, (b) daemon 제어 채널의 진행 스냅샷을
GUI가 기존 3초 폴링에서 함께 읽는 방식으로 붙인다.

## Components and Interfaces

### ReplicationManager (stardustlib/replication_manager.py)

```python
class ChunkOrigin(NamedTuple):
    """청크의 물리 보관 위치. device_id=None이면 이 device 로컬."""
    index: int
    device_id: str | None

def _local_chunks(self, virtual_path: str) -> list[tuple[int, bytes]]:
    """이 device 스토리지에 있는 청크만 (idx, 암호문)으로 읽는다.

    원격 device 보관 청크는 읽지 않는다(릴레이 왕복 방지). 로컬 청크가 하나도
    없으면 빈 목록을 돌려주고 호출자가 skipped로 끝낸다.
    """

def _origin_devices(self, virtual_path: str) -> dict[int, str]:
    """청크 index → 원본 보관 device_id. 로컬 청크는 자기 device_id."""

def replicate(self, virtual_path: str) -> ReplicationResult:
    """로컬 청크만 홀더에 올리고, 파일 상태는 서버 레지스트리로 판정한다."""
```

`_replicate_chunks`는 청크마다 `exclude`에 그 청크의 원본 보관 device를 더한다.
현재 코드의 `base_exclude + quota_blocked_devices()`에 `origin[idx]`를 추가하는
형태이므로 구조 변경은 작다.

### 진행 상태 (stardustlib/replication_progress.py, 신규)

```python
@dataclass
class ProgressSnapshot:
    """복제 진행 스냅샷. 사용자 데이터는 담지 않는다(경로·수치만)."""
    virtual_path: str
    stage: str          # reading|storing|idle
    done: int           # 처리한 청크 수
    total: int          # 전체 청크 수
    secured: int        # 목표 카피 수를 확보한 청크 수(chunk-copy-policy: target_copies)
    started_at: float   # monotonic

class ProgressTracker:
    """진행 상태를 메모리에 보관한다(파일 기록 없음, 스레드 안전).

    ReplicationManager가 청크 단위로 갱신하고, 제어 채널이 스냅샷을 읽는다.
    """
    def begin(self, virtual_path: str, total: int, stage: str) -> None: ...
    def advance(self, done: int, secured: int) -> None: ...
    def finish(self) -> None: ...
    def snapshot(self) -> ProgressSnapshot | None: ...
```

ReplicationManager는 선택적 `progress: ProgressTracker | None`을 받는다(미주입 시
no-op). CLI 단발 경로는 주입하지 않아 기존 동작 그대로다.

### 제어 채널 (stardustlib/daemon_control.py)

```
POST /ctl/progress   (X-Ctl-Token)
  → 200 {"active": bool, "path": str, "stage": str,
         "done": int, "total": int, "secured": int, "elapsed": float}
```

진행이 없으면 `{"active": false}`. 라우트는 기존 `/ctl/get`·`/ctl/announce`와 같은
토큰 인증을 쓰고 127.0.0.1에만 바인딩한다.

### GUI (stardustlib/gui/app.py, actions.py)

`_poll_meta`(3초 주기)에서 `actions.replication_progress(config_path)`를 함께
호출해 진행이 있으면 상태바에 표시한다. 조회 실패·데몬 미실행은 무시하고 기존
표시를 유지한다. 수동 백업 요청(`_backup_selected`)은 Requirement 3에 따라
청크 보관 device를 확인해 위임 경로(`announce`)를 우선한다.

## Data Models

기존 스키마를 그대로 쓴다(마이그레이션 없음).

- `file_chunks(virtual_path, chunk_index, chunk_ref, source_id, device_id, size, hash)`
  — `device_id`가 청크의 물리 보관 device. NULL이면 로컬.
- `files(..., device_id, replication_status, ...)` — `device_id`는 레코드 관리
  기기로 유지하되 복제 대상 선정에는 쓰지 않는다.
- 서버 `chunks`/`replicas`/`hosting` 테이블 변경 없음.

`MetadataStore`에 조회 하나를 더한다.

```python
def list_paths_with_local_chunks(
    self, statuses: tuple[str, ...], device_id: str
) -> list[str]:
    """이 device에 청크가 하나라도 있는 파일 경로(복제 상태 필터).

    `file_chunks.device_id = ? OR IS NULL`(로컬) 기준. 청크 레코드가 없는 파일은
    대상에서 제외한다 — 물리 데이터가 없는 device는 백업을 맡지 않는다.
    """
```

## Correctness Properties

### Property 1: 소유는 사용자, 실행은 보관 위치

*임의의* 파일 f와 device d에 대해, d가 f의 백업을 수행하면 d는 f의 청크를 최소 1개
로컬에 보관한다. 반대로 f의 어떤 청크도 d에 없으면 d는 f를 대상으로 삼지 않으며,
원격 읽기를 시도하지 않는다.

### Property 2: 원본 기기에는 사본을 만들지 않는다

*임의의* 청크 c에 대해, c의 원본을 보관한 device는 c의 홀더가 되지 않는다. 따라서
placement 결과에 그 device가 포함되지 않고, 이미 그런 replica가 등록돼 있으면
heal이 그것을 유효 복제본으로 세지 않는다.

### Property 3: 릴레이 왕복 없음

*임의의* 백업 수행에서, 전송된 바이트는 로컬 스토리지에서 읽은 청크뿐이다. 원격
device의 청크를 읽기 위한 `read_chunk` 릴레이 호출은 0이다(복구·읽기 경로는 예외).

### Property 4: 진행은 단조 증가하고 종료 시 정리된다

*임의의* 복제 수행에서 `done`은 0에서 시작해 `total`까지 단조 증가하며, 수행이
끝나면(성공·실패·예외 무관) 스냅샷은 `active=false`가 된다.

### Property 5: 진행 조회는 데이터를 노출하지 않는다

`/ctl/progress` 응답은 가상 경로와 수치만 포함하고 파일 내용·키·토큰을 담지 않는다.

## Error Handling

- 로컬 청크가 하나도 없으면: `replicate`는 `ReplicationResult(status="skipped",
  chunk_count=0)`을 돌려주고 상태를 바꾸지 않는다. 호출자(수동 요청)는 위임 경로로
  넘어간다.
- 위임 대상 device가 오프라인이면: `announce` 대신 GUI 상태바에 "위임 실패:
  {device} 오프라인"을 표시하고 pending을 유지한다. 예외를 던지지 않는다.
- 제외 후 후보 홀더가 없으면: pending + WARNING 1회(홀더별 중복 억제).
- `/ctl/progress` 조회 실패(데몬 미실행·타임아웃 2초): GUI는 진행 표시를 생략한다.
- 진행 추적 자체의 예외는 복제를 중단시키지 않는다(추적은 부가 기능).

## Testing Strategy

- 단위: `_local_chunks`가 원격 device 청크를 건너뛰는지, `_origin_devices`가
  청크별 보관 device를 정확히 돌려주는지(fake MetadataStore).
- 단위: placement `exclude`에 원본 보관 device가 포함되는지, 제외 후 후보가 없으면
  pending + 경고 1회인지.
- 단위: `ProgressTracker`의 단조 증가·종료 정리(Property 4), 스냅샷 필드(Property 5).
- 통합: 청크가 두 device에 나뉜 파일에서 각 device가 자기 청크만 올려 서버 레지스트리
  기준으로 파일이 replicated가 되는지(인메모리 cloud fake).
- 회귀: 청크가 모두 로컬인 파일의 기존 백업 경로가 그대로인지.
- 회귀: `read_chunk` 릴레이 호출 수가 0인지(Property 3, fake RemoteSource 카운터).
