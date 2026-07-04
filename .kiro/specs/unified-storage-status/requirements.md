---
inclusion: manual
---

# 통합 스토리지 상태 뷰 — Requirements

## 배경
원칙: "사용자는 자신의 다수 디바이스 × 다수 스토리지의 상태를 한눈에 알 수 있어야
하며, 이는 모든 디바이스에서 동일하게 보여야 한다."

현재(as-is)는 상태 출처가 이원화돼 원칙을 위반한다:
- 보고 있는 디바이스의 자기 소스: 로컬 jbod 라이브(준비/초기화, 경로, 라이브 용량).
- 다른 디바이스의 소스: 서버 레지스트리(`device_sources`)의 마지막 보고값(디바이스
  단위 online, state 없음).

따라서 같은 소스가 디바이스마다 다른 의미·신선도로 보인다. 또한 레지스트리에 소스
단위 state(초기화 중)가 없어 다른 디바이스에서 초기화 상태를 알 수 없다.

목표: 서버 device_sources 레지스트리를 단일 진실 원천으로 삼아, 모든 디바이스가
자기 소스 포함 전부를 동일하게 렌더한다.

## Requirements

### Requirement 1: 단일 원천 통합 뷰
- WHEN 사용자가 스토리지 상태를 조회하면 THE 시스템 SHALL 그 사용자의 모든 디바이스의
  모든 소스를 device_sources 레지스트리에서 구성해 반환한다(보고 있는 디바이스 포함).
- THE 동일 사용자의 임의의 두 온라인 디바이스에서 동시 조회한 결과는 (레지스트리 동기
  시점 기준) 동일한 소스 집합·상태·용량을 보여야 한다.
- THE 보고 있는 디바이스의 소스에는 "이 기기" 표식을 부여한다(관리 동작 게이팅용).
  데이터 자체는 레지스트리에서 온다.

### Requirement 2: 상태 어휘 통일
- THE 각 소스의 표시 상태 SHALL {오프라인, 초기화 중, 준비됨} 중 하나다.
  - IF 소스의 디바이스가 오프라인(heartbeat 만료)이면 THE 상태 SHALL "오프라인".
  - IF 디바이스 온라인이고 소스 state=initializing이면 THE 상태 SHALL "초기화 중".
  - 그 외(디바이스 온라인 + state=ready) THE 상태 SHALL "준비됨".

### Requirement 3: state 보고(신선도)
- WHEN 데몬이 소스를 신고할 때 THE 데몬 SHALL state(ready|initializing)·capacity_bytes·
  used_bytes를 포함한다(경로·파일명은 비포함 — zero-knowledge).
- WHEN 소스가 추가/포맷 완료/디태치/사용량 유의미 변동(≥64 MiB 또는 ≥5%)되면 THE
  데몬 SHALL 즉시 재신고한다.
- WHILE 데몬이 온라인이면 THE 데몬 SHALL 최소 60초 주기로 재신고한다(누락 보정).

### Requirement 4: 관리 동작은 디바이스-로컬
- THE 소스 추가/디태치 SHALL 그 소스를 소유한 디바이스에서만 허용한다. 다른 디바이스의
  소스는 상태만 표시하고 관리 버튼은 비활성(현행 유지).

### Requirement 5: 마이그레이션(무손실)
- THE device_sources에 `state TEXT NOT NULL DEFAULT 'ready'` 컬럼을 ADD COLUMN으로
  추가한다. 기존 행은 'ready'로 채워진다(데이터 손실 없음).
- 롤백: 구버전 서버/클라이언트는 state를 읽지 않으므로 컬럼이 있어도 무해(전방·후방
  호환). 백업은 표준 DB 백업 절차를 따른다.

### Requirement 6: 오프라인/실패 동작(강등)
- IF 서버 미도달/미로그인이면 THE GUI SHALL 통합 뷰 대신 이 디바이스의 로컬 소스만
  라이브로 표시하고 "오프라인: 다른 디바이스 상태 미상"을 안내한다(일관 뷰는 온라인
  전제, 오프라인은 명시적 강등).
- IF 어떤 디바이스가 한 번도 신고하지 않았으면 THE 그 디바이스 소스는 어디에서도
  표시되지 않는다(모든 디바이스에서 동일).

## 비기능
- 백업 상태(완료/대기/미백업)는 이미 replication_status 전역 동기화로 일관됨 — 동일
  원칙으로 정렬 유지(본 스펙 범위는 스토리지 용량·가용 상태).
- 추가 네트워크 부담 최소화: 조회는 경량 인증 HTTP(GET /devices, /devices/sources)만
  사용(현행과 동일), P2P/open_online 미사용.
