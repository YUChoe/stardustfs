# Design: 원격 파일 수정 시 로컬 소유권 이전 (3a)

## Overview

원격 소유 파일 수정 시 OSError를 내던 write_file을, 로컬 소스에 새로 기록하고
metadata 소유권을 로컬로 이전하도록 변경한다. 가상 경로는 유지된다. 원래 소유
디바이스의 물리 파일은 고아가 되며, 각 디바이스가 동기화 후 orphan GC로 자체
정리한다.

## 1. 소유권 이전 (JBODManager.write_file)

기존 분기:
```
existing = lookup(virtual_path)
if existing:
    owner = existing.device_id
    if owner is not None and owner != self.device_id:
        raise OSError(...)         # ← 제거
    # 같은 위치 덮어쓰기
else:
    # 신규 생성
```

변경 후:
```
existing = lookup(virtual_path)
if existing:
    owner = existing.device_id
    if owner is not None and owner != self.device_id:
        # 소유권 이전(takeover): 로컬에 새로 기록, metadata를 로컬 소유로 갱신
        self._takeover_write(virtual_path, existing, encrypted, len(data))
        return
    # (기존) 같은 위치 덮어쓰기
else:
    # (기존) 신규 생성
```

### _takeover_write(virtual_path, existing, encrypted, size)

1. 로컬 쓰기 대상 소스 선택: `select_source(len(encrypted))` (원격 소스 제외됨)
2. 새 physical_path 생성: `_generate_physical_path(virtual_path)`
3. 트랜잭션:
   - `source.write(new_physical_path, encrypted)`
   - `metadata_store.update(virtual_path, source_id=local, physical_path=new,
     file_size=size, modified_at=now, device_id=self.device_id)`
     → version 증가, sync_status='pending'
   - commit
4. 실패 시: 부분 파일 삭제 + metadata 롤백 (기존 신규 생성 경로와 동일 패턴)
5. 공간 부족 시 InsufficientStorageError

metadata_store.update가 source_id/physical_path까지 갱신할 수 있어야 한다. 현재
update 시그니처를 확인해 source_id/physical_path 인자를 받도록 확장한다(이미 일부
필드 갱신을 지원하면 키워드 추가).

이전 후 레코드: device_id=PC-A, source_id=PC-A 로컬, physical_path=새 위치,
version+1, pending. 다음 동기화에서 서버 업로드 → 다른 디바이스로 전파.

## 2. 동기화 전파

기존 sync_client의 version 기반 병합을 그대로 사용한다. 이전으로 version이
증가했으므로, 서버/다른 디바이스의 이전 레코드보다 우선한다. 별도 변경 없음.

다른 디바이스(PC-B)가 이 레코드를 수신하면 device_id가 PC-A로 갱신되어, PC-B에서
그 파일을 읽으면 PC-A로 라우팅된다. PC-B 입장에서 자신이 갖고 있던 물리 파일은
이제 metadata가 가리키지 않으므로 orphan이 된다(→ 3).

## 3. orphan GC (MetadataStore + JBODManager)

각 디바이스가 자신의 로컬 소스 물리 파일 중, 활성 metadata에서 "자신이 소유자로"
참조하지 않는 파일을 삭제한다.

### 보존 집합 계산 (MetadataStore)

```python
def live_physical_paths_for_device(self, device_id) -> set[tuple[str, str]]:
    # deleted=0 AND (device_id == 자신 OR device_id IS NULL) 인 레코드의
    # (source_id, physical_path) 집합 반환
```

- device_id IS NULL(레거시)도 로컬 소유로 간주하여 보존(R4.2).
- tombstone(deleted=1)은 물리 파일이 이미 없거나 정리 대상이므로 보존 집합에서
  제외하되, 물리 삭제는 기존 tombstone 경로가 처리하므로 orphan GC는 "활성
  레코드가 참조하는 파일만 보존"으로 단순화한다.

### 스캔 및 삭제 (JBODManager.gc_orphan_files)

```python
def gc_orphan_files(self) -> int:
    live = metadata_store.live_physical_paths_for_device(self.device_id)
    removed = 0
    for source in self.sources:
        if not source.is_active or source.is_remote:
            continue
        for name in source.list_physical_files():      # 소스 루트의 실제 파일명
            if (source.source_id, name) not in live:
                source.delete(name)
                removed += 1
    return removed
```

- 원격 소스(is_remote)는 스캔하지 않는다(R3.4).
- 다른 디바이스 소유 레코드의 physical_path는 보존 집합에 없지만, 그 물리 파일은
  애초에 이 디바이스 디스크에 없으므로 영향 없다.
