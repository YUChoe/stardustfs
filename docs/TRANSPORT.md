# StardustFS 전송 계층 (Transport)

디바이스 간 파일·복제본 전송이 NAT/방화벽 뒤에서도 동작하도록, StardustFS는
직접 TCP → 홀펀칭 UDP → 서버 릴레이의 3단 캐스케이드를 사용한다. 이 문서는 전송
경로, 홀펀칭 구성요소, 데몬 전송 위임, 스필오버 시 홀더 측 소스 선택을 정리한다.

상위 아키텍처는 [ARCHITECTURE.md](./ARCHITECTURE.md), 제품 방향은
[ROADMAP.md](./ROADMAP.md)를 참조한다.

## 전송 캐스케이드

같은 사용자의 다른 디바이스(파일 op) 또는 임의 홀더(복제본 op)로 전송할 때, 다음
순서로 시도하고 앞 단계가 실패하면 다음으로 내려간다.

1. 직접 TCP (같은 LAN 전용) — 상대가 광고한 접속 주소로 HTTP P2P 요청. 디바이스는
   로컬(LAN) 주소를 광고한다. 사용자에게 라우터 포트포워딩을 기대하지 않으므로 공인
   주소 보정(reflexive)은 하지 않는다. 따라서 이 단계는 같은 LAN에 있을 때만 성립하고,
   그때는 가장 빠르며 서버 대역폭을 쓰지 않는다.

   도달 가능성이 없는 주소(다른 네트워크의 사설 IP)는 아예 시도하지 않고 곧바로 2단계로
   내려간다(`remote_source.direct_tcp_viable`: 상대가 사설이면 내 /24 서브넷일 때만
   시도, 공인·루프백·호스트명은 시도). 시도하는 경우에도 연결 단계만 짧게 잡아
   (`DIRECT_CONNECT_TIMEOUT` 2s, 복제 홀더는 `DIRECT_HOLDER_TIMEOUT` 3s) 낭비를 막고,
   읽기/쓰기 타임아웃은 대용량 LAN 전송을 막지 않도록 길게 유지한다.
2. 홀펀칭 UDP — 서버 랑데부로 상대 주소를 학습하고 양방향 UDP 펀치로 직접 경로를
   연 뒤, 신뢰성 UDP(rudp) 위에서 P2P op를 전송한다. 대부분의 가정용 NAT(full/
   restricted cone)를 라우터 설정 없이 통과한다.
3. 서버 릴레이 — 홀펀칭마저 실패(symmetric NAT/CGNAT)할 때만, 그리고 서버 정책이
   허가할 때만 사용하는 최후 수단. 서버는 암호문 blob만 중계하고 내용을 보지 못한다.

전송 우선순위(직접 가능하면 릴레이를 쓰지 않음)는 홀펀칭 스펙의 Correctness
Property 3으로 명문화돼 있다.

## 홀펀칭 구성요소

UPnP는 폐지됐고(라우터 포트포워딩 의존 제거), 서버 HTTP로 공인 IP를 조회해
`connection_address`를 보정하던 경로도 함께 폐지됐다(`GET /network/reflexive` 제거).
다른 네트워크로의 NAT 트래버설은 전부 UDP 홀펀칭이 담당한다. 아래의 reflexive는
랑데부가 학습하는 UDP 매핑을 말하며, 폐지된 HTTP 공인 IP 조회와는 다르다.

- `stardustlib/rudp.py` — 순수 파이썬 신뢰성 UDP. 고정 11바이트 헤더(magic `RU`,
  type DATA/ACK, msg_id u32, frag_idx/frag_count u16)로 메시지를 ~1200B 프래그먼트로
  분할·재조립하고, 프래그먼트별 ACK·타임아웃 재전송·중복 억제를 처리한다. 대용량
  전송(예: 12MiB)에서 한 메시지를 한꺼번에 보내 멈추던 문제를 막기 위해 송신
  윈도우(`DEFAULT_WINDOW`, 256 in-flight)와 무진전 재시도 상한을 둔다. 상한 초과 시
  조용한 손실 없이 `TimeoutError`.
- `stardustlib/p2p_udp.py` — rudp 위의 P2P op 요청/응답. REQ/RESP 1바이트 태그 +
  JSON, 요청 msg_id를 응답에 에코해 매칭한다. 서버 측은 `P2PServer.dispatch_async`를
  그대로 재사용하므로 인가 규칙이 TCP 경로와 동일하다.
