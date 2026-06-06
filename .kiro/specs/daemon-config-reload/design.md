---
inclusion: manual
---

# daemon config 리로드 + 수시 신고 — 설계

## Components and Interfaces

### daemon.py
- `_reload_path(metadata_db)` = `{metadata_db}.daemon.reload`.
- `signal_reload(metadata_db) -> dict`: 리로드 센티넬 생성(대기 없음). 제어 파일 없으면
  not_running.
- `serve(metadata_db, cleanup, on_reload=None)`: 시작 시 stop·reload 센티넬 정리. 틱
  루프에서 reload 센티넬 발견 시 제거 후 `await on_reload()`(있으면). stop은 기존대로.

### jbod_manager.py
- `replace_local_sources(new_local_sources)`: self.sources에서 is_remote=False인 기존
  로컬 소스를 제거하고 new_local_sources(초기화된 StorageSource)로 교체. 원격 소스와
  _source_map의 원격 항목, _remote_devices, _recover_fn은 보존. 같은 JBODManager
  객체를 in-place 갱신(p2p_server/sync_client 참조 유지).

### stardustfs.py
- `_build_local_sources(config) -> list[StorageSource]`: config의 directory/loopback
  소스를 생성·initialize해 반환(_build_core에서 추출, 재사용).
- startup_v2: `on_reload` 콜백 정의 — config 재로드 → _build_local_sources →
  jbod_manager.replace_local_sources → 인벤토리 즉시 재신고(report_sources). serve에
  on_reload 전달. 소스 신고 주기 기본 60s.

### gui/actions.py
- `daemon_signal_reload(config_path) -> dict`: daemon.signal_reload 위임.

### gui/app.py
- `_reload_daemon()`: daemon_signal_reload 호출(재시작 아님). 소스 add/detach 후 호출
  (기존 _restart_daemon 대체).

## Correctness Properties

### Property 1: 무중단 리로드
*임의의* 리로드에 대해, P2P 서버·sync_client·heartbeat·relay 워커는 중단되지 않고
JBODManager의 로컬 소스만 교체된다(daemon 프로세스 유지).

### Property 2: 참조 보존
*임의의* 리로드에 대해, p2p_server·sync_client가 들고 있는 jbod_manager 참조는 그대로이며
교체된 소스를 즉시 사용한다(같은 객체 in-place 갱신).

### Property 3: 원격·복구 보존
*임의의* 리로드에 대해, _remote_devices와 _recover_fn은 보존된다(로컬 소스만 교체).

## Error Handling
- 리로드 중 소스 생성 실패: 기존 소스 유지, 경고 로그. 센티넬은 소비됨(다음 신호로 재시도).
- signal_reload는 제어 파일 없으면 not_running(daemon 미실행).

## Testing Strategy
- jbod: replace_local_sources가 로컬만 교체하고 원격/_recover_fn 보존.
- daemon: signal_reload 센티넬 생성/감지; serve가 reload 시 on_reload 호출 후 센티넬 제거
  (경량 단위 — serve 루프 일부 또는 센티넬 함수).
- device_manager: 기본 주기 60s 반영.
