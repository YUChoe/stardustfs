---
inclusion: manual
---

# 홀펀칭 직접 전송 (UPnP 폐지 + UDP 데이터 채널)

## 배경
직접 P2P는 현재 httpx HTTP(TCP)로 광고된 IP:port에 연결하는 방식이라 NAT 뒤에서는
포트 매핑(UPnP)이 성공해야만 도달했다. UPnP는 환경 의존적이고 이중 NAT에서 무력하며,
릴레이는 서버 대역폭을 쓰는 최후 수단이자 상품 정책으로 제한된다. 따라서 NAT 뒤에서도
서버를 거의 경유하지 않고 직접 연결을 여는 UDP 홀펀칭을 실제 데이터 전송 경로로
승격한다. UPnP 기능은 폐지한다.

holepunch.py는 이미 랑데부 등록 + 동시 오픈(reachability 확인)까지 있으나, 펀치로 연
UDP 경로 위에 데이터 전송이 없다. 이 스펙은 그 위에 신뢰성 UDP 메시지 채널을 얹어
P2P op(read/write/exists/list/space, replica_store/fetch/delete)를 직접 전송한다.

## Requirements

### Requirement 1: UPnP 폐지
#### Acceptance Criteria
1. THE 클라이언트 SHALL UPnP 포트 매핑 코드·의존성(async-upnp-client)·호출을 제거한다
   (device_manager setup_upnp/teardown_upnp/IGD, requirements, startup 호출).
2. THE 제거 SHALL reflexive 주소 조회 등 NAT 보조(홀펀칭에 쓰이는) 기능을 깨지 않는다.
3. 회귀 테스트 그린(UPnP 전용 테스트는 제거/대체).

### Requirement 2: 랑데부 활성화 + 등록
#### Acceptance Criteria
1. THE daemon SHALL 시작 시 서버 UDP 랑데부에 등록해 자신의 reflexive UDP 주소를
   학습하고, 그 주소를 라우팅에 쓸 수 있게 서버에 게시한다.
2. THE 서버 랑데부 SHALL 같은 사용자 두 디바이스의 reflexive 주소를 교환하고 punch
   신호를 중개한다(토큰 검증, 같은 user_id만).
3. IF 랑데부/펀치가 실패하면 THE 클라이언트 SHALL 릴레이(정책 허가 시)로 fallback한다.

### Requirement 3: 신뢰성 UDP 메시지 채널 (rudp)
#### Acceptance Criteria
1. THE rudp SHALL 임의 크기 메시지(요청/응답, 최대 청크 4MiB+)를 MTU 이하 데이터그램으로
   분할(fragment)하고 재조립(reassemble)한다.
2. THE rudp SHALL 데이터그램 유실에 대해 ack/재전송(타임아웃·재시도 상한)으로 신뢰
   전달을 보장하고, 전 구간 실패 시 규격 오류를 반환한다.
3. THE rudp SHALL 순수 파이썬(표준 asyncio/socket)만 사용한다(C 의존 없음).
4. THE rudp SHALL 요청-응답 1:1 매칭(msg_id)으로 동시 다중 요청을 구분한다.

### Requirement 4: P2P op over UDP + 전송 선택
#### Acceptance Criteria
1. THE 클라이언트 SHALL 직접 전송을 우선순위 (1) 펀치된 직접 UDP → (2) 릴레이(정책
   허가 시)로 시도한다(직접 우선, 릴레이 최후).
2. THE UDP P2P 서버 SHALL 기존 dispatch(op, payload) 로직을 재사용해 동일한 op
   의미를 제공한다(인가는 기존과 동일: 같은 user 토큰, 복제본은 소유자=요청자).
3. THE 전송 SHALL replica_store/fetch(4MiB 청크)를 직접 UDP로 왕복 성공시킨다.

### Requirement 5: 검증
#### Acceptance Criteria
1. rudp 단위/PBT: fragment∘reassemble 항등, 유실·재정렬 하에서 신뢰 전달, 타임아웃 오류.
2. 2-호스트(또는 로컬 2-소켓) e2e: 펀치→직접 UDP로 replica 왕복 바이트 일치.
3. 펀치 실패 시 릴레이(허가) fallback, 릴레이 비허가 시 규격 오류.