- 안전장치: device_id가 설정되지 않았으면(None) GC를 건너뛴다(전체 삭제 위험 방지).

### StorageSource.list_physical_files()

물리 파일은 소스 루트(DirectorySource) 또는 동반 디렉토리(LoopbackSource)에 평평한
`<uuid>_<name>` 형식으로 저장된다. 루트 직속 파일 이름 목록을 반환한다.

```python
# DirectorySource: os.listdir(self._path) 중 파일만
# LoopbackSource: os.listdir(self._companion_dir) 중 파일만
```

디렉토리(하위 폴더)는 제외한다. 물리 파일명만 대상으로 한다.

### 호출 시점

- 시작 시 1회: 클라이언트 구동·초기 동기화 완료 후 `jbod.gc_orphan_files()` 1회.
- 소유권 이전 발생 시: 이전(takeover)이 일어나면 "GC 필요" 플래그만 세운다.
  실제 GC는 동기화 사이클이 끝난 뒤, 그 사이클에 이전이 한 번이라도 있었으면
  1회만 실행한다(파일 단위가 아니라 사이클 단위 디바운스).
- 즉시성 불필요(R3.5): 소유권을 잃은 디바이스가 다음 구동/동기화 시 정리.

### 디바운스 (다중 파일 동시 수정 대응)

여러 파일이 동시에 이전되어도 GC가 파일마다 전체 스캔을 반복하면 안 된다. 따라서:

```python
# JBODManager
self._gc_needed = False            # 이전 발생 시 set

def _takeover_write(...):
    ...
    self._gc_needed = True          # 플래그만 set (스캔하지 않음)

def gc_orphan_files_if_needed(self) -> int:
    if not self._gc_needed:
        return 0
    self._gc_needed = False
    return self.gc_orphan_files()   # 사이클당 1회 전체 스캔
```

SyncClient는 동기화 사이클 종료 후 `gc_orphan_files_if_needed()`를 호출한다.
takeover가 N건 있었어도 한 사이클에 스캔은 1회뿐이다. 시작 시 1회는
`gc_orphan_files()`를 직접 호출한다(플래그 무관).

주의: 소유권을 "넘겨받는" 쪽(PC-A)에서 takeover가 일어나면 PC-A의 _gc_needed가
set되지만, 정작 orphan이 생기는 디바이스는 "넘겨준" 쪽(PC-B)이다. PC-A에서의 GC는
대개 삭제할 것이 없다(자기 파일은 모두 보존 집합에 있음). 실제 정리는 PC-B가
동기화로 device_id 변경을 수신한 뒤, PC-B의 시작 시/이후 사이클에서 일어난다.
PC-B는 takeover를 직접 하지 않았으므로 _gc_needed가 set되지 않을 수 있다. 이를
위해 동기화 병합에서 "내가 소유자이던 레코드의 device_id가 남으로 바뀐 것"을
감지하면 _gc_needed를 set한다(아래 4.2).

## 4. 안전성

- 로컬 소유/레거시(NULL) 수정은 기존 덮어쓰기 경로 유지(R4.1, R4.2).
- orphan GC는 "활성 metadata가 참조하는 파일은 절대 삭제 안 함"을 불변식으로 한다.
  보존 집합 계산이 실패하거나 device_id가 None이면 GC 전체를 건너뛴다(R4.3).
- orphan GC는 자신 소유 + 레거시(NULL) 레코드를 보존 집합에 포함하므로, 동기화가
  아직 안 된 상태에서 잘못 삭제하는 일을 막는다.

## 시퀀스: PC-A가 PC-B 소유 파일 수정

1. PC-A WebDAV write /3333333.txt → JBODManager.write_file
2. lookup → device_id=PC-B (원격) → _takeover_write
3. PC-A loop-00x에 암호문 기록 + metadata update(device_id=PC-A, 새 위치, version+1, pending)
4. 사용자에게는 동일 경로 1개 파일, 내용은 새 버전
5. 다음 sync: PC-A가 레코드 업로드(version↑)
6. PC-B sync 수신: device_id=PC-A로 갱신. PC-B의 옛 물리 파일은 orphan
7. PC-B sync 후 gc_orphan_files: 옛 물리 파일이 보존 집합에 없음 → 삭제, 디스크 회수

## 정확성 속성 (PBT 후보)

- Property 1 (이전 후 읽기 일관성): 이전 후 같은 경로 읽기는 새 내용을 반환한다.
- Property 2 (orphan GC 보존): 활성 metadata가 참조하는 물리 파일은 GC 후에도
  존재한다. 참조되지 않는 파일만 삭제된다.
- Property 3 (소유권 단일성): 이전 후 device_id는 정확히 로컬을 가리킨다.
