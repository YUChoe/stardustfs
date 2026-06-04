---
inclusion: manual
---

# 리플리케이션 운영 활성화 — Tasks

## Phase A1: 호스팅 자동 활성 + 용량 신고
- [x] A1.1 config `replication` 섹션을 dict로 로드(추가 키 무시, 스키마 변경 불필요).
      기본 enabled=false.
- [x] A1.2 `_build_parity_store`를 replication.enabled + provided_bytes*0.5
      (_RECIPROCITY_FRACTION)로 전환. replication 섹션 없으면 레거시 p2p.parity_enabled
      + parity_max_bytes 폴백.
- [x] A1.3 `replication_hosting.report_hosting`: POST /replication/hosting. startup_v2
      device 등록 직후 enabled+provided>0+device_id 시 1회 신고. 인증/404/네트워크
      graceful(False 반환, 경고).
- [x] A1.4 단위 테스트 6종: parity max=provided*0.5 / 비활성 None / 레거시 폴백 /
      신고 성공 페이로드 / 404 graceful / 도달 불가 graceful.

## Phase A2: 파일 자동 백업 루프
- [x] A2.1 metadata `list_virtual_paths_for_replication(statuses, owner_device_id)`:
      deleted=0 + status 필터 + 소유(device_id==owner OR NULL) — 원격 소유 제외.
- [x] A2.2 `replication_scheduler.py`: backup 루프(주기마다 ≤max개 replicate,
      asyncio.to_thread, 실패 격리, 정지 신호 즉시 깨움). startup_v2에서 enabled 시
      ReplicationManager+스케줄러 시작, _cleanup에서 stop(매니저 close 포함).
- [x] A2.3 단위 테스트 5종: 대상 선별(status·소유·tombstone) / 전체 복제 / 실패 격리
      / max 상한 / start·stop 라이프사이클.

## Phase A3: 주기적 재복제(heal) 루프
- [x] A3.1 heal 루프: replicated/pending 파일에 ensure_replicas(주기·상한·격리,
      to_thread). 첫 주기는 heal_interval 후 시작(기동 직후 churn 방지). start()가
      backup+heal 두 태스크 기동.
- [x] A3.2 단위 테스트 2종: ensure_replicas 호출 / 실패 격리.

## Phase A4: 검증 / 문서
- [x] A4.1 전체 클라이언트 스위트 482 passed/1 skip(enabled=false 기본, 회귀 없음).
- [~] A4.2 로컬 서버 + 다중 홀더 자동 백업/heal 스모크 — 인프로세스 E2E
      (test_replication_e2e)로 엔진은 검증됨. daemon 통합 스모크는 수동(후속).
- [x] A4.3 ARCHITECTURE/HANDOVER에 운영 활성화 동작 반영.

## Phase A5: 재복제 유예(grace) 정책
- [x] A5.1 ReplicationManager.replication_health(virtual_path): 재복제 없이 청크별
      online 복제 수만 점검(HealthSummary degraded/min_online/chunk_count).
- [x] A5.2 스케줄러 heal에 유예 게이트: degraded가 heal_grace_seconds(기본 24h) 이상
      지속된 파일만 ensure_replicas. 건강 회복 시 관측 기록 삭제(일시 오프라인 churn
      방지). config heal_grace_seconds.
- [x] A5.3 단위 테스트: 유예 경과 후 재복제 / 유예 중 대기 / 건강 파일 건너뜀+기록
      삭제 / 실패 격리.

## Phase A6: 정책 다운로드 + 기본 활성
- [x] A6.1 서버 GET /replication/policy(reciprocity_fraction/min_replicas, Settings
      기반). placement도 Settings 비율 사용(정책-집행 일치). 테스트 2종.
- [x] A6.2 클라이언트 fetch_policy(graceful None). daemon 시작 시 1회 내려받아
      parity 비율·매니저 min_replicas에 적용.
- [x] A6.3 주기적 정책 갱신: 스케줄러 policy_loop(policy_fetcher/on_policy/
      policy_interval, 시작 즉시 1회+주기). on_policy가 ReplicationManager.
      set_min_replicas + ParityStore.set_max_bytes(provided*fraction) 반영.
- [x] A6.4 기본값 변경: replication.enabled 기본 true, 호혜 비율 0.5, 목표 복제본 수
      기본 2(무료/기본 = 홀더 2개, 서버 Settings/policy). provided_bytes 미설정 시
      로컬 총 용량을 제공(타인 최대 50% 사용) → device가 기본으로 호스팅에 참여해
      자동 백업이 실제로 완료된다.
- [x] A6.6 backup 루프가 none + pending을 모두 대상(목표 미달 pending을 즉시 재시도).
      heal 루프는 replicated 전용(건강→저하 시 24h 유예 후 보충). pending이 24h
      기다리지 않고 홀더 확보 시 곧 replicated가 된다.
- [x] A6.5 테스트: 정책 비율 반영 parity / 기본 활성 max 0 / fetch_policy 성공·도달불가
      / 스케줄러 정책 루프 적용.

## 비범위
- 등급별 정책(MVP4), 교차 사용자 릴레이, UDP 데이터 채널 전환.
