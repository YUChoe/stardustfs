---
inclusion: manual
---

# JBOD 리모트 스필오버 + 콜드 축출(티어링)

## 배경
로컬+리모트 스토리지를 JBOD처럼 다뤄, 로컬이 꽉 차면 여유 있는 다른 스토리지로 파일을
기록하고, 백업(복제본)이 있는 콜드 파일은 로컬 공간이 부족할 때 로컬 원본을 비워
공간을 회수한다.

현재 `select_source`는 리모트 소스를 제외하므로 로컬이 모두 차면
InsufficientStorageError이며 리모트로 흘려보내지 않는다. 리모트로의 raw 기록은
`_evacuate_to_remote`(push_blob + 메타 device_id 갱신)에 이미 있어 재사용한다.

## 결정(사용자 확정)
- 스필오버 대상: 온라인 내 디바이스(같은 계정)만. 도달 가능한 리모트가 없으면
  InsufficientStorageError(무손실 에러). 타 사용자는 비범위.
- 축출 시점: 로컬 공간 부족 시에만(콜드). 평소엔 로컬 원본 유지(빠른 읽기).
- 안전 임계치: replicated 이고 현재 온라인 복제본 수 ≥ min_replicas일 때만 삭제
  (기본 min_replicas=1; 등급제로 후일 ≥2 보장). 스테일 플래그로 삭제 금지 — 삭제
  직전 실측.

## Requirements

### Requirement 1: 로컬 만석 시 리모트 스필오버(쓰기)
#### Acceptance Criteria
1. WHEN 신규 파일 쓰기에서 로컬 소스 중 파일 크기를 수용할 곳이 없으면 THE 클라이언트
   SHALL 온라인 리모트 디바이스(같은 계정)에 암호문을 기록하고 메타데이터를 그 디바이스
   소유로 등록한다(device_id/source_id/physical_path=리모트).
2. WHEN 도달 가능한 온라인 리모트가 없으면 THE 클라이언트 SHALL InsufficientStorageError를
   발생시킨다(무손실).
3. THE 로컬 소스 간 분산(여러 로컬 소스 중 여유 최대 선택)은 기존 select_source 동작을
   유지한다(로컬 우선).
4. WHEN 스필오버로 리모트에 기록된 파일을 읽으면 THE read_file SHALL 기존 원격 라우팅
   (device_id=리모트 → P2P/릴레이 fetch → 복호화)으로 동작한다.

### Requirement 2: 콜드 축출(로컬 공간 회수)
#### Acceptance Criteria
1. WHILE 로컬 여유 공간이 임계치 미만이면 THE 클라이언트 SHALL replicated 상태이고 현재
   온라인 복제본 수 ≥ min_replicas인 로컬 파일을 오래된 순으로 골라 로컬 원본을 삭제하고
   "복제본 전용(local-evicted)" 상태로 표시한다.
2. THE 축출 SHALL 삭제 직전 온라인 복제본 수를 실측해 min_replicas 미만이면 그 파일은
   건너뛴다(스테일 replicated 플래그로 삭제 금지).
3. WHEN local-evicted 파일을 읽으면 THE 클라이언트 SHALL 복제 홀더에서 복구(recover)해
   제공한다. 재구성 실패(홀더 도달 불가)면 규격 에러를 반환한다.
4. THE 축출 SHALL 로컬 여유가 목표치 이상으로 회복되면 멈춘다(필요분만 축출).

### Requirement 3: 안전·무손실
#### Acceptance Criteria
1. THE 축출 SHALL 리모트 기록/복제본 확인이 성공한 뒤에만 로컬 원본을 삭제한다
   (대상 확보 전 원본 삭제 금지).
2. THE 변경 SHALL zero-knowledge·소유권 모델을 유지한다(서버는 암호문/version만,
   리모트 기록은 같은 계정 device).
