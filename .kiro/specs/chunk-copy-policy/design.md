---
inclusion: manual
---

# 청크 3카피 정책 — Design

## 개요

카피를 `(device_id, source_id)` 위치로 추상화하고, 각 청크가 서로 다른 위치 3곳에
놓이도록 유지한다. 원본과 복제본을 나누던 비대칭을 없앤다.

세 가지가 바뀐다.

1. 스키마: 클라이언트 `file_chunks`가 청크당 여러 위치를, 서버 `replicas`가 기기당
   여러 소스를 표현하도록 확장한다.
2. 배치: 다른 기기를 우선하고, 부족하면 같은 기기의 다른 소스를 쓴다. 위치 3곳을
   채우지 못하면 미달로 기록한다.
3. 호스팅 상한: 서버 `/replication/policy`가 기기별 할당량을 내려주고, 제공 용량의
   50%를 예약하는 규칙을 없앤다.

저장 형태도 통일한다. 타 사용자 청크를 별도 디렉토리(`{metadata_db}.parity/`)가 아니라
스토리지 소스에 넣어, 용량 집계와 물리 상한을 하나로 만든다(Requirement 6).
zero-knowledge 제약은 그대로다 — 타 사용자 청크는 열쇠가 없어 열 수 없고 가상 경로도
알 수 없으며, 소유자 본인 요청일 때만 내준다.

## 기존 배포 정책과의 관계

`docs/DISTRIBUTION_POLICY.md`의 4단(기본 배치 → 스필오버 → 백업 사본 → 축출)은 로컬
공간을 아끼는 방향이다. 3카피 목표는 그중 3단(백업 사본)의 목표를 바꾸는 것이고,
1단·2단 동작은 건드리지 않는다.

로컬 카피 생성에는 여유 공간 임계를 두지 않는다. 다른 기기에 여유가 없어 내
스토리지가 카피로 차는 것은 허용된 결과이고, 3카피 확보가 새 데이터 수용보다
우선한다. 임계·정리 순서를 두지 않으므로 정책이 단순해진다.

- 로컬이 차서 새 파일을 저장할 수 없으면 기존 2단 규칙대로 동작한다(내 다른 온라인
  기기로 스필오버, 불가하면 용량 부족 에러). 카피를 지워 공간을 만들지 않는다.
- 축출 판정만 고친다: 총 카피 수가 아니라 **서로 다른 기기의 카피 수**로 한다. 카피
  3개가 모두 로컬이면 비우는 순간 0이 되므로 축출 대상이 아니다. heal 이전 판정도
  같은 기준이다.

로컬 카피와 다른 기기 카피는 지위가 같다(Requirement 2.6). 위치가 같은 기기라는
이유로 먼저 지워지지 않는다.

## Components and Interfaces

### 카피 위치 (stardustlib/chunk_location.py, 신규)

```python
@dataclass(frozen=True)
class ChunkLocation:
    """청크 카피 한 벌의 위치. 카피 간 우열은 없다."""
    device_id: str          # 보관 기기
    source_id: str          # 그 기기 안의 스토리지 소스
    chunk_ref: str | None   # 로컬 소스 경로(내 기기), ParityStore면 None
    kind: str               # "source" | "parity"

def distinct_devices(locations: list[ChunkLocation]) -> int:
    """서로 다른 기기 수. 3카피가 한 기기에 몰린 상태를 구분하는 기준."""
```

### MetadataStore

```python
def get_chunk_locations(self, virtual_path: str) -> dict[int, list[ChunkLocation]]:
    """청크 index → 이 기기가 아는 위치 목록."""

def add_chunk_location(self, virtual_path: str, index: int,
                       location: ChunkLocation, size: int, hash: str) -> None:
    """카피 위치를 추가한다(같은 위치 재등록은 멱등)."""

def remove_chunk_location(self, virtual_path: str, index: int,
                          device_id: str, source_id: str) -> None:
    """카피 위치를 제거한다(이전 완료 후 로컬 카피 삭제 시)."""
```

### ReplicationManager