- `stardustlib/holepunch_service.py` — 하나의 UDP 소켓을 공유해 (1) 랑데부 제어
  (register/connect/punch, JSON·PUNCH/ACK)와 (2) rudp 데이터(P2P op)를 다중화한다.
  수신 데이터그램 앞 2바이트가 rudp magic이면 rudp로, 아니면 제어 큐로 보낸다.
  데몬이 시작 시 register로 reflexive UDP 주소를 학습하고 keepalive(20s)로 갱신한다
  (ISP/CGNAT의 UDP NAT 매핑 타임아웃이 50초 미만이라 그 안에 최소 2회 갱신). per-peer
  펀치 상태는 펀치 동안에만 유지하고 끝나면 정리해 오프라인 피어 세션을 남기지 않는다.
  랑데부 호스트명은 IPv4로 미리 해석한다(asyncio UDP `sendto`가
  호스트명을 해석하지 않아 패킷이 잘못 전달되던 문제 회피).
- 서버 `rendezvous.py`(옵트인 `rendezvous_enabled`) — STUN+시그널링을 겸하는 랑데부.
  register에 reflexive 주소를 회신하고, connect 시 양쪽에 상대 주소 + punch 신호를
  보내 동시 오픈을 유도한다.

## 데몬 전송 위임

GUI/CLI는 단발 프로세스라 홀펀칭 세션(상주 UDP 소켓·랑데부 등록)을 가질 수 없다.
따라서 전송(put/get)을 항상-온라인 데몬에 위임한다(`stardustlib/daemon_control.py`).

- 데몬이 127.0.0.1의 임의 포트에 제어 서버를 띄우고 `{port, token}`을 소유자 전용
  제어 파일(`{metadata_db}.daemon.ctl.json`, 0o600)에 기록한다.
- GUI/CLI는 제어 파일을 읽어 `POST /ctl/put`·`/ctl/get`을 `X-Ctl-Token`으로 호출한다.
  같은 머신이므로 데이터가 아니라 로컬 경로만 전달한다.
- 데몬이 없거나 연결 실패면 호출자가 직접 수행으로 fallback한다(홀펀칭 없이 직접
  TCP→릴레이만 가능).

## 스필오버와 홀더 측 소스 선택

로컬 소스가 모두 만석이면 신규 파일을 온라인 리모트 디바이스(같은 계정)로 스필오버
한다(`storage_pool._write_to_remote` → `RemoteSource.push_blob` → 위 캐스케이드).
홀더는 `p2p_server._op_write`로 암호문을 받아 자신의 소스에 저장한다.

스필오버 쓰기에는 `source_id`가 없으므로, 홀더는 페이로드 크기 이상의 여유가 있는
소스를 `StoragePool.select_source(len(data))`로 골라 저장하고, 사용한 소스 id를
응답한다. 소유자는 그 id를 메타데이터에 기록해 이후 get이 정확한 소스를 가리킨다.
(과거에는 첫 소스를 맹목적으로 골라, 작은 루프백(10MiB)에 큰 파일(12MiB)을 못 담고
HTTP 500을 내던 버그가 있었다 — 2026-06-07 수정.) 맞는 소스가 없으면 HTTP 507.

## 대용량 파일 청크 전송

단일 `/p2p/write`/`/p2p/read`로는 rudp 단일 메시지 한계(frag_count u16 × 1200B ≈
78.6MB)·홀더 `MAX_WRITE_SIZE`(100MB)·릴레이 본문 한계를 넘는 파일을 전송할 수 없다
(713 MiB 스필오버가 "830670 프래그먼트 > 65535"로 실패하던 사례). 그래서 리모트
파일 쓰기/읽기를 4 MiB(`REMOTE_CHUNK_SIZE`) 청크로 분할한다. 메타데이터 모델은
그대로(리모트 파일 1개 항목)이고 청크화는 RemoteSource ↔ 홀더 전송 계층에만 있다.

- 쓰기(`push_blob`/`write` → `_push_chunked`): 데이터가 4 MiB를 넘으면 청크 루프로
  `/p2p/write_chunk`를 보낸다. 첫 청크(offset=0)에서 홀더가 `select_source(total_size)`로
  큰 소스를 고르고(없으면 507) source_id를 응답하면, 이후 청크는 그 source_id로 같은
  파일의 offset 위치에 이어 쓴다(`StorageSource.write_chunk`, seek 기반). 중간 청크
  실패 시 홀더의 부분 파일을 `/p2p/delete`로 정리하고 OSError(조용한 손실 없음).
