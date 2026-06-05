---
inclusion: manual
---

# 디바이스 소스 레지스트리 (서버 기반 리모트 스토리지 목록)

## 배경
GUI 스토리지 목록은 로컬 소스만 정확히 보여준다. 리모트(같은 계정의 다른 디바이스)
스토리지는 그 디바이스의 소스 인벤토리를 알 길이 없어 표시하지 못한다. P2P 직접
조회는 NAT 타임아웃으로 느리고 오프라인 디바이스는 조회 불가하다. 따라서 각 디바이스가
자신의 소스 인벤토리(식별자/타입/용량/사용량)를 중앙 서버에 신고하고, GUI는 서버를
조회해 모든 디바이스의 스토리지를 한 목록으로 보여준다.

zero-knowledge 유지: 신고 항목은 소스 식별자/타입/용량/사용 바이트뿐이며, 물리 경로나
파일 내용·이름은 신고하지 않는다.

## Requirements

### Requirement 1: 디바이스 소스 신고
사용자 스토리: 디바이스 daemon으로서, 내 로컬 소스 인벤토리를 서버에 신고해, 다른
디바이스의 GUI가 내 스토리지를 볼 수 있게 한다.

#### Acceptance Criteria
1. WHEN daemon이 시작되어 device가 등록되면 THE daemon SHALL 로컬 소스 인벤토리를
   서버에 신고한다(PUT /devices/{device_id}/sources).
2. THE 신고 항목 SHALL source_id, type, capacity_bytes, used_bytes만 포함하고
   물리 경로·파일명은 포함하지 않는다.
3. WHEN 신고가 실패하면(서버 오류·오프라인) THE daemon SHALL 경고 로그를 남기고
   계속 진행한다(시작 차단 금지).
4. THE 신고 SHALL 원격 소스(is_remote=true)를 제외하고 로컬 소스만 포함한다.

### Requirement 2: 디바이스 소스 조회
사용자 스토리: GUI로서, 내 모든 디바이스의 소스 인벤토리를 서버에서 조회해, 로컬·리모트
스토리지를 한 목록으로 표시한다.

#### Acceptance Criteria
1. WHEN GUI가 스토리지 목록을 열면 THE GUI SHALL 로컬 소스(실시간 용량)와 함께
   서버에 신고된 다른 디바이스의 소스를 표시한다.
2. THE 조회 응답 SHALL 각 항목에 device_id, device_name, source_id, type,
   capacity_bytes, used_bytes, is_online, updated_at을 포함한다.
3. THE GUI SHALL 자기 디바이스의 항목을 리모트 목록에서 제외한다(로컬 섹션에서 표시).
4. IF 로그인되지 않았거나 서버 도달 불가하면 THE GUI SHALL 리모트를 빈 목록으로 두고
   로컬만 표시한다(예외로 목록 전체가 비지 않게 한다).
5. THE 리모트 조회 SHALL P2P 호출 없이 서버 HTTP만 사용해, 단일 워커 스레드를
   장시간(수 초+) 막지 않는다.

### Requirement 3: 인증·격리
#### Acceptance Criteria
1. THE 신고 엔드포인트 SHALL 인증을 요구하고, 대상 device가 호출자(current_user)
   소유가 아니면 거부한다(404/403).
2. THE 조회 엔드포인트 SHALL 호출자 소유 디바이스의 소스만 반환한다.
3. WHEN device가 삭제되면 THE 서버 SHALL 해당 device의 소스 레코드를 함께 삭제한다
   (ON DELETE CASCADE).

### Requirement 4: 마이그레이션
#### Acceptance Criteria
1. THE 서버 SHALL device_sources 테이블을 IF NOT EXISTS로 생성한다(기존 DB 호환).
2. 신고 전 기존 디바이스는 소스 레코드가 없으며, THE 조회 SHALL 그 디바이스를 리모트
   목록에서 생략한다(빈 인벤토리는 행 없음).
