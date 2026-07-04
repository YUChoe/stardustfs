---
inclusion: manual
---

# 통합 스토리지 상태 뷰 — 설계

## 개요
서버 `device_sources` 레지스트리를 단일 진실 원천으로 삼는다. 각 디바이스 데몬이
자기 소스의 상태(state·용량)를 주기적·변경 시 신고하고, GUI는 자기 소스 포함 전부를
레지스트리에서 동일하게 렌더한다. 관리(추가/디태치)는 소유 디바이스에서만 수행한다.

## Components and Interfaces

### 서버 (../stardustfs-server)
- `device_sources`에 `state` 컬럼 추가(Data Models 참조).
- `POST /devices/sources`(report): 요청 항목에 `state` 수용. 누락 시 'ready'.
- `GET /devices/sources`(list_all_sources): 응답에 `state`와 디바이스 `is_online`을
  포함(현행 join 유지). 자기 디바이스 제외 없이 사용자의 모든 소스를 반환한다(자기
  포함 — 클라이언트가 "이 기기" 표식을 붙인다).
- `device_service.replace_sources`: state 저장. `list_all_sources`: state 반환.

### 클라이언트 보고 (device_manager / stardustfs.py)
- `build_local_source_inventory`: 각 항목에 `state` 추가 — 소스가 활성(FAT 마운트
  완료)이면 'ready', 아니면 'initializing'. (LoopbackSource.is_active 기준.)
- 보고 트리거:
  - 시작/리로드 시(현행).
  - 변경 시: 소스 추가/포맷 완료/디태치/사용량 ±(≥64 MiB 또는 ≥5%) → 즉시 `report_sources`.
  - 주기: 데몬 heartbeat 루프에 60초 재신고 추가(누락 보정).

### 클라이언트 조회/표시 (gui/actions.py, gui/app.py)
- `storage_overview`: 로컬-라이브 특수처리를 제거하고 레지스트리(GET /devices/sources)
  만으로 전 소스를 구성한다. 각 행에 `state`·`is_online`·`device`·용량·`self`(이 기기)
  포함. 자기 디바이스 식별(`_identify_self`)은 표식·관리 게이팅에만 사용(제외 아님).
- 표시 상태 산출: 디바이스 offline → "오프라인"; online + state=initializing →
  "초기화 중"; online + ready → "준비됨".
- 오프라인 강등: 서버 미도달 시 이 디바이스의 로컬 소스만 라이브 표시 + 안내.
- 관리: detach/add 버튼은 `self`=true 행에서만 활성(현행 리모트 detach 금지 일반화).

## Data Models
- device_sources(서버): 기존 (device_id, source_id, type, capacity_bytes, used_bytes,
  updated_at) + 신규 `state TEXT NOT NULL DEFAULT 'ready'`. PK (device_id, source_id) 불변.
- 마이그레이션: `ALTER TABLE device_sources ADD COLUMN state TEXT NOT NULL DEFAULT
  'ready'`(존재 시 무시). 기존 행 'ready'. 클라이언트 메타 DB 스키마 변경 없음.

## Correctness Properties

### Property 1: 디바이스 간 동일성
*임의의* 동일 사용자 두 온라인 디바이스 A, B에 대해, 같은 레지스트리 버전에서 조회하면
A와 B가 보는 소스 집합·각 소스의 상태·용량은 동일하다(자기 표식만 다름).

### Property 2: 상태 단일값
*임의의* 소스에 대해 표시 상태는 {오프라인, 초기화 중, 준비됨} 중 정확히 하나이며,
디바이스 online + state로 결정된다(로컬/리모트 구분에 의존하지 않음).

### Property 3: 신선도 수렴
*임의의* 소스 상태 변경(추가/포맷/디태치/용량)에 대해, 변경 후 즉시 보고 또는 다음
60초 주기 내에 레지스트리에 반영되어 모든 디바이스가 갱신값을 본다.

## Error Handling
- 서버 미도달/미로그인: 통합 뷰 불가 → 로컬 소스 라이브 표시 + "다른 디바이스 미상"
  안내(강등, 예외 아님).
- 보고 실패: 다음 트리거/주기에 재시도. 레지스트리는 마지막 성공값 유지.
- state 누락(구버전 클라이언트 보고): 서버가 'ready'로 간주(후방 호환).
- 디바이스 오프라인: 소스는 마지막 보고 용량 + "오프라인"으로 표시(사라지지 않음).

## Testing Strategy
- 서버: device_sources state 저장/조회, 마이그레이션 ADD COLUMN 멱등, list_all_sources
  가 자기 포함 전체 + state + is_online 반환.
- 클라이언트 보고: build_local_source_inventory가 활성=ready/비활성=initializing,
  변경 트리거·60초 주기 보고.
- storage_overview: 레지스트리 단일 원천으로 self 표식 포함 전 소스 구성, 오프라인 강등.
- 일관성: 두 디바이스 모의(같은 레지스트리 응답)에서 동일 렌더(상태 산출 함수 단위).
- 회귀: 기존 storage_overview/detach/devices 테스트 갱신(자기 포함·state 반영).

## 단계
Phase 1 서버 스키마+엔드포인트(state) → Phase 2 클라이언트 보고(state+트리거+주기)
→ Phase 3 조회/표시 통합(self 표식·상태 산출·오프라인 강등) → Phase 4 회귀/문서.
