---
inclusion: manual
---

# 암호화 리플리케이션(패리티 백업) — Tasks

규모가 크고 클라이언트+서버 양쪽 + 스키마 마이그레이션을 포함한다. 각 Phase 후 양쪽
pytest 그린 유지, 전송/복구는 로컬 서버 E2E로 검증.

## Phase 1: 청킹 + 클라이언트 상태
- [x] 1.1 `stardustlib/chunker.py`: split(blob, size=4MiB)->[(idx,bytes)], join(parts).
- [x] 1.2 PBT: 임의 크기·경계에서 join(split(x))==x.
- [x] 1.3 metadata_store: files에 replication_status(none|pending|replicated) 컬럼
      마이그레이션(기존 NULL=none 호환).

## Phase 2: 서버 레지스트리/배치/회계
- [x] 2.1 스키마 마이그레이션: chunks / replicas / hosting 테이블 + 인덱스
      (DATABASE_SCHEMA.md 갱신, 백업+롤백 명시).
- [x] 2.2 `replication_service.py`: placement(용량·온라인·호혜), registry 등록/조회,
      hosted/provided 회계.
- [x] 2.3 라우터 `/replication/placement|replicas|health` (access_token 인증).
- [x] 2.4 서버 테스트: 배치 ≥3, 호혜 쿼터 집행, 레지스트리 조회, 인가 거부.

## Phase 3: 호스트 역할 (parity store)
- [x] 3.1 `parity_store.py`: 타 사용자 청크 저장/조회/삭제, 쿼터 집행, 인가 검증.
- [x] 3.2 P2P 서버에 replica op 추가: store/fetch/delete (인가 토큰 검증).
- [x] 3.3 인가: `/auth/verify` 위임에 same_user=False 경로 추가, 소유자=요청자
      인가는 ParityStore가 청크 단위로 집행. 데몬은 p2p.parity_enabled 시 활성.
- [x] 3.4 단위 테스트: 저장/조회/쿼터 초과 거부/비인가 거부/호스트 비가독
      (`tests/test_parity_store.py`, 12개).

## Phase 4: 복제 매니저 + 전송
- [x] 4.1 `replication_manager.replicate`: 평문→암호문 blob 암호화 → 청크 분할 →
      청크 등록 + placement → 홀더 직접 push → ack → record_replica →
      모든 청크 ≥min_replicas(기본 3) 확보 시 replicated, 아니면 pending(경고).
      file_ref/chunk_id는 가상경로 SHA-256(서버에 경로 비노출).
- [x] 4.2 `replication_manager.recover`: GET /replication/chunks/{file_ref}로 청크
      목록 조회 → 청크별 온라인·도달 가능한 홀더에서 fetch(스웜) → join → 복호화 →
      jbod.write_file 복원. 도달 불가 청크가 있으면 RecoveryError(누락 chunk_id 명시).
      서버 추가: ReplicationService.list_chunks + GET /replication/chunks/{file_ref}.
- [x] 4.3 전송 fallback: 직접 push/fetch 구현. 홀더 도달 불가 시 해당 홀더만 실패
      처리하고 다음 홀더로 진행(스웜으로 복구 가능). 교차 사용자 릴레이 fallback은
      `.kiro/specs/cross-user-replica-relay/`로 해소(복제본 op 한정 교차 사용자 릴레이
      허용 + 홀더가 payload 토큰으로 요청자=소유자 도출). 홀펀칭은 Phase 5.
- [x] 4.4 CLI: `backup`/`restore` 명령(온라인+동기화). 종료 코드 0/3/4, pending 경고.
- [x] 4.5 단위 테스트: replicate→recover 라운드트립(암호화 일치, Property 3),
      홀더<3 → pending, 홀더 실패 건너뛰기, 누락/오프라인 복구 RecoveryError,
      홀더 1곳만 도달 가능 시 복구 성공(스웜), file_ref 경로 비노출. 서버 list_chunks
      소유자 격리 테스트.

