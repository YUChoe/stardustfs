---
inclusion: manual
---

# 홀펀칭 직접 전송 — 설계

## 개요
UPnP를 폐지하고, UDP 홀펀칭으로 연 직접 경로 위에 신뢰성 UDP 메시지 채널(rudp)을
얹어 P2P op를 직접 전송한다. 직접 UDP 실패 시에만 릴레이(정책 허가)로 fallback.

## Components and Interfaces

### Phase 1: UPnP 폐지
- device_manager.py: async_upnp_client import 블록·_HAS_UPNP·_UPNP_LEASE_DESCRIPTION·
  _upnp_* 필드·setup_upnp·teardown_upnp 제거. stop()의 teardown_upnp 호출 제거.
  query_reflexive_ip·_is_private_or_cgnat_ip·set/get_connection_address는 유지(NAT 보조).
- stardustfs.py: device_mgr.setup_upnp() 호출 제거.
- requirements.txt: async-upnp-client 제거.
- 테스트: UPnP 전용 테스트 제거.

### Phase 2: 랑데부 활성화 + 등록
- 서버 rendezvous_enabled 기본 True(또는 배포 설정). daemon이 시작 시 HolePunchSession로
  register → reflexive UDP (ip,port) 학습. 이 reflexive 주소를 device의 라우팅 정보로
  서버에 게시(connection_address와 별개의 udp_addr, 또는 routing 응답 확장).
- 라우팅: 피어의 udp_addr를 얻기 위해 랑데부 connect(서버가 양쪽에 상대 주소+punch 신호).

### Phase 3: 신뢰성 UDP 메시지 채널 (stardustlib/rudp.py)
- 프레임 헤더(고정 바이트): magic, msg_id(u32), type(REQ/RESP/ACK/FIN), frag_idx(u16),
  frag_count(u16), payload_len. 데이터그램 페이로드 ≤ ~1200B(MTU 보수).
- 송신: 메시지를 프래그먼트로 분할, 각 프래그먼트 전송 후 ACK 대기(선택적 반복/윈도우).
  타임아웃 시 미-ack 프래그먼트 재전송, 재시도 상한 초과 시 실패.
- 수신: frag_count만큼 모이면 재조립해 상위로 전달, 수신 프래그먼트마다 ACK.
- 요청-응답: msg_id로 매칭. 한 소켓에서 다중 메시지 다중화.
- 순수 파이썬(asyncio DatagramProtocol). C 의존 없음.

### Phase 4: P2P over UDP + 전송 선택
- stardustlib/p2p_udp.py: rudp 위에서 P2P op 요청/응답. 서버측은 P2PServer.dispatch_async
  재사용(인가 동일). 클라이언트측 send_op(addr, op, payload)→(status, result).
- replication_manager._holder_store/_holder_fetch, remote_source: 직접 전송을
  (1) 펀치 UDP → (2) 릴레이(정책) 순으로. 기존 HTTP-to-advertised-IP 직접 경로 대체.
- daemon: UDP P2P 수신 루프 기동(랑데부로 펀치 수락 + rudp 서버).

## Correctness Properties

### Property 1: 프래그먼트 항등
*임의의* 바이트열 m에 대해 reassemble(fragment(m)) == m (크기·경계 무관).

### Property 2: 유실 하 신뢰 전달
*임의의* 데이터그램 유실/재정렬 시퀀스에 대해, 재시도 상한 내라면 수신 메시지는
송신 메시지와 동일하다. 상한 초과 시 규격 오류(조용한 손실 없음).

### Property 3: 전송 우선순위
*임의의* 전송에 대해, 직접 UDP(펀치)가 가능하면 릴레이를 쓰지 않는다. 릴레이는 직접
실패 시에만, 그리고 서버 정책이 허가할 때만 사용된다.

## Error Handling
- 펀치 실패(symmetric/CGNAT): 릴레이(허가) fallback, 비허가면 규격 오류.
- rudp 재시도 상한 초과: TimeoutError/OSError(누락 프래그먼트 명시).
- 랑데부 미응답: 직접 UDP 불가로 간주, fallback.

## Testing Strategy
- rudp: PBT(fragment∘reassemble 항등), 유실·재정렬 시뮬레이션(가짜 소켓)에서 신뢰 전달,
  타임아웃 오류.
- p2p_udp: 로컬 2-소켓으로 op 왕복(read/replica_store/fetch 4MiB), dispatch_async 연동.
- 통합: 펀치 성공→직접 UDP replica 왕복 바이트 일치; 펀치 실패→릴레이(허가) fallback.

## 단계 진행
Phase 1(UPnP 폐지) 우선 적용·커밋. 이후 Phase 3(rudp) → Phase 2(랑데부 등록) →
Phase 4(P2P over UDP 연결) → Phase 5(e2e) 순으로 점진 구현·검증.
