---
inclusion: manual
---

# 루프백 FAT 이미지 스토리지 — 설계

## 개요
`LoopbackSource`를 파일 내 FAT 파일시스템(pyfatfs)으로 재구현한다. `<path>.img`는
고정 크기 FAT 이미지이고 모든 파일은 그 안에 저장된다(`.d` 동반 디렉토리 제거).
JBOD/p2p_server/sync는 StorageSource 계약만 사용하므로 LoopbackSource 내부만 바뀐다.

## Components and Interfaces

### pyfatfs 사용 (검증된 API)
- 포맷: 파일을 size_bytes로 사전할당(`truncate`) 후 `PyFat().mkfs(path, fat_type,
  size=size_bytes)`. fat_type은 size로 선택(≤2GiB FAT16, 초과 FAT32).
- 마운트: `PyFatFS(path, read_only=...)`. 파일 핸들을 보유.
- I/O: `fs.open(p,'wb'|'rb'|'r+b')`, `fs.makedirs(recreate=True)`, `fs.listdir`,
  `fs.remove`, `fs.removedir`, `fs.exists`, `fs.getsize`.
- 공간 부족: `PyFATException`("Not enough free space ...") → OSError("insufficient
  space")로 변환.

### LoopbackSource 재구현 (storage_source.py)
- `initialize()`:
  1) `<path>`가 유효한 FAT 이미지면 마운트, 아니면(신규/비-FAT) 사전할당+mkfs 후
     마운트(기존 비-FAT 데이터는 폐기 — 마이그레이션 없음).
  2) read_only 인자로 쓰기/조회 모드를 구분(데몬=쓰기, 오프라인 세션=read_only).
- 경로 매핑: physical_path(소스 루트 상대, 예 `<uuid>_<name>` 또는 `dir/<uuid>_<name>`)를
  이미지 내부 절대경로 `/` + physical_path로 매핑. 상위 디렉토리는 write 시 makedirs.
- `read/write/exists/delete/mkdir/rmdir/list_dir`: PyFatFS로 위임.
- `write_chunk(path,data,offset,total_size)`: offset=0이면 용량 검사(free ≥ total_size)
  후 새 파일 생성; offset>0이면 `r+b`로 열어 seek(offset) 후 기록. (pyfatfs 파일
  핸들 seek 지원 검증 필요 — 미지원 시 임시 호스트 파일에 조립 후 일괄 기입으로 폴백.)
- `read_chunk(path,offset,length)`: `rb`로 열어 seek(offset) 후 length 읽기.
- `get_available_space`: free clusters × bytes_per_cluster(PyFat 내부에서 산출).
  `get_total_space`: size_bytes.
- `list_physical_files`: 이미지 루트의 파일 목록(orphan GC용).
- 단일 PyFatFS 핸들을 인스턴스가 보유(매 호출 open/close 비용 회피). 스레드 직렬화는
  JBOD가 단일 워커/데몬 IO 루프에서 호출하는 기존 가정과 인스턴스 lock으로 보장.

### 동시 접근 (작업 큐 + read_only)
- 쓰기는 데몬 단독, 데몬의 이미지별 작업 큐로 직렬화(별도 OS 락 불요).
- CLI/GUI 오프라인 세션은 `read_only=True`로 LoopbackSource를 구성(조회·용량 표시).
  read_only 핸들은 FAT를 변경하지 않으므로 데몬 쓰기와 공존해도 손상 없음.

### 기존 스토리지 폐기 (마이그레이션 없음)
- `<path>`가 FAT가 아니면 새 FAT 이미지로 포맷(기존 데이터 폐기). `.img.d`에 의존
  하지 않으며, 사용자가 배포 전 기존 소스를 비운다고 가정.

## Data Models
- DB 스키마 변경 없음. physical_path 의미(소스 루트 상대 경로) 동일하게 유지 →
  이미지 내부 경로로 그대로 사용. 마이그레이션은 파일 위치 이전일 뿐.

## Correctness Properties

### Property 1: 라운드트립 항등
*임의의* 바이트열 m과 경로 p에 대해, write(p,m) 후 read(p)==m(청크 경로 포함).

### Property 2: 용량 한정
*임의의* 쓰기에 대해, 이미지 여유 공간을 초과하면 OSError(insufficient space)이고
이미지에 부분 파일이 남지 않는다(실패 원자성).

### Property 3: 마이그레이션 무손실
*임의의* 기존 `.d` 파일 집합에 대해, 마이그레이션 성공 후 모든 파일을 이미지에서
동일 바이트로 읽을 수 있고, 실패 시 `.d`가 보존된다.

### Property 4: 동시 접근 안전
*임의의* 시점에 한 이미지에 대한 쓰기 핸들은 하나뿐이다(파일 락). 동시 쓰기로 인한
FAT 손상이 발생하지 않는다.

## Error Handling
- 공간 부족: PyFATException → OSError("insufficient space: need N, available M").
- 락 경합: 쓰기 락 실패 → read_only 폴백 또는 OSError(데몬 외 프로세스의 쓰기 차단).
- 손상/포맷 불가: 비활성 처리 + 로그(ERROR), `.d` 보존(마이그레이션 전이면).
- pyfatfs 미설치: import 에러를 초기화 단계에서 명확히 보고(요구사항: 의존성 고정).

## Testing Strategy
- 단위: 포맷/마운트, read/write/list/delete/mkdir, write_chunk/read_chunk 라운드트립,
  용량 초과 OSError, get_available/total_space.
- 마이그레이션: `.d`에 파일 N개 → 초기화 → 이미지에서 동일 바이트 읽힘 + `.d.bak`
  생성. 중간 실패 시 `.d` 보존.
- 동시 접근: 두 핸들 쓰기 락 경합 시 한쪽만 쓰기.
- 회귀: 기존 LoopbackSource 사용 테스트(test_p2p_server/test_jbod/test_storage_*,
  test_remote_chunked_transfer)가 새 구현으로 그린.

## 단계
Phase 1 핵심 read/write/space + 포맷/마운트 → Phase 2 chunk + 용량/락 → Phase 3
마이그레이션 → Phase 4 회귀/문서. 각 Phase 후 회귀 확인.
