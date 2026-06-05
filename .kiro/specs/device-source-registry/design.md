---
inclusion: manual
---

# 디바이스 소스 레지스트리 — 설계

## 개요
각 디바이스 daemon이 로컬 소스 인벤토리를 서버에 신고하고, GUI는 서버를 조회해 모든
디바이스의 스토리지를 통합 표시한다. P2P 미사용(서버 HTTP만)으로 비차단·오프라인 표시.

## Components and Interfaces

### 서버 (../stardustfs-server)
- DB 테이블 `device_sources` (database.py SCHEMA_SQL에 추가):
  ```sql
  CREATE TABLE IF NOT EXISTS device_sources (
      device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL,
      type TEXT NOT NULL,
      capacity_bytes INTEGER NOT NULL DEFAULT 0,
      used_bytes INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (device_id, source_id)
  );
  CREATE INDEX IF NOT EXISTS idx_device_sources_device ON device_sources(device_id);
  ```
- 스키마(schemas.py): `DeviceSourceItem(source_id, type, capacity_bytes, used_bytes)`,
  `DeviceSourcesReport(sources: list[DeviceSourceItem])`,
  `DeviceSourceEntry(device_id, device_name, source_id, type, capacity_bytes,
  used_bytes, is_online, updated_at)`.
- DeviceService 메서드:
  - `replace_sources(user_id, device_id, sources)`: device 소유 검증 후 그 device의
    소스 레코드를 전량 교체(삭제 후 삽입). 소유 아니면 DeviceNotFound/AccessDenied.
  - `list_all_sources(user_id)`: 사용자 모든 device의 소스를 devices와 조인해 반환.
    is_online은 last_heartbeat로 실시간 판정.
- 라우터(devices.py):
  - `PUT /devices/{device_id}/sources` → replace_sources, 200 {"status":"ok"}.
  - `GET /devices/sources` → list_all_sources, list[DeviceSourceEntry].

### 클라이언트 (stardustlib)
- `device_manager.build_local_source_inventory(jbod)` (모듈 함수): jbod.sources 중
  is_remote=false인 소스에서 {source_id, type, capacity_bytes, used_bytes} 목록 생성.
  type은 클래스명 매핑(LoopbackSource→"loopback", DirectorySource→"directory").
- `DeviceManager.report_sources(sources)`: PUT /devices/{device_id}/sources. 실패 시
  False 반환(예외 없음).
- `DeviceManager.list_all_sources()`: GET /devices/sources. 실패 시 [] 반환.
- daemon 시작(stardustfs.py startup_v2): device_id 확보 후 report_hosting 부근에서
  build_local_source_inventory + report_sources 호출(실패 무시).
- GUI `actions.storage_overview`: 로컬은 기존대로 캐시 오프라인 세션에서. 리모트는
  경량 인증(CredentialStore→AuthClient, open_online 미사용)으로 GET /devices/sources +
  GET /devices를 조회해, (name, os)로 자기 device를 식별·제외하고 나머지를 행으로 만든다.

## Data Models
- 리모트 행(GUI): {scope:"remote", device_id, device(=name), name(=source_id),
  type, online, total(=capacity_bytes), used(=used_bytes)}.
- 로컬 행은 기존과 동일(실시간 total/available).

## Correctness Properties

### Property 1: 신고 멱등성
*임의의* 소스 인벤토리 신고에 대해, 같은 내용을 반복 신고해도 device_sources의 최종
상태는 동일하다(전량 교체 방식 — PRIMARY KEY(device_id, source_id) upsert/replace).

### Property 2: 소유 격리
*임의의* 사용자 U와 device D에 대해, U가 D를 소유하지 않으면 PUT은 거부되고, GET은
U 소유 device의 소스만 반환한다.

### Property 3: 자기 제외
*임의의* 조회에 대해, 호출 디바이스 자신의 항목은 리모트 목록에 포함되지 않는다.

### Property 4: 비차단
*임의의* 리모트 조회에 대해, P2P 직접 연결을 사용하지 않으므로 NAT 타임아웃으로 인한
장시간(수 초+) 워커 블로킹이 발생하지 않는다.

## Error Handling
- 신고 실패(HTTP≥400/네트워크): 경고 로그 + False 반환, daemon 시작 계속.
- 조회 실패: [] 반환 → GUI는 로컬만 표시.
- PUT 소유 불일치: DeviceNotFoundError(404)/DeviceAccessDeniedError(403) — 기존 예외 재사용.
- device 삭제 시 소스 레코드 CASCADE 삭제.

## Testing Strategy
- 서버: replace_sources/list_all_sources 단위(소유 격리, 전량 교체, 조인·온라인 판정),
  엔드포인트 인증/소유 검증.
- 클라이언트: build_local_source_inventory(원격 제외·타입 매핑), storage_overview가
  미로그인 시 remote=[] (기존 테스트 유지), report_sources/list_all_sources 실패 시
  False/[] 반환.
