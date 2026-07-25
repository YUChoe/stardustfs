# StardustFS 로드맵

여러 디바이스의 스토리지를 하나의 가상 파일서버로 묶는다. 파일은 각 디바이스에
AES-256-GCM으로 암호화 저장되고, 메타데이터는 중앙 서버로 동기화되며, 다운로드는
파일을 가진 디바이스에서 직접(또는 릴레이) 가져온다.

불변 원칙
- zero-knowledge: 서버는 암호문 blob + version 정수만 본다. 콘텐츠는 소유 디바이스에서 전송.
- 같은 유저 디바이스 간에만 평문 P2P/릴레이. 교차 계정은 암호화 복제 청크만.
- 실패는 규격 에러로 반환한다("graceful 건너뛰기" 금지).

## 진행 현황

| 단계 | 내용 | 상태 |
|---|---|---|
| MVP1 | 로컬 암호화 저장 (암호화 엔진, 스토리지 통합) | 완료 |
| MVP2 | 멀티디바이스 동기화 (tombstone 삭제, CAS, 원격 소스, P2P, 릴레이, version 롱폴링) | 완료 |
| MVP10 | CLI 가상 파일서버 — WebDAV 제거, `stardust put/get/ls` 업/다운로드 | 완료 |
| 인증 | 토큰 기반 전환 (credentials.json, login/logout) | 완료 |
| MVP3 | 암호화 리플리케이션 — 청킹, ≥3 홀더 복제, 호혜 회계, 자동 backup/heal | 완료 |
| 전송 | NAT 캐스케이드 — 직접 TCP → 홀펀칭 UDP → 서버 릴레이 (UPnP 폐지) | 완료 |
| 스토리지 | 루프백을 FAT 이미지(pyfatfs)로 전환, 스필오버/티어링/디태치 | 완료 |
| GUI | 데스크톱 파일탐색기 (CLI/코어 API 재사용) | 완료 (mvp12) |
| 파셜 동기화 | 레코드 단위 증분 메타데이터 전송 (256B 패딩, since/CAS) | **진행 중 (mvp13)** |
| MVP4 | 서비스 플랫폼 — 등급/과금/중계 정책 | 보류 |

## 현재 단계: 파셜/증분 메타데이터 동기화 (mvp13)

서버가 메타데이터를 레코드 단위 암호문(`metadata_records`)으로 저장하고, 클라이언트는
변경된 레코드만 `since` 필터로 받고 base_version CAS로 올린다. record_id는 경로의
HMAC(불투명), 레코드 평문은 256B 배수로 패딩해 암호문 크기로 경로 길이를 추정하지
못하게 한다. 구버전 서버(레코드 미지원)에는 전체 blob 경로로 자동 폴백한다.
스펙: `.kiro/specs/partial-metadata-sync/`.

## 다음 후보

- 교차 사용자 릴레이 fallback: 릴레이 허브 인가 모델 재설계(보안 민감, 별도 스펙).
- 기존 blob 전용 user의 레코드 모드 마이그레이션(현재는 신규 user만 레코드 모드).
- MVP4 서비스 플랫폼: 등급별 백업 사본 수·호혜 호스팅 비율·팀 풀 격리
  (placement·호혜 회계에 정책값을 얹는 형태). erasure coding(저장 효율).

## 컴포넌트 지도 (stardustlib/)

- 저장/메타: `storage_source`, `metadata_store`, `storage_pool`, `chunker`
- 동기화: `sync_client` (주기 폴링 + version 롱폴링 + CAS)
- 원격/전송: `remote_source`, `p2p_server`, `relay_client`/`relay_worker`,
  `rudp`/`p2p_udp`/`holepunch_service`
- 복제: `replication_manager`/`replication_scheduler`/`replication_hosting`,
  `parity_store`
- 디바이스/데몬: `device_manager`, `daemon`/`daemon_control`, `credential_store`
- 접근 계층: `cli/`, `gui/`

## 참고 문서

- 작업 인수인계 상세: [HANDOVER.md](./HANDOVER.md)
- 전송 캐스케이드 상세: [TRANSPORT.md](./TRANSPORT.md)
- 파일 저장 위치 정책: [DISTRIBUTION_POLICY.md](./DISTRIBUTION_POLICY.md)
- 스펙: `.kiro/specs/<기능>/{requirements,design,tasks}.md`
