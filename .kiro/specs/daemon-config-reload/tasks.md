---
inclusion: manual
---

# daemon config 리로드 + 수시 신고 — Tasks

- [x] 1 device_manager: _SOURCE_REPORT_INTERVAL 기본 60s. startup_v2 기본 60s.
- [x] 2 daemon.py: _reload_path + signal_reload + serve(on_reload) 리로드 센티넬 처리
      (시작 시 정리, 틱에서 감지·제거·콜백, 종료 시 정리).
- [x] 3 jbod_manager.replace_local_sources(new_local_sources): 로컬만 교체, 원격·
      _remote_devices·_recover_fn 보존, in-place.
- [x] 4 stardustfs.py: _build_local_sources 추출 + startup_v2 on_reload(재로드→교체→
      즉시 재신고), serve에 on_reload 전달.
- [x] 5 gui/actions.daemon_signal_reload + app._reload_daemon로 add/detach 후 리로드
      (재시작 대체).
- [x] 6 테스트: replace_local_sources(로컬 교체·원격 보존), signal_reload 센티넬,
      serve on_reload 호출, device_manager 기본 주기 60s. 회귀 528 passed/1 skip.

## 비범위(후속)
- used_bytes 변동(파일 쓰기/삭제)의 즉시 신고는 주기(60s)로 충분 — 변경 이벤트 훅은 후속.
