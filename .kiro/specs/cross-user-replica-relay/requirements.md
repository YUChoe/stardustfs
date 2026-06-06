---
inclusion: manual
---

# 교차 사용자 복제본 릴레이 fallback (MVP3 이관 항목)

## 배경
MVP3 리플리케이션에서 홀더는 자기 디바이스 + 타 사용자 디바이스를 포함하는 상호
(reciprocal) 피어 네트워크다. 직접 P2P `/p2p/replica_{store,fetch,delete}`는 교차
사용자 토큰을 검증(`/auth/verify` same_user=False)하고 소유자=요청자 인가를
ParityStore가 청크 단위로 집행하므로 이미 교차 사용자 직접 전송을 지원한다.

그러나 직접 연결 실패(이중 NAT/CGNAT) 시의 릴레이 fallback은 릴레이 허브가 같은
user_id 디바이스 간만 중개하도록 막혀 있어, 타 사용자 홀더로의 복제본 전달이
릴레이로는 불가능하다(replication-parity tasks Phase 4.3·6.78 이관 항목). 이 스펙은
복제본 op에 한해 교차 사용자 릴레이를 허용한다.

## Requirements

### Requirement 1: 복제본 op 교차 사용자 릴레이 허용 (서버)
사용자 스토리: 청크 소유자로서, 직접 연결이 안 되는 타 사용자 홀더에게도 서버 릴레이로
복제본을 store/fetch/delete 할 수 있어야 한다.

#### Acceptance Criteria
1. WHEN `/relay/request`의 op가 복제본 op(replica_store/replica_fetch/replica_delete)
   이면 THE 서버 SHALL 대상 디바이스 소유자가 요청자와 달라도 릴레이를 허용한다.
2. WHEN op가 복제본 op가 아니면(파일 read/write 등) THE 서버 SHALL 기존대로 대상
   디바이스 소유자=요청자(같은 user_id)를 요구한다(403 DeviceAccessDenied).
3. THE 서버 SHALL payload/result를 불투명 blob으로 중계하며 해석·영속화하지 않는다
   (기존 zero-knowledge 유지).

### Requirement 2: 홀더의 요청자 신원 도출 (클라이언트)
사용자 스토리: 홀더로서, 릴레이로 받은 복제본 op의 요청자를 로컬 사용자로 가정하지
않고 실제 소유자(토큰)로 인가해야 한다.

#### Acceptance Criteria
1. WHEN 릴레이 워커가 복제본 op를 수신하면 THE 클라이언트 SHALL payload의 auth_token을
   `/auth/verify`(same_user=False)로 검증해 요청자 user_id를 도출하고, 그 요청자로
   ParityStore 인가를 집행한다.
2. IF auth_token이 없거나 무효이면 THE 클라이언트 SHALL 401을 반환한다.
3. THE 변경 SHALL 기존 동기 dispatch(파일 op read/write/exists 등) 동작과 그 테스트를
   바꾸지 않는다(복제본 경로만 비동기 토큰 검증 추가).

### Requirement 3: 릴레이 payload에 소유자 토큰 포함 (클라이언트)
#### Acceptance Criteria
1. WHEN _holder_store/_holder_fetch가 릴레이로 fallback하면 THE 클라이언트 SHALL 릴레이
   payload에 소유자 auth_token을 포함한다(직접 경로와 동일).
2. THE 직접 경로의 동작(직접 200→성공, 직접 비-200→릴레이 안 함)은 변경하지 않는다.

### Requirement 4: 인가·격리 (보안 불변)
#### Acceptance Criteria
1. THE 홀더 ParityStore SHALL chunk_id별 소유자=요청자 인가를 그대로 집행한다(요청자가
   소유자와 다르면 403). 릴레이 경유라도 동일.
2. THE 변경 SHALL 파일 데이터 op(read/write/list 등)의 교차 사용자 릴레이를 허용하지
   않는다(복제본 op 화이트리스트만).
3. THE 릴레이된 복제본은 소유자 키로 암호화된 불투명 청크이며 홀더·서버는 복호화할 수
   없다(zero-knowledge 유지).
