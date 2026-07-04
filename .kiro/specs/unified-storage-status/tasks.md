---
inclusion: manual
---

# 통합 스토리지 상태 뷰 — Tasks

## Phase 1: 서버 스키마 + 엔드포인트 (../stardustfs-server)
- [x] 1.1 device_sources에 `state TEXT NOT NULL DEFAULT 'ready'` 추가 + init_db에
      멱등 마이그레이션(_migrate: PRAGMA table_info 확인 후 ADD COLUMN).
- [x] 1.2 schemas: DeviceSourceItem.state(기본 'ready'), DeviceSourceEntry.state.
- [x] 1.3 device_service.replace_sources(state 저장)/list_all_sources(state 반환,
      자기 포함 전체 — 기존부터 전체 반환). 라우터는 model_dump로 state 전달.
- [x] 1.4 서버 테스트: state 저장/조회 + 미전송 시 'ready' 기본. 서버 97 passed.

## Phase 2: 클라이언트 보고 (device_manager / stardustfs.py)
- [x] 2.1 build_local_source_inventory에 `state`(is_active→ready, else initializing).
- [x] 2.2/2.3 보고 시점: 기존 시작 1회 + start_source_report 60초 주기 + _on_reload
      재신고가 이미 존재 → state 추가로 전 경로가 state를 실음(추가 트리거 불요).

## Phase 3: 조회/표시 통합 (gui/actions.py, gui/app.py)
- [x] 3.1 storage_overview를 레지스트리 단일 원천으로 재작성: {sources:[...], online}.
      각 행 device_id/device/source_id/type/total/used/state/online/self. 오프라인은
      _local_live_sources로 강등(online=False).
- [x] 3.2 상태 산출(_status): online=False→오프라인, state!=ready→초기화 중, else 준비됨.
- [x] 3.3 GUI 모달 통합 표(디바이스|이름|상태|용량). detach는 self 행에서만(self 게이팅).
- [x] 3.4 transfers/초기화 차단(storage_initializing)은 이 디바이스 기준 유지.
- [x] 3.5 테스트: 오프라인 강등(online=False, self) 갱신. 클라 597 passed.

## Phase 4: 회귀/문서
- [x] 4.1 회귀 그린(클라 597 / 서버 97). 일관성: 단일 원천이라 동일 레지스트리→동일 렌더.
- [ ] 4.2 ARCHITECTURE/device-source-registry 스펙에 단일 원천·state·통합 뷰 반영(후속).

## 비범위
- 백업 상태(완료/대기/미백업) 재설계 — 이미 replication_status 전역 동기화로 일관(별도).
- 다른 디바이스의 소스를 원격에서 관리(추가/디태치) — 관리 동작은 소유 디바이스 한정.
- 라이브 신선도 최우선 모드(자기 소스만 라이브) — 일관성 우선 채택, 신선도는 보고
  주기·변경 트리거로 보완.
