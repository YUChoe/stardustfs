---
inclusion: manual
---

# 홀펀칭 직접 전송 — Tasks

## Phase 1: UPnP 폐지 (먼저 적용·커밋)
- [x] 1.1 device_manager: UPnP import/_HAS_UPNP/_UPNP_LEASE_DESCRIPTION/_upnp_* 필드/
      setup_upnp/teardown_upnp 제거, stop()의 teardown 호출 제거. reflexive·주소 헬퍼 유지.
- [x] 1.2 stardustfs.py: setup_upnp() 호출 제거.
- [x] 1.3 requirements.txt: async-upnp-client 제거.
- [x] 1.4 UPnP 전용 테스트 없음(유지 헬퍼만 테스트됨), 회귀 그린. ARCHITECTURE/ROADMAP
      UPnP 언급 정리.

## Phase 2: 랑데부 활성화 + 등록
- [ ] 2.1 daemon 시작 시 HolePunchSession.register로 reflexive UDP 학습.
- [ ] 2.2 reflexive UDP 주소를 device 라우팅에 게시(서버 routing/devices 확장 또는 신규).
- [ ] 2.3 서버 rendezvous 기본 활성(설정) + 같은 user connect 중개 확인(기존 테스트 활용).

## Phase 3: 신뢰성 UDP 메시지 채널 (rudp.py) — 완료
- [x] 3.1 프레임 인코딩/디코딩(_encode/_decode, 11B 헤더) + fragment(). 항등 테스트.
- [x] 3.2 RudpEndpoint: feed/send/recv. 프래그먼트별 ACK + 타임아웃 재전송 + 재조립,
      완성 메시지 1회 전달(중복 방지). 가짜 라우터로 유실·재정렬·타임아웃·중복 테스트(13종).
- [x] 3.3 msg_id 다중화(send에 msg_id 지정 가능 → 응답 에코용). RudpProtocol 소켓 어댑터.

## Phase 4: P2P over UDP + 전송 선택
- [x] 4.1 p2p_udp.P2pUdpNode: rudp 위 P2P op 서버(dispatch 주입=dispatch_async 재사용)
      /클라이언트 send_op. REQ/RESP 태그 + msg_id 에코 매칭. rudp 송신 상태를
      (addr,msg_id)로 키잉(에코 응답 충돌 방지). 테스트 4종(소/대(4MiB)/오류/동시).
- [ ] 4.2 daemon UDP 수신 루프(펀치 수락 + P2pUdpNode 기동) — Phase 2 이후.
- [ ] 4.3 replication_manager/remote_source 직접 전송을 펀치 UDP→릴레이(정책) 순으로
      대체 — Phase 2(피어 UDP 주소 학습) 이후.

## Phase 5: 검증/문서
- [ ] 5.1 통합 e2e(펀치→직접 UDP replica 왕복 일치, 펀치 실패→릴레이 fallback).
- [ ] 5.2 ARCHITECTURE/ROADMAP 갱신(UPnP 폐지, 홀펀칭 전송).

## 비범위
- QUIC/KCP 등 외부 신뢰성 전송 라이브러리(순수 파이썬 유지).
- 윈도우/혼잡 제어 고도화(초기엔 단순 stop-and-wait/selective-ack 수준).
