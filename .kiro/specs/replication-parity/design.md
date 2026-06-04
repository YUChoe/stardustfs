---
inclusion: manual
---

# 암호화 리플리케이션(패리티 백업) — Design

## 개요

암호화 청크를 ≥3개 홀더에 복제하고, 서버가 위치 레지스트리·배치·건강성·호혜 회계를
담당한다. 호스트는 암호문만 보관(복호화 불가). 전송은 P2P 직접 + UDP 홀펀칭 + 서버
릴레이이며, ≥3 복제의 스웜 중복성으로 도달 가능한 홀더에서 받는다.

## torrent/NAT 검토 결과 (설계 반영)

BitTorrent의 NAT 통과(UPnP/NAT-PMP, μTP UDP, DHT, BEP-55 홀펀칭)는 풀콘/제한콘
NAT에는 효과적이나 symmetric NAT·CGNAT·double NAT은 해결하지 못한다(포트 예측 실패,
데이터 릴레이 부재). 따라서 풀 BitTorrent 채택은 비권장(추가로 libtorrent는 C++ 빌드
의존 → 무-C 원칙 충돌). 차용할 것은 (1) 스웜 중복성(≥3 복제 + 도달 가능한 홀더
연결)과 (2) 선택적 μTP식 UDP 홀펀칭이며, symmetric/CGNAT의 보장된 fallback으로 기존
서버 릴레이(TURN 등가)를 유지한다.

## Components and Interfaces

### 클라이언트 (stardustlib/)
- `replication_manager.py`(신규): 백업/복구 오케스트레이션.
  - `replicate(virtual_path)`: 암호문 청크 분할 → 서버에 placement 요청 → 각 청크를
    배치된 홀더에 push(전송 계층) → ack 수집 → 레지스트리 확정 → ≥3 확보 시 파일을
    replicated로 표시(아니면 pending).
  - `recover(virtual_path)`: 레지스트리 조회 → 청크별 도달 가능한 홀더에서 fetch →
    결합 → 복호화 → 로컬 복원.
- `chunker.py`(신규): 암호문 ↔ 고정 크기 청크 분할/결합. `split(blob)->[(idx,bytes)]`,
  `join(parts)->blob`. (암호화는 기존 encryption_engine; 청킹은 암호문 위에서 수행.)
- `parity_store.py`(신규): 호스트 역할. 타 사용자 청크 암호문을 로컬 영역에 저장/조회/
  삭제, 쿼터 집행, 인가 검증. P2P 서버의 신규 op(store/fetch/delete replica)로 노출.
- 전송: 기존 `remote_source`/`p2p_server`/`relay_*` 확장 + `holepunch.py`(신규, 선택)
  — 순수 파이썬 UDP 홀펀칭(서버 랑데부로 양쪽 동시 오픈). 실패 시 릴레이.

### 서버 (../stardustfs-server)
- `replication_service.py`(신규):
  - placement: 용량·온라인·호혜 균형으로 ≥3 홀더 후보 선정.
  - registry: `chunks`(chunk_id, owner_user_id, size) / `replicas`(chunk_id,
    holder_device_id, status, updated_at).
  - health: 홀더 heartbeat로 online 복제 수 집계, <3 지속 시 재복제 큐잉.
  - 회계: device별 provided_bytes / hosted_bytes, 호혜 쿼터(0.5·provided) 집행.
- 라우터(신규): `/replication/placement`(배치 요청), `/replication/replicas`
  (등록/조회), `/replication/health`(재복제 트리거). 인증은 access_token.
- 인가: 복제본 접근 토큰(share_token 인프라 승계) — 소유자가 홀더에서 청크를 fetch할
  권한을 서버가 위임 검증(`/auth/verify` 경로 재사용).

## Data Models

서버 신규 테이블(스키마 변경 — 마이그레이션 필요):
```
chunks:   id(PK chunk_id) | owner_user_id(FK) | file_ref | idx | size | created_at
replicas: id(PK) | chunk_id(FK) | holder_device_id(FK) | status(active|stale) | updated_at
            UNIQUE(chunk_id, holder_device_id)
hosting:  device_id(PK,FK) | provided_bytes | hosted_bytes | updated_at
```
- 서버는 청크 내용·키를 저장하지 않는다(위치/회계 메타데이터만).
- 클라이언트 메타데이터(files)에 replication_status(none|pending|replicated) 추가.

