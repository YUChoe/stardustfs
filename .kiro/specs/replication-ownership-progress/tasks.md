---
inclusion: manual
---

# 복제 소유 모델 정정 및 진행 가시성 — Tasks

## Phase 1: 홀더 배치에서 원본 보관 device 제외 (Requirement 2)

- [ ] 1.1 `MetadataStore.get_chunks`로 청크별 `device_id`를 얻어 index→device 맵을
      만드는 `ReplicationManager._origin_devices` 추가. 청크 레코드가 없으면 빈 맵.
- [ ] 1.2 `_replicate_chunks`의 exclude에 `origin[idx]` 추가(자기 device·쿼터 배제와
      합집합). `_heal_chunk`에도 동일 적용.
- [ ] 1.3 제외 후 후보가 없으면 pending + WARNING 1회(경로별 중복 억제).
- [ ] 1.4 테스트: 원본 보관 device가 후보에서 빠지는지, 후보 없으면 pending·경고.

## Phase 2: 백업 대상 선정을 로컬 청크 기준으로 (Requirement 1)

- [ ] 2.1 `MetadataStore.list_paths_with_local_chunks(statuses, device_id)` 추가
      (`file_chunks.device_id = ? OR IS NULL`). 청크 레코드가 없는 파일은 제외.
      스키마는 확인 완료: virtual_path, chunk_index, chunk_ref, source_id,
      device_id, size, hash.
- [ ] 2.2 `ReplicationScheduler`가 `list_virtual_paths_for_replication` 대신 2.1을
      사용. 기존 함수는 남겨 CLI 호환 유지.
- [ ] 2.3 `ReplicationManager._chunks_to_replicate` → `_local_chunks`로 교체:
      원격 device 청크는 건너뛴다. `read_ciphertext` 전체 읽기 폴백을 제거한다
      (이번 릴레이 왕복 787 MB의 직접 계기).
- [ ] 2.4 파일 상태 판정을 서버 레지스트리 기준으로: 로컬 청크만 올린 뒤
      `_health(file_ref)`로 전체 청크의 online 복제 수를 확인해 replicated/pending 결정.
- [ ] 2.5 로컬 청크가 없으면 `status="skipped"`로 조기 반환(원격 읽기 금지).
- [ ] 2.6 테스트: 청크가 두 device에 나뉜 파일에서 각자 자기 청크만 올리는지,
      `read_chunk` 릴레이 호출이 0인지, 합쳐서 replicated가 되는지.

## Phase 2b: 자기 device 식별 실패 노출 (Requirement 3)

- [ ] 2b.1 `CLISession._identify_self`가 None이면 WARNING(조회한 name/os 포함).
- [ ] 2b.2 `make_replication_manager`가 서버 정책의 `min_replicas`를 전달.
- [ ] 2b.3 `ReplicationManager`가 self device_id 없이 동작하면 1회 로그로 알린다.
- [ ] 2b.4 테스트: device_id 미확정 시 경고, 원본 제외 규칙만으로 배치되는지.

## Phase 3: 수동 백업 위임 (Requirement 4)

- [ ] 3.1 `actions.backup_paths`가 청크 보관 device를 확인해, 전부 원격이면
      그 device로 위임(서버 경유 announce 또는 릴레이 ctl)하고 전송하지 않는다.
- [ ] 3.2 위임 대상 오프라인 시 상태바 메시지(i18n 키 추가: `backup_delegate_offline`).
- [ ] 3.3 일부만 로컬이면 로컬분 전송 + 나머지 위임.
- [ ] 3.4 테스트: 위임 경로에서 로컬 전송이 일어나지 않는지, 오프라인 처리.

## Phase 4: 진행 로그 (Requirement 5)

- [x] 4.1 전송 단계 진행 로그(청크 20개 이상, 10분할) — 커밋 8f0970d.
- [ ] 4.2 읽기 단계 진행 로그: `_local_chunks`가 청크를 읽는 동안 동일 간격으로.
- [ ] 4.3 홀더 전송이 직접 3초 + 릴레이 35초를 모두 소진하면 지연 홀더를 로그에.
- [ ] 4.4 테스트: 20개 미만 파일에서 진행 로그가 없는지(회귀).

## Phase 5: 진행 상태 GUI 노출 (Requirement 6)

- [ ] 5.1 `stardustlib/replication_progress.py`에 `ProgressTracker`/`ProgressSnapshot`
      추가(메모리, 스레드 안전, 파일 기록 없음).
- [ ] 5.2 `ReplicationManager`에 선택적 `progress` 주입. 청크 루프에서 갱신하고
      종료 시 `finish()`(예외 경로 포함 `finally`).
- [ ] 5.3 daemon이 tracker를 만들어 매니저와 제어 서버에 함께 주입.
- [ ] 5.4 `POST /ctl/progress` 라우트 추가(기존 토큰 인증). 진행 없으면
      `{"active": false}`.
- [ ] 5.5 `actions.replication_progress(config_path)` 추가(타임아웃 2초, 실패 시 None).
- [ ] 5.6 GUI `_poll_meta`에서 함께 조회해 상태바에 "백업 중: {name} {done}/{total}"
      표시. i18n 키 추가(ko/en).
- [ ] 5.7 테스트: tracker 단조 증가·정리, 라우트 응답 필드, GUI 폴링 실패 무시.

## Phase 6: 문서

- [ ] 6.1 `docs/ARCHITECTURE.md` 리플리케이션 절에 소유 모델(사용자 단위)과 device
      분담, 원본 기기 제외 규칙, 진행 가시성을 반영.
- [ ] 6.2 `.kiro/knowledge-graph.json` 갱신은 커밋 훅 절차에 따른다.

## 검증

- 각 Phase 종료 시 `pytest` 전체 통과(현재 기준 752 passed / 1 skipped).
- 실환경 확인: lg14와 데스크톱에 청크가 나뉜 파일을 백업했을 때
  `op=read_chunk` 릴레이 호출이 0이고, 양쪽이 각자 청크를 올려 replicated가 되는지.
