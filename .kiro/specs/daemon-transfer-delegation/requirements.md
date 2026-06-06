---
inclusion: manual
---

# 데몬 전송 위임 (GUI/CLI 전송을 데몬이 수행)

## 배경
GUI/CLI는 전송(put/get)을 자체 온라인 세션(open_online)으로 직접 수행하는데, NAT 직접
연결의 핵심인 홀펀칭 서비스는 항상-온라인 데몬(별도 프로세스)에만 있다. 그래서 로컬
만석 시 리모트 스필오버나 리모트 파일 get이 GUI 세션에서는 홀펀칭을 못 쓰고 직접
TCP(NAT 차단)→릴레이(대용량 413·정책)로 귀결돼 실패한다.

해결: 데몬이 전송을 수행한다. 데몬은 항상-온라인 + 홀펀칭 + 릴레이 정책을 한 곳에서
보유하므로, GUI/CLI는 데몬에 put/get을 위임하고 데몬이 직접 UDP(홀펀칭)→릴레이 순으로
전송한다. 데몬 미실행 시에는 기존처럼 세션이 직접 수행(로컬 위주)한다.

## Requirements

### Requirement 1: UDP/릴레이 파일 op 인증 (보안 전제)
#### Acceptance Criteria
1. WHEN P2P 파일 op(read/write/list/exists/mkdir/rmdir/space 등)가 UDP(홀펀칭) 또는
   릴레이로 수신되면 THE 홀더 SHALL payload의 auth_token을 검증(same_user=True)한 뒤
   처리한다. 토큰 없음/무효/타 사용자면 401/403.
2. THE 복제본 op는 기존대로 same_user=False + ParityStore 소유자 인가.
3. THE 직접 TCP(HTTP) op는 기존 _parse_and_verify(토큰 검증)를 유지한다.
4. THE 클라이언트 SHALL UDP/릴레이 파일 op payload에 auth_token을 포함한다.

### Requirement 2: RemoteSource 홀펀칭 UDP 전송
#### Acceptance Criteria
1. THE RemoteSource SHALL 직접 TCP 도달 불가 시 릴레이 전에 홀펀칭 UDP를 시도한다
   (직접 TCP → 직접 UDP → 릴레이). udp_send 콜백 주입 방식.
2. THE 대용량 blob(파일/청크)은 rudp 분할로 전달된다(413 회피).

### Requirement 3: 데몬 전송 위임 채널
#### Acceptance Criteria
1. THE 데몬 SHALL 로컬 제어 채널(127.0.0.1 전용 + 토큰)을 열어 put/get 명령을 받는다.
   포트·토큰은 소유자 전용 제어 파일에 기록한다.
2. THE 데몬 SHALL put(virtual_path, local_path)에서 파일을 읽어 write_file(로컬 우선,
   만석 시 홀펀칭으로 리모트 스필오버)하고 메타데이터를 업로드한다.
3. THE 데몬 SHALL get(virtual_path, local_path)에서 read_file(로컬/원격 홀펀칭)로
   복호화해 로컬에 저장한다.
4. THE GUI/CLI SHALL 데몬이 실행 중이면 put/get을 데몬에 위임하고, 미실행이면 기존
   세션 직접 수행으로 fallback한다.

### Requirement 4: 안전·격리
#### Acceptance Criteria
1. THE 제어 채널 SHALL 127.0.0.1 바인딩 + 제어 파일 토큰으로만 접근 가능.
2. THE 위임 SHALL zero-knowledge·소유권 모델을 유지한다(데몬도 같은 사용자/키).
