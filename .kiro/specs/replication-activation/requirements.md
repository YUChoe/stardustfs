---
inclusion: manual
---

# 리플리케이션 운영 활성화 — Requirements

리플리케이션 엔진(스펙 replication-parity, Phase 1~7)을 daemon에 붙여 실제로
동작하게 한다. 호스팅 자동 활성 + 용량 신고, 파일 자동 백업, 주기적 재복제(heal).

## Requirement 1: 호스팅 자동 활성 및 용량 신고

사용자 스토리: 사용자로서, 내 device가 네트워크에 제공 용량을 알려 다른 사용자의
암호문을 보관하고, 그 대가로 내 백업을 받고 싶다.

### Acceptance Criteria
1. WHEN daemon이 시작되고 `replication.enabled`가 참이며 device_id가 확보되면
   THE daemon SHALL POST `/replication/hosting`으로 `provided_bytes`를 신고한다.
2. THE daemon SHALL 로컬 ParityStore의 최대 용량을 `provided_bytes * 0.5`(호혜
   비율)로 설정한다.
3. IF `replication.enabled`가 거짓이면 THE daemon SHALL 호스팅을 신고하지 않고
   ParityStore도 활성화하지 않는다(기존 동작 유지).
4. IF device_id가 없으면(미등록) THE daemon SHALL 호스팅 신고를 건너뛰고 경고를
   로깅한다(크래시 금지).

## Requirement 2: 파일 자동 백업

사용자 스토리: 사용자로서, 업로드한 파일이 자동으로 다른 기기들에 암호화 복제되어
한 기기를 잃어도 데이터가 보존되길 원한다.

### Acceptance Criteria
1. WHILE daemon이 실행 중이고 `replication.enabled`가 참이면 THE daemon SHALL
   `backup_interval_seconds`마다 `replication_status`가 `none`인 로컬 소유 파일을
   찾아 replicate를 수행한다.
2. WHEN replicate가 ≥`min_replicas` 홀더를 확보하면 THE daemon SHALL 해당 파일을
   `replicated`로, 아니면 `pending`으로 표시한다.
3. THE daemon SHALL 한 주기에 처리하는 파일 수와 동시 복제 수에 상한을 둔다.

## Requirement 3: 주기적 재복제(heal)

사용자 스토리: 사용자로서, 일부 기기가 오프라인이 되어 복제본이 줄어도 자동으로
복구되길 원한다.

### Acceptance Criteria
1. WHILE daemon이 실행 중이고 `replication.enabled`가 참이면 THE daemon SHALL
   `heal_interval_seconds`마다 `replicated`/`pending` 파일의 건강성을 점검하고
   부족한 청크를 ensure_replicas로 보충한다.
2. WHEN 보충 후에도 도달 가능한 소스가 없으면 THE daemon SHALL `pending`으로 두고
   경고를 로깅한다.

## Requirement 4: 오프라인/실패 내성

### Acceptance Criteria
1. IF 서버 도달 불가 또는 홀더 부족이면 THE daemon SHALL 해당 파일을 `pending`으로
   두고 다음 주기에 재시도한다(백그라운드 작업 중단/crash 금지).
2. THE daemon 종료 시 SHALL 백그라운드 복제/재복제 작업을 정리한다.

## 비범위 (후속)
- 등급별 백업 사본 수/호혜 비율 정책(MVP4 플랫폼).
- 교차 사용자 릴레이 fallback(릴레이 허브 인가 재설계, 별도 스펙).
- 데이터 전송의 UDP 채널 전환(현재 직접 HTTP + 스웜).
