---
inclusion: manual
---

# 리플리케이션 운영 활성화 — Design

## 개요

daemon(상주 프로세스)에 (1) 호스팅 용량 신고, (2) 파일 자동 백업, (3) 주기적
재복제를 붙인다. 엔진(ReplicationManager/ParityStore/서버 `/replication/*`)은 그대로
재사용하고, 오케스트레이션 루프만 추가한다. CLI 단발 명령(`backup`/`restore`/`heal`)은
유지된다.

## Components and Interfaces

### 설정 (config "replication" 섹션)
```
replication: {
  enabled: bool = false,            // 운영 활성 마스터 스위치
  provided_bytes: int = 0,          // 네트워크에 제공하는 용량(호스팅)
  backup_interval_seconds: int = 300,
  heal_interval_seconds: int = 3600,
  max_files_per_cycle: int = 20     // 한 주기 처리 상한
}
```
- ParityStore 최대 용량 = `provided_bytes * RECIPROCITY_FRACTION(0.5)`.
- `enabled=false`(기본)면 기존 동작과 동일(호스팅·백업·재복제 없음).

### 클라이언트
- `stardustlib/replication_scheduler.py`(신규): `ReplicationScheduler`.
  - `start()`: 백그라운드 asyncio 태스크 2개(backup 루프, heal 루프) 기동.
  - backup 루프: `metadata.list_local_unreplicated()`(status=none, 로컬 소유,
    tombstone 제외)에서 ≤max_files_per_cycle개를 골라 `manager.replicate`. 동기
    매니저는 `asyncio.to_thread`로 호출(루프 비차단).
  - heal 루프: `replicated`/`pending` 파일에 `manager.ensure_replicas`.
  - `stop()`: 태스크 취소 + 매니저 close.
- `device_manager`(또는 신규 헬퍼): `report_hosting(provided_bytes)` →
  POST `/replication/hosting`. 시작 시 1회 + heartbeat마다 갱신(선택).
- `stardustfs.py startup_v2`: replication.enabled면 호스팅 신고 + ParityStore 활성
  (provided*0.5) + ReplicationScheduler 시작. `_cleanup`에서 stop.
- `metadata_store`: 자동 백업 대상 조회 메서드 추가
  (`list_by_replication_status(status, owner_device_id)` 또는 기존 조회 재사용).

### 서버
변경 없음 — `/replication/hosting`·`/replication/*`는 이미 존재(Phase 2/4).

## Data Models
- 신규 테이블 없음. 클라이언트 `files.replication_status`(none|pending|replicated)
  를 백업/heal 상태로 사용.

## Correctness Properties

### Property 1: 비활성 무영향
*임의의* 설정에서 `replication.enabled`가 거짓이면, 호스팅 신고·자동 백업·재복제는
일어나지 않으며 기존 daemon 동작과 동일하다.

### Property 2: 호혜 한도 정합
*임의의* 활성 device에 대해, 로컬 ParityStore가 보관을 허용하는 최대 바이트는
신고한 `provided_bytes`의 0.5를 넘지 않는다.

### Property 3: 백업 진행 보장
*임의의* `status=none` 로컬 소유 파일은 충분한 홀더가 있으면 결국 `replicated`가
되고, 부족하면 `pending`으로 표시되어 다음 주기에 재시도된다(silent 누락 없음).

### Property 4: 실패 격리
*임의의* 백그라운드 주기에서 한 파일의 복제/재복제가 실패해도, 예외는 그 파일에
국한되며 루프·daemon은 계속 동작한다.

## Error Handling
- 서버 도달 불가/홀더 부족: 해당 파일 `pending` + 경고 로깅, 다음 주기 재시도.
- 호스팅 신고 실패(401/네트워크): 경고 로깅 후 계속(다음 heartbeat에서 재시도).
- 파일 읽기/암호화 실패: 해당 파일 건너뛰고 ERROR 로깅(루프 유지).
- daemon 종료: 백그라운드 태스크 cancel + await, 매니저 close.

## Testing Strategy
- 단위: 스케줄러가 status=none 파일을 골라 replicate 호출(가짜 매니저로 호출 검증),
  실패 시 다음 파일로 진행(실패 격리), enabled=false면 무동작.
- 호스팅 신고 헬퍼: provided_bytes 페이로드 + 404/네트워크 graceful.
- 회귀: enabled 기본 false로 기존 daemon 라이프사이클 테스트 그린 유지.

## 마이그레이션 / 배포
- config `replication` 섹션은 선택(없으면 enabled=false). 기존 설정 영향 없음.
- 단계적: 서버 `/replication/*` 미배포 시 호스팅 신고/백업이 404 → 경고 후 비활성
  진행(기존 동작 유지).