```python
def _target_locations(self, chunk_index: int, known: list[ChunkLocation],
                      candidates: list[dict]) -> list[dict]:
    """이 청크에 추가할 위치를 고른다.

    다른 기기 후보를 먼저 쓴다. 다른 기기 후보가 하나도 없을 때만 같은 기기의
    미사용 소스를 쓴다(여유 공간 임계는 두지 않는다 — 로컬이 카피로 차는 것은 허용된
    결과다). 같은 소스에 두 번 두지 않는다.
    """

def _relocate_copy(self, virtual_path: str, chunk_index: int,
                   from_loc: ChunkLocation, to_device: str) -> bool:
    """같은 기기에 몰린 카피 하나를 다른 기기로 옮긴다.

    저장 → 서버 등록 → 로컬 삭제 순서를 지켜 카피 수가 3 아래로 내려가지 않게 한다.
    어느 단계든 실패하면 로컬 카피를 남기고 False를 돌려준다.
    """
```

### ParityStore (stardustlib/parity_store.py, 재구현)

```python
class ParityStore:
    """타 사용자 청크를 스토리지 소스에 보관한다.

    자체 파일 I/O와 index.json을 버리고 StorageSource와 메타데이터 DB를 쓴다.
    인가(소유자만 fetch/delete)와 쿼터는 DB 회계로 집행한다.
    """

    def __init__(self, storage_pool, metadata_store, max_bytes: int | None): ...

    def store(self, chunk_id: str, owner_user_id: str, data: bytes) -> None:
        """소스를 골라 쓰고 DB에 등록한다. 공간·쿼터 초과면 QuotaExceededError."""

    def fetch(self, chunk_id: str, requester_user_id: str) -> bytes: ...
    def delete(self, chunk_id: str, requester_user_id: str) -> None: ...
    def used_bytes(self) -> int:
        """DB 집계(SUM(size)). 파일 스캔하지 않는다."""
```

물리 경로는 내 청크(`<hh>/<hex32>_cNNNN`)와 구분되도록 별도 접두사를 쓴다(예:
`p/<hh>/<chunk_id>`). 경로 규칙은 사람이 구분하기 쉽게 하려는 것이고, 인가는 DB의
`owner_user_id`로 집행한다.

### 서버 (app/services/replication_service.py)

```python
async def record_replica(self, owner_user_id: str, chunk_id: str,
                         holder_device_id: str, source_id: str) -> bool:
    """카피 위치를 등록한다(기기+소스 단위 멱등)."""

async def placement(self, user_id: str, size: int, count: int,
                    exclude: list[str], exclude_locations: list[tuple[str, str]]):
    """가용 위치 후보를 고른다.

    가용 = hosting_quota_bytes - hosted_bytes ≥ size (RECIPROCITY_FRACTION 제거).
    exclude_locations로 이미 카피가 있는 (device, source)를 뺀다.
    """
```

`/replication/policy` 응답에 `target_copies`와 요청 기기의 `hosting_quota_bytes`를
추가한다. 기기별 할당량은 `hosting` 테이블에 서버가 기록하며, 클라이언트는 신고하지
않는다(사용량만 보고).

## Data Models

### 클라이언트 — MetadataStore v8

```sql
-- v7: PRIMARY KEY (virtual_path, chunk_index) → 청크당 위치 1개
-- v8: 위치를 행으로 분리. 청크 자체 정보(size/hash)는 위치와 무관하게 같다.
CREATE TABLE file_chunks (
    virtual_path  TEXT    NOT NULL,
    chunk_index   INTEGER NOT NULL,
    device_id     TEXT,                    -- NULL = 이 기기(레거시 호환)
    source_id     TEXT    NOT NULL,
    chunk_ref     TEXT,                    -- 로컬 소스 경로. parity면 NULL
    kind          TEXT    NOT NULL DEFAULT 'source',  -- source | parity
    size          INTEGER NOT NULL,
    hash          TEXT,
    PRIMARY KEY (virtual_path, chunk_index, device_id, source_id)
);
CREATE INDEX idx_file_chunks_path ON file_chunks(virtual_path, chunk_index);
```

마이그레이션은 기존 행을 그대로 이관한다(위치 1개). PK가 바뀌므로 테이블을 새로
만들어 복사한 뒤 교체한다. `device_id`가 PK에 들어가므로 NULL을 허용할 수 없어,
이관 시 NULL은 이 기기의 `device_id`로 채우고 모르면 빈 문자열을 쓴다.