## Phase 5: UDP 홀펀칭 (선택, 직접 연결 확대)
- [x] 5.1 서버 랑데부(UDP): app/rendezvous.py — register로 reflexive UDP 주소 학습,
      connect로 같은 사용자 두 디바이스에 서로 주소 + punch 신호 교환. 토큰은
      decode_token으로 검증(같은 user_id만 중개). main.py lifespan 옵트인
      (rendezvous_enabled 기본 False, 테스트 영향 없음). 테스트 5종.
- [x] 5.2 `holepunch.py`: 순수 파이썬 UDP 동시 오픈(표준 socket/asyncio, 무-C).
      단일 소켓으로 register→request_peer→punch. 성공 시 직접 UDP 경로, 실패 시
      호출자가 릴레이 fallback. 테스트 5종(동시 오픈 성공/무응답 실패/랑데부 왕복).
- [x] 5.3 punch 실패(symmetric/CGNAT/이중 NAT)는 호출자가 릴레이로 귀결
      (test_punch_fails_without_peer → False 반환 = fallback 트리거). 릴레이는 기존
      보장된 fallback.
- [~] (이관) 데이터 전송의 HTTP→UDP 통합은 별도 범위 — 현 P2P 전송은 httpx HTTP(TCP)
      라 punch로 연 UDP 매핑을 재사용할 수 없다. 홀펀칭은 직접 도달성 판정/랑데부
      인프라로 제공하고, 실제 전송 전환(UDP 데이터 채널)은 미착수. daemon 자동
      랑데부 등록도 전송 전환과 함께 후속.

## Phase 6: 건강성 / 재복제 / 호혜 집행
- [x] 6.1 건강성 기반 재복제: ReplicationManager.ensure_replicas(virtual_path) +
      _heal_chunk. 청크별 list_replicas로 online 복제 수 집계, <min_replicas면 온라인
      홀더에서 청크를 받아(불변 청크 → 재암호화 없이 바이트 동일) 새 홀더로 복사 +
      record. 온라인 소스 없는 청크는 unrecoverable 보고. 모두 충족 시 replicated,
      아니면 pending. CLI `heal` 명령.
- [x] 6.2 재복제 동시성 상한: asyncio.Semaphore(max_concurrent_repair=4)로 청크 단위
      병렬 제한. 홀더 store 실패 시 다음 후보로 진행(백오프 대신 후보 순회).
- [x] 6.3 호혜 쿼터 집행: placement가 avail=provided*0.5-hosted ≥ size 인 온라인
      device만 후보로(미제공 device는 avail 0 → 제외). 정책값 RECIPROCITY_FRACTION=0.5.
      테스트: 미제공 device 미배치 + (Phase 2) 용량·호혜·exclude.
- [x] (해소) 교차 사용자 릴레이 fallback: `.kiro/specs/cross-user-replica-relay/`에서
      구현. 릴레이 허브는 복제본 op(replica_*)에 한해 교차 사용자 중계를 허용하고,
      홀더는 payload의 소유자 토큰을 검증(same_user=False)해 ParityStore 인가에 사용한다.
      파일 데이터 op는 여전히 같은 user_id만 릴레이.

## Phase 7: 검증 / 문서
- [x] 7.1 E2E(tests/test_replication_e2e.py): 실제 P2PServer 홀더 + ParityStore +
      중앙 mock. replicate → 소스 훼손 → recover 바이트 일치. 홀더 2곳 중지(스웜)에서
      1곳으로 복구 성공. 호스트는 암호문만 보관(평문 비가독).
- [x] 7.2 가용성: 홀더 부족(<3) → pending(E2E test_pending_when_insufficient_holders).
      홀더 상실 후 heal로 자동 회복(test_heal_restores_replication_after_holder_loss).
- [x] 7.3 양쪽 pytest 회귀 그린(클라이언트 469/1 skip, 서버 88).
- [x] 7.4 ROADMAP MVP3를 "엔진 구현 완료"로 갱신, ARCHITECTURE에 리플리케이션
      컴포넌트·흐름 추가, HANDOVER에 현황·이관 항목·스펙 위치 반영.

## 비범위 (후속)
- Erasure coding(Reed-Solomon)로 저장 효율 개선.
- DHT 분산 레지스트리(현재 중앙 서버).
- 유료 등급별 복제 수/대역폭/지역 분산(MVP4 플랫폼).
- 전체 BitTorrent/libtorrent 스택.