- 읽기(`read_from_source` → `_read_chunked`): 평문 file_size가 4 MiB를 넘으면 범위
  읽기(`/p2p/read_chunk`, offset/length)로 이어붙인다. 암호문 실제 크기는 평문과
  다르므로 짧은 읽기(요청보다 적게 반환)를 EOF로 간주한다. 4 MiB 이하 파일은 기존
  단일 경로(하위호환).
- 각 청크는 기존 캐스케이드(직접 TCP → 홀펀칭 UDP → 릴레이)와 auth_token 인가를 그대로
  탄다. 4 MiB는 rudp(≈3500 프래그먼트)·홀더 100MB 한계 내라 홀펀칭 UDP로 안정 전송.
  스펙: `.kiro/specs/remote-chunked-transfer/`.

## 복제 청크 무결성 검증

복제 청크에는 내용 해시(`chunker.chunk_hash` = 암호문의 SHA-256 hex)가 붙는다. 복제
시 서버 레지스트리(`chunks.hash`)에 등록하고, 복구·재복제에서 홀더가 돌려준 바이트를
그 해시와 비교한다.

- 불일치하면 그 홀더의 응답을 버리고 같은 청크를 다음 홀더에서 재요청한다. 유효한
  사본을 가진 홀더가 하나라도 도달 가능하면 복구가 성공한다.
- 재복제(heal)는 복사 소스로 쓸 바이트를 먼저 검증해, 손상된 사본이 새 홀더로
  퍼지는 것을 막는다. 유효 소스가 없으면 `unrecoverable`로 보고한다.
- `chunk_id`는 여전히 위치 식별자(`SHA-256(file_ref:idx)`)이고 내용 해시는 별도
  필드다. 해시가 없는 레거시 청크는 검증을 생략하고(하위 호환) 다음 복제 때 채워진다.

해시 이전에는 손상이 전체 결합 후 AES-GCM 태그 실패로만 드러나 어느 청크가 문제인지
알 수 없고 부분 재시도가 불가능했다. 스펙: `.kiro/specs/chunk-integrity-hash/`.

## UDP/릴레이 인가

UDP와 릴레이 경로에는 서버 게이트키퍼가 없으므로, 파일 op 요청 본문에 소유자
`auth_token`을 실어 홀더가 직접 검증한다(`p2p_server.dispatch_async`).

- 파일 데이터 op: 요청자가 로컬 user_id와 같아야 한다(같은 사용자 디바이스 간만).
  불일치 시 401/403.
- 복제본 op(replica_*): 교차 사용자 상호 호스팅이므로 `/auth/verify`(same_user=False)로
  요청자를 도출하고, 소유자=요청자 인가는 ParityStore가 청크 단위로 집행한다.

## 실패 처리와 한계

- 홀펀칭 실패(symmetric NAT/CGNAT): 릴레이(허가 시)로 fallback, 비허가면 규격 에러.
- rudp 재시도 상한 초과: 누락 프래그먼트를 명시한 `TimeoutError`/`OSError`(조용한
  손실 없음).
- 랑데부 미등록/미응답: 홀펀칭 불가로 간주한다. 같은 LAN이면 직접 TCP로 동작하고,
  다른 네트워크면 릴레이만 남는다(허가 시). 랑데부 UDP 포트(기본 9091)가 서버/방화벽/
  보안그룹에서 열려 있어야 등록이 성립한다.
- 전제: 홀펀칭은 상대 디바이스의 데몬이 실제 온라인이고 랑데부에 등록(reflexive
  학습 완료)돼 있어야 직접 경로가 열린다.

## E2E 검증 (2026-06-07)

서로 다른 NAT 뒤의 두 호스트 간 12MiB 업로드 스필오버가 홀펀칭 UDP로 성공했다:
`직접 P2P 타임아웃 → 홀펀칭 UDP 전송 시도(write) → 펀치 성공 → 홀더 저장 → 데몬 put
완료`. rudp 송신 윈도우로 대용량 전송 멈춤이 사라졌고, 홀더 측 소스 선택 수정으로
큰 파일이 여유 있는 소스에 안착한다.

## 관련 스펙

- `.kiro/specs/holepunch-transport/` — 홀펀칭 직접 전송(rudp·p2p_udp·랑데부·캐스케이드).
- `.kiro/specs/daemon-transfer-delegation/` — 데몬 전송 위임 제어 채널.
- `.kiro/specs/storage_pool-spillover-eviction/` — 스필오버·콜드 축출.
- `.kiro/specs/cross-user-replica-relay/` — 교차 사용자 복제본 릴레이 인가.