### 클라이언트 — 타 사용자 청크 보관 인덱스 (v8에 함께 추가)

```sql
-- ParityStore의 index.json을 대체한다. 인가(owner_user_id)와 쿼터 회계(SUM(size))를
-- SQL로 처리하고, 청크 바이트는 스토리지 소스에 놓인다.
CREATE TABLE hosted_chunks (
    chunk_id       TEXT NOT NULL PRIMARY KEY,
    owner_user_id  TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    physical_path  TEXT NOT NULL,
    size           INTEGER NOT NULL,
    stored_at      REAL NOT NULL
);
CREATE INDEX idx_hosted_chunks_owner ON hosted_chunks(owner_user_id);
```

### 서버

```sql
-- 기존: UNIQUE(chunk_id, holder_device_id) → 기기당 1행
ALTER TABLE replicas ADD COLUMN source_id TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX idx_replicas_location
    ON replicas(chunk_id, holder_device_id, source_id);

-- 기존 UNIQUE 제약은 테이블 재생성으로만 제거되므로, 재생성 시점에 함께 정리한다.

-- hosting: provided_bytes(클라이언트 신고) → quota_bytes(서버 할당)
ALTER TABLE hosting ADD COLUMN quota_bytes INTEGER NOT NULL DEFAULT 0;
```

`provided_bytes`는 이관 기간 동안 남겨 두고 읽지 않는다(다음 정리에서 제거).

## Correctness Properties

### Property 1: 카피 수는 위치 수와 같다

*임의의* 청크 c에 대해, c의 카피 수는 서버 레지스트리에 등록된 서로 다른
`(holder_device_id, source_id)` 쌍의 수와 같다. 어떤 카피도 원본으로 따로 세지
않으며, 같은 소스에 두 번 등록되지 않는다.

### Property 2: 같은 소스에 두 카피를 두지 않는다

*임의의* 청크 c와 소스 s에 대해, c의 카피는 s에 최대 1개 놓인다. 디스크 하나가
고장나면 그 소스의 카피만 잃는다.

### Property 3: 다른 기기가 우선한다

*임의의* 배치에서, 사용 가능한 다른 기기 후보가 있으면 같은 기기의 소스를 쓰지
않는다. 같은 기기 카피는 다른 기기 후보가 하나도 없을 때만 만들어진다.

### Property 4: 카피가 모두 로컬인 청크는 축출되지 않는다

*임의의* 청크 c에 대해, c의 카피가 모두 이 기기에 있으면 축출 대상이 아니다. 축출
판정은 서로 다른 기기의 카피 수로 하므로 비우는 순간 0이 되는 상황이 생기지 않는다.
공간 확보를 위해 카피를 지우는 경로는 없다.

### Property 5: 이전 중에도 카피 수가 줄지 않는다

카피 이전의 어느 시점에서도 총 카피 수는 이전 시작 시점의 값보다 작아지지 않는다.
새 위치 저장과 서버 등록이 모두 성공한 뒤에만 원래 카피를 지운다. 실패하면 원래
카피가 남아 카피 수가 유지된다.

### Property 6: 위치 분포를 카피 수와 따로 판정한다

*임의의* 청크 c에 대해 "카피 수"와 "서로 다른 기기 수"를 각각 구할 수 있다. 카피 3개가
한 기기에 몰린 상태는 카피 수 3, 기기 수 1로 기록되며 heal 대상이 된다.

### Property 7: 읽기는 위치 종류와 무관하게 성공한다

*임의의* 청크 c에 대해 도달 가능한 카피가 하나라도 있으면 읽기가 성공한다. 로컬
소스 카피가 있으면 그것을 쓰고, 없으면 다른 기기의 카피를 가져온다(소스 카피는
파일 op, ParityStore 카피는 replica_fetch).

### Property 8: 보관 청크도 스토리지 소스 용량에 집계된다

