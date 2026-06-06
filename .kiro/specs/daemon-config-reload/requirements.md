---
inclusion: manual
---

# daemon config 리로드 + 소스 인벤토리 수시 신고

## 배경
daemon은 시작 시에만 config의 sources를 mount하므로, GUI가 소스를 추가/분리해도 실행
중인 daemon에 반영되지 않아 전체 재시작이 필요했다(서비스 일시 중단). 또 소스 인벤토리
신고가 5분 주기라 리모트 디바이스의 용량/목록 반영이 느렸다.

## Requirements

### Requirement 1: 소스 인벤토리 신고 주기 단축 + 변동 시 즉시 신고
#### Acceptance Criteria
1. THE daemon SHALL 소스 인벤토리를 기본 60초 주기로 재신고한다(설정
   `p2p.source_report_interval_seconds`, 기본 60).
2. WHEN 소스 목록이 변경(config 리로드)되면 THE daemon SHALL 주기를 기다리지 않고
   즉시 재신고한다.

### Requirement 2: daemon config 리로드 신호(무중단 재조립)
#### Acceptance Criteria
1. THE daemon SHALL 리로드 센티넬을 감지하면 P2P/동기화/heartbeat를 중단하지 않고
   config의 로컬 소스만 다시 읽어 JBODManager의 소스를 교체(remount)한다.
2. THE 리로드 SHALL 기존 원격 소스(RemoteSource)·복구 콜백을 보존하고 로컬 소스만
   교체한다.
3. WHEN 리로드가 완료되면 THE daemon SHALL 갱신된 인벤토리를 즉시 서버에 재신고한다.
4. THE GUI SHALL 소스 추가/분리 후 전체 재시작 대신 리로드 신호를 보낸다.
5. IF 리로드 중 오류가 발생하면 THE daemon SHALL 기존 소스 구성을 유지하고 로그를
   남긴다(중단 금지).

### Requirement 3: 호환·안전
#### Acceptance Criteria
1. THE 리로드 센티넬 SHALL 정지 센티넬과 별개 파일이며, daemon 시작 시 잔존 센티넬을
   정리한다.
2. THE JBODManager 소스 교체 SHALL p2p_server/sync_client 등 기존 jbod 참조를 깨지
   않도록 같은 객체를 in-place 갱신한다(객체 교체 아님).
