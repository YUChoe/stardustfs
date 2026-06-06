---
inclusion: manual
---

# 디바이스 소스 레지스트리 — Tasks

## Phase 1: 서버
- [x] 1.1 database.py SCHEMA_SQL에 device_sources 테이블 + 인덱스 추가.
- [x] 1.2 schemas.py에 DeviceSourceItem/DeviceSourcesReport/DeviceSourceEntry 추가.
- [x] 1.3 DeviceService.replace_sources(user_id, device_id, sources) — 소유 검증 후 전량 교체.
- [x] 1.4 DeviceService.list_all_sources(user_id) — devices 조인 + is_online 판정.
- [x] 1.5 devices.py: PUT /devices/{device_id}/sources, GET /devices/sources.
- [x] 1.6 서버 테스트: 소유 격리, 전량 교체 멱등, 조인·온라인, 미존재 device 거부.

## Phase 2: 클라이언트 신고
- [x] 2.1 device_manager.build_local_source_inventory(jbod) — 원격 제외, 타입 매핑.
- [x] 2.2 DeviceManager.report_sources / list_all_sources (실패 시 False/[]).
- [x] 2.3 stardustfs.py startup_v2: device_id 확보 후 소스 인벤토리 신고(실패 무시).
- [x] 2.4 클라이언트 테스트: 인벤토리 생성(원격 제외/타입), 신고/조회 실패 폴백.

## Phase 3: GUI
- [x] 3.1 actions.storage_overview 리모트 부분을 서버 레지스트리 조회로 교체
      (경량 인증, open_online·P2P 미사용, 자기 device 제외).
- [x] 3.2 GUI 리모트 행: 용량=capacity_bytes 기준 used/total, 경로=device_name.
- [x] 3.3 디바이스 온라인 카운트: refresh()마다 devices_summary로 갱신됨(확인 완료).
      스크린샷 1/2는 IPG 접속 전 값의 staleness — 새로고침 시 정정.

## Phase 4: 주기 재신고
- [x] 4.1 DeviceManager.start_source_report(inventory_provider, interval) +
      _source_report_loop: 시작 1회 신고 외에 interval(기본 300s)마다 재신고. stop()에서
      heartbeat와 함께 취소. daemon startup_v2에서 build_local_source_inventory를
      provider로 시작(p2p.source_report_interval_seconds 설정).
- [x] 4.2 테스트: 주기 신고가 provider 결과를 report_sources로 전달, device_id 없으면 no-op.

## 비범위(후속)
- 타 사용자 스토리지 노출(현재 같은 계정만).
