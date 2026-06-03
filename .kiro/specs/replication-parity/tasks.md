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
- [~] 4.3 전송 fallback: 직접 push/fetch 구현. 홀더 도달 불가 시 해당 홀더만 실패
      처리하고 다음 홀더로 진행(스웜으로 복구 가능). 교차 사용자 릴레이 fallback은
      릴레이 허브가 같은 user_id 간만 허용하므로 서버 확장 필요 → Phase 6로 이관.
      홀펀칭은 Phase 5.
- [x] 4.4 CLI: `backup`/`restore` 명령(온라인+동기화). 종료 코드 0/3/4, pending 경고.
- [x] 4.5 단위 테스트: replicate→recover 라운드트립(암호화 일치, Property 3),
      홀더<3 → pending, 홀더 실패 건너뛰기, 누락/오프라인 복구 RecoveryError,
      홀더 1곳만 도달 가능 시 복구 성공(스웜), file_ref 경로 비노출. 서버 list_chunks
      소유자 격리 테스트.

## Phase 5: UDP 홀펀칭 (선택, 직접 연결 확대)
- [ ] 5.1 서버 랑데부 엔드포인트(양쪽 reflexive 주소 교환 + 동시 오픈 신호).
- [ ] 5.2 `holepunch.py`: 순수 파이썬 UDP 동시 오픈(C 의존 없이). 성공 시 직접 경로,
      실패 시 릴레이 fallback.
- [ ] 5.3 symmetric/CGNAT은 릴레이로 귀결됨을 테스트로 확인(보장된 fallback).

## Phase 6: 건강성 / 재복제 / 호혜 집행
- [ ] 6.1 홀더 heartbeat로 online 복제 수 집계, <3 지속(기본 24h) 시 재복제 큐잉.
- [ ] 6.2 재복제 동시성 상한 + 백오프.
- [ ] 6.3 호혜 쿼터 미충족 device의 신규 배치 제한(정책값 설정).

## Phase 7: 검증 / 문서
- [ ] 7.1 E2E(로컬 서버 + 다중 device): replicate → 소스 중지 → 다른 홀더 recover →
      바이트 일치. 홀더 1곳만 도달 가능한 경우 복구 성공(스웜).
- [ ] 7.2 가용성: 홀더 부족 → pending + 경고 → 확보 후 자동 완료.
- [ ] 7.3 양쪽 pytest 회귀 그린.
- [ ] 7.4 ROADMAP의 MVP3 항목을 본 스펙으로 갱신, HANDOVER/ARCHITECTURE 반영.

## 비범위 (후속)
- Erasure coding(Reed-Solomon)로 저장 효율 개선.
- DHT 분산 레지스트리(현재 중앙 서버).
- 유료 등급별 복제 수/대역폭/지역 분산(MVP4 플랫폼).
- 전체 BitTorrent/libtorrent 스택.