## Correctness Properties

### Property 1: ≥3 복제 불변식
*임의의* 파일이 replicated로 표시되면, 그 파일의 모든 청크는 서로 다른 ≥3개 홀더에
복제본을 가진다.

### Property 2: zero-knowledge 유지
*임의의* 복제 동작에 대해, 서버는 청크 암호문·평문·키를 저장하지 않고(위치/크기/회계
메타데이터만), 홀더는 복호화 불가능한 암호문만 보관한다.

### Property 3: 복구 라운드트립
*임의의* 원본 파일에 대해, 청크 분할→복제→(도달 가능한 홀더에서)fetch→결합→복호화는
원본 바이트와 정확히 일치한다.

### Property 4: 가용성 명시
*임의의* 파일이 ≥3 복제를 확보하지 못하면, 상태는 pending이며 사용자에게 경고된다
(silent 완료 금지).

### Property 5: 호스트 비가독
*임의의* 복제본 접근에 대해, 홀더는 암호문만 반환하며 소유자 키 없이는 내용을 복원할
수 없다.

### Property 6: 호혜 회계 정합
*임의의* 시점에 device의 hosted_bytes는 실제 보관 중인 타 사용자 청크 합과 일치하고,
무료 device의 가용 복제 제공량은 0.5·provided_bytes를 기준으로 집행된다.

## Error Handling

- 홀더 부족(<3 배치 불가): 파일 pending + 경고 + 재시도 큐(규격 에러 아님, 차단 아님).
- 홀더 쿼터 초과: store 거부(규격 에러) → 서버가 다른 홀더 재배치.
- 청크 fetch 전 홀더 도달 불가: 다음 홀더 시도. 모든 홀더 실패 시 복구 규격 에러
  (누락 chunk_id 명시).
- 전송 실패: 직접→홀펀칭→릴레이 순 fallback. 모두 실패 시 해당 홀더만 실패 처리.
- 재복제 폭주 방지: device당 동시 재복제 수 상한 + 백오프.

## Testing Strategy

- 단위: chunker split/join 라운드트립(PBT: 임의 크기·청크 경계), parity_store 저장/
  조회/쿼터/인가, placement 회계(호혜 균형).
- 서버: 레지스트리 등록/조회, 건강성 집계(<3 트리거), 호혜 쿼터 집행, 인가 거부.
- E2E(로컬 서버 + 다중 device): 파일 replicate → 소스 device 중지 → 다른 홀더에서
  recover → 바이트 일치. 홀더 1개만 도달 가능한 상황에서 복구 성공(스웜).
- 가용성: 홀더 부족 시 pending 표시 + 경고 + 홀더 확보 후 자동 완료.
- 전송: 홀펀칭 성공/실패→릴레이 fallback 경로.

## 마이그레이션 / 배포

- 서버 스키마 신규 테이블(chunks/replicas/hosting) 추가 → DB 마이그레이션. 기존
  데이터 영향 없음(신규 기능).
- 클라이언트 files 테이블에 replication_status 컬럼 추가(기존 NULL=none 호환).
- 단계적 롤아웃: 미배포 서버에서는 replication 엔드포인트 404 → 클라이언트가 기능
  비활성(기존 동작 유지).

## 단계 (요약, tasks.md 상세)

1. chunker + 클라이언트 메타 replication_status.
2. 서버 레지스트리/배치/회계 + 라우터 + 스키마 마이그레이션.
3. parity_store(호스트 역할) + P2P replica op + 인가.
4. replication_manager(replicate/recover) + 전송 fallback.
5. UDP 홀펀칭(선택, 서버 랑데부) — 직접 연결 폭 확대.
6. 건강성/재복제 + 호혜 쿼터 집행.
7. E2E + 문서 + 로드맵 갱신.
