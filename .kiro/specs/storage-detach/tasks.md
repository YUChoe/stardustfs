---
inclusion: manual
---

# 스토리지 attach/detach (evacuate) — Tasks

## Phase 1: 디렉터리 타입 폐지
- [x] 1.1 actions.add_source가 loopback만 허용(directory 거부, size 필수).
- [x] 1.2 GUI 스토리지 관리에서 '디렉터리 추가' 제거(loopback만).
- [x] 1.3 기존 directory 설정 로드 허용(추가만 차단). 테스트: directory 추가 거부.

## Phase 2: 로컬 evacuate + 원자적 detach
- [x] 2.1 metadata.list_files_in_source(source_id): 활성(deleted=0) 파일 목록.
- [x] 2.2 jbod.select_source(exclude_ids) 추가.
- [x] 2.3 jbod.evacuate_source(source_id): 로컬 소스로 raw 암호문 이동(대상 기록 성공
      후 원본 삭제) + 메타 source_id/physical 갱신. 대상 없으면 unmoved.
      {ok, moved, unmoved} 반환. 로컬 소유(또는 NULL)만 이동.
- [x] 2.4 actions.detach_source(config, source_id): evacuate ok면 config에서 제거
      + 세션 invalidate, unmoved면 유지 + 보고(detached 플래그).
- [x] 2.5 단위 테스트 6종: directory 거부 / 이동·무손실 / 용량부족 보류 / 빈 소스 ok /
      detach 제거 / unmoved 유지. GUI detach 버튼이 detach_source 호출.

## Phase 3: 리모트(타 디바이스) evacuate
- [x] 3.1 P2P /p2p/write가 사용한 source_id 반환 + RemoteSource.push_blob(raw 암호문
      기록 후 원격 source_id 반환). jbod._evacuate_to_remote: 로컬 불가 파일을 온라인
      리모트로 push → 메타 device_id/source_id/physical_path 갱신 → 원본 삭제.
- [x] 3.2 도달 가능한 리모트 없거나 실패 시 unmoved(원본 보존). detach_source는
      로그인 시 온라인 세션(로컬+리모트), 아니면 오프라인(로컬만). 테스트 mock 리모트
      2종(로컬 만석→리모트 이동, 리모트 오프라인→unmoved).
- [ ] (비범위) 타 사용자 리모트: 권한·인가 확장 — 별도.

## Phase 4: GUI 연동 + 문서
- [ ] 4.1 GUI 스토리지 관리: detach 버튼이 detach_source 호출, 진행/결과(이동 N,
      미이동 M) 표시. 미이동 시 사유 안내.
- [ ] 4.2 ARCHITECTURE에 attach/detach·evacuate 반영.