*임의의* 시점에서 `get_available_space()`는 내 청크와 타 사용자 보관 청크가 쓴 공간을
모두 반영한다. 보관 청크가 스토리지 소스 밖(별도 디렉토리)에 놓여 집계를 벗어나는
경로는 없다. `ParityStore.used_bytes()`는 DB 집계와 일치한다.

## Error Handling

- 위치 후보가 3곳 미만: 확보분만 두고 미달로 기록한다. `pending` + 사유 로그(가능
  위치 수, 다른 기기 수). 재시도는 heal 주기에 맡긴다.
- 같은 기기 소스가 부족(소스 1개): 카피 1개만 두고 미달. 소스를 추가하면 다음
  주기에 채운다.
- 소스 용량 부족: 그 소스를 후보에서 빼고 다음 후보로. 모두 부족하면 미달.
- 로컬이 카피로 차서 새 파일 저장 불가: 기존 2단 규칙대로 스필오버를 시도하고,
  불가하면 용량 부족 에러(`InsufficientStorageError`). 카피를 지워 공간을 만들지
  않는다. 타 사용자 보관 청크가 공간을 차지해 내 파일을 저장하지 못하는 상황도 이
  경로로 그대로 노출한다(requirements의 미해결 과제 참조 — 정책으로 별도 해결).
- 보관 청크 저장 시 소스 공간 부족: `QuotaExceededError` → p2p 507. 쿼터 초과와 같은
  응답이라 요청자 쪽 홀더 배제 로직이 그대로 동작한다.
- `.parity/` → 소스 이관 중 공간 부족: 옮기지 못한 청크를 로그로 남기고 `.parity/`에
  남긴다. 이관은 다음 기동에 재시도한다(무손실 우선).
- 이전 중 새 위치 저장 실패: 로컬 카피 유지, 다음 heal 주기 재시도.
- 이전 중 서버 등록 실패: 새 위치에 카피가 남아 일시적으로 4카피가 된다. 다음 주기에
  등록을 재시도하고, 등록되면 원래 카피를 지운다(중복은 내구성을 해치지 않는다).
- 정책 조회 실패: 직전 할당량 유지. 한 번도 받지 못했으면 할당량 0(호스팅 안 함).
- 마이그레이션 실패: `.v7.bak` 경로를 로그에 남기고 기동 중단.
- 오프라인: 서버 레지스트리를 못 읽으면 카피 수 판정을 보류한다. 미달로 단정해
  불필요한 재배치를 하지 않는다.

## Testing Strategy

- 단위: `_target_locations`가 다른 기기를 우선하는지, 같은 소스를 두 번 고르지
  않는지, 후보 부족 시 확보분만 돌려주는지.
- 단위: `distinct_devices`가 한 기기 3카피를 기기 수 1로 세는지(Property 6).
- 단위: `_relocate_copy`가 저장·등록·삭제 순서를 지키는지, 각 단계 실패 시 로컬
  카피를 남기는지(Property 5).
- 단위: 마이그레이션 v7 → v8이 기존 행을 위치 1개로 이관하는지, 백업 파일을
  만드는지, 실패 시 중단하는지.
- 통합: 기기 1대·소스 3개에서 3카피가 서로 다른 소스에 놓이는지. 기기를 추가하면
  heal이 카피를 옮기고 총 3을 유지하는지(Property 3·5).
- 통합: 로컬 카피를 지운 뒤에도 다른 기기 카피로 읽기가 성공하는지(Property 6).
- 서버 단위: `record_replica`가 기기+소스 단위로 멱등인지, `placement`가 할당량 −
  사용량으로 후보를 고르는지(비율 제거).
- 단위: 재구현한 `ParityStore`가 소스에 쓰고 `hosted_chunks`에 등록하는지, 소유자만
  fetch/delete 하는지, `used_bytes()`가 DB 집계와 일치하는지, 소스 공간 부족 시
  `QuotaExceededError`인지.
- 단위: 보관 청크가 `get_available_space()`에 반영되는지(Property 8).
- 단위: `.parity/` → 소스 이관이 청크를 옮기고 인덱스를 DB로 넣는지, 공간 부족 시
  남긴 청크를 로그로 알리는지.
- 회귀: 기존 백업·복구·heal 경로가 위치 1개 상태에서 그대로 동작하는지.
