# Implementation Plan: MVP2 클라이언트 멀티디바이스

## Overview

StardustFS 클라이언트에 중앙 서버 인증, 메타데이터 동기화, P2P 파일 전송, Key 백업/복원 기능을 추가한다. 기존 MVP1 코드와의 하위 호환성을 유지하면서 모듈별로 점진적으로 구현한다.

## Tasks

- [x] 1. 프로젝트 구조 및 기반 설정
  - [x] 1.1 MVP2 예외 클래스 추가
    - `stardustlib/exceptions.py`에 AuthenticationError, SyncError, DeviceRegistrationError, P2PConnectionError, ConfigMigrationError 추가
    - _Requirements: 1.3, 1.5, 3.3, 6.6, 6.8_
  - [x] 1.2 설정 파일 v2 타입 정의 및 ConfigLoader 확장
    - ServerConfig, SyncConfig, P2PConfig, RemoteSourceConfig, StardustConfigV2 TypedDict 정의
    - v2 설정 파싱 로직 구현: server, sync, p2p 섹션 검증
    - server.url (https:// 스킴), device_name (1-64자), sync.interval_seconds (10-3600), p2p.port (1024-65535), p2p.enabled (boolean), remote source device_id (UUID) 검증
    - 모든 에러를 문자열 리스트로 수집하여 반환
    - version 필드 검증 (1 또는 2만 허용)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12_
  - [x]* 1.3 Property 1: v2 설정 검증 일관성 PBT 작성
    - **Property 1: v2 설정 검증 일관성**
    - hypothesis로 임의의 v2 설정 딕셔너리를 생성하여 유효/무효 필드에 대한 검증 결과 일관성 확인
    - **Validates: Requirements 2.3, 2.4, 2.5, 2.7, 2.9, 2.11**
  - [ ]* 1.4 ConfigLoader v2 단위 테스트 작성
    - 유효한 v2 설정 파싱, 각 필드별 경계값 검증, 에러 수집 동작 테스트
    - _Requirements: 2.1-2.12_

- [x] 2. v1→v2 설정 마이그레이션
  - [x] 2.1 ConfigLoader에 v1→v2 마이그레이션 로직 구현
    - version 1 감지 시 자동 v2 변환
    - 기존 webdav, sources, metadata_db, key_file 필드 보존
    - server(url: null), sync(interval_seconds: 30, conflict_strategy: "copy"), p2p(port: 9090, enabled: false) 기본값 추가
    - 원본 파일을 "{원본}.v1.bak" 형식으로 백업 (기존 백업 존재 시 순번 부여)
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9_
  - [x]* 2.2 Property 2: v1→v2 마이그레이션 필드 보존 PBT 작성
    - **Property 2: v1→v2 마이그레이션 필드 보존**
    - hypothesis로 임의의 유효한 v1 설정을 생성하여 마이그레이션 후 필드 보존 확인
    - **Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5, 13.6**
  - [x]* 2.3 Property 8: 백업 파일명 고유성 PBT 작성
    - **Property 8: 백업 파일명 고유성**
    - hypothesis로 임의의 파일 경로와 기존 백업 파일 집합을 생성하여 고유 파일명 생성 확인
    - **Validates: Requirements 13.9**
  - [ ]* 2.4 마이그레이션 단위 테스트 작성
    - 정상 마이그레이션, 백업 실패 시 중단, 기존 백업 존재 시 순번 부여 테스트
    - _Requirements: 13.1-13.9_

- [x] 3. Checkpoint - 설정 관련 모듈 검증
  - 모든 테스트 통과 확인, 질문이 있으면 사용자에게 문의.

- [x] 4. MetadataStore v2 스키마 확장
  - [x] 4.1 MetadataStore 스키마 마이그레이션 구현
    - files 테이블에 version, device_id, sync_status 컬럼 ALTER TABLE 추가
    - schema_version 테이블 생성 및 버전 기록
    - 기존 레코드 초기값 설정 (version=1, sync_status="synced")
    - 마이그레이션 전 DB 파일 백업 ("{원본}.v1.bak")
    - 트랜잭션 롤백 처리, schema_version 존재 시 건너뛰기
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8_
  - [x] 4.2 MetadataStore에 version 증가 및 sync_status 관리 메서드 추가
    - 파일 변경 시 version 증가, device_id 설정
    - sync_status 변경 메서드 (synced, pending, conflict)
    - _Requirements: 4.5, 4.6_
  - [ ]* 4.3 MetadataStore v2 단위 테스트 작성
    - 스키마 마이그레이션, version 증가, sync_status 변경, 롤백 테스트
    - _Requirements: 14.1-14.8, 4.5, 4.6_

- [x] 5. AuthClient 구현
  - [x] 5.1 AuthClient 모듈 구현 (`stardustlib/auth_client.py`)
    - httpx 기반 비동기 HTTP 클라이언트
    - login(): POST /auth/login → access_token + refresh_token 저장
    - refresh_token(): POST /auth/refresh → 새 토큰 쌍 수신
    - get_valid_token(): 만료 1분 전 자동 갱신
    - 401 응답 시 AuthenticationError, 네트워크 오류/5xx 시 기존 토큰 유지
    - 10초 타임아웃 시 오프라인 모드 전환
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
  - [ ]* 5.2 AuthClient 단위 테스트 작성
    - pytest-httpx로 로그인 성공/실패, 토큰 갱신, 오프라인 전환 테스트
    - _Requirements: 1.1-1.7_

- [x] 6. DeviceManager 구현
  - [x] 6.1 DeviceManager 모듈 구현 (`stardustlib/device_manager.py`)
    - register(): POST /devices → device_id 수신 및 로컬 저장
    - device_name, OS 정보, P2P 접속 주소 전송
    - 등록 실패 시 10초 간격 최대 5회 재시도, 모두 실패 시 오프라인 모드
    - start_heartbeat(): 60초마다 PUT /devices/{id}/heartbeat
    - heartbeat 3회 연속 실패 시 간격 120초로 증가, 성공 시 60초 복원
    - 기존 device_id 캐시 시 재등록 생략
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_
  - [x] 6.2 UPnP NAT 트래버설 구현
    - miniupnpc로 외부 포트 매핑 시도 (10초 타임아웃)
    - 성공 시 외부 IP:port를 connection_address로 사용
    - 실패 시 로컬 IP:port 사용, WARNING 로그
    - 종료 시 매핑 해제 시도
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_
  - [ ]* 6.3 DeviceManager 단위 테스트 작성
    - 등록 성공/재시도/실패, heartbeat 간격 변경, UPnP 성공/실패 테스트
    - _Requirements: 3.1-3.8, 10.1-10.5_

- [x] 7. Checkpoint - 인증 및 디바이스 관리 검증
  - 모든 테스트 통과 확인, 질문이 있으면 사용자에게 문의.

- [x] 8. KeyBackupEngine 구현
  - [x] 8.1 KeyBackupEngine 모듈 구현 (`stardustlib/key_backup_engine.py`)
    - encrypt_for_backup(): PBKDF2-SHA256 (600000 iterations, 16B salt) → AES-256-GCM (12B IV) 암호화
    - blob 형식: salt(16B) + iv(12B) + tag(16B) + ciphertext
    - decrypt_from_backup(): blob 파싱 → 파생 키 재생성 → 복호화
    - 복호화 실패 시 IntegrityError 발생
    - _Requirements: 6.1, 6.2, 6.5, 6.6, 6.7_
  - [x]* 8.2 Property 5: Key 백업 라운드트립 PBT 작성
    - **Property 5: Key 백업 라운드트립**
    - hypothesis로 임의의 32바이트 master_key와 8자 이상 비밀번호로 encrypt→decrypt 라운드트립 검증
    - **Validates: Requirements 6.7**
  - [x]* 8.3 Property 6: 잘못된 비밀번호로 복호화 실패 PBT 작성
    - **Property 6: 잘못된 비밀번호로 복호화 실패**
    - hypothesis로 올바른 비밀번호와 다른 비밀번호로 복호화 시 IntegrityError 발생 확인
    - **Validates: Requirements 6.6**
  - [ ]* 8.4 KeyBackupEngine 단위 테스트 작성
    - 정상 암호화/복호화, 잘못된 비밀번호, blob 변조 테스트
    - _Requirements: 6.1, 6.2, 6.5, 6.6, 6.7_

- [x] 9. ConflictResolver 구현
  - [x] 9.1 ConflictResolver 모듈 구현 (`stardustlib/conflict_resolver.py`)
    - detect_conflict(): server_version > local_base_version이면 충돌 판정
    - resolve_conflict(): 로컬 파일을 conflict copy로 rename, 서버 버전을 원본 경로에 적용
    - generate_conflict_name(): "{이름} (conflict - {device} - {YYYY-MM-DD HH-MM-SS}).{ext}" 형식
    - 동일 파일명 존재 시 "(2)", "(3)" 순번 추가
    - conflict copy 생성 실패 시 sync_status "pending" 유지, 로그 기록
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.8, 5.9_
  - [x]* 9.2 Property 3: 메타데이터 병합 정확성 PBT 작성
    - **Property 3: 메타데이터 병합 정확성**
    - hypothesis로 임의의 server_version, local_version, local_base_version 조합에 대해 4가지 경우의 상호 배타성 및 완전성 검증
    - **Validates: Requirements 5.1, 5.5, 5.6, 5.7**
  - [x]* 9.3 Property 4: 충돌 파일명 형식 및 고유성 PBT 작성
    - **Property 4: 충돌 파일명 형식 및 고유성**
    - hypothesis로 임의의 virtual_path와 device_name에 대해 파일명 형식 준수 및 고유성 검증
    - **Validates: Requirements 5.2, 5.8**
  - [ ]* 9.4 ConflictResolver 단위 테스트 작성
    - 충돌 감지, conflict copy 생성, 순번 부여, FS 오류 처리 테스트
    - _Requirements: 5.1-5.9_

- [x] 10. SyncClient 구현
  - [x] 10.1 SyncClient 모듈 구현 (`stardustlib/sync_client.py`)
    - initial_sync(): GET /sync/metadata → 로컬 DB와 병합
    - 병합 로직: version 비교 기반 (높은 version 채택, 동일 version+다른 device_id → conflict)
    - start_periodic_sync(): interval_seconds마다 PUT /sync/metadata 업로드
    - upload_key() / download_key(): PUT/GET /sync/key (10초 타임아웃, 3회 재시도)
    - 오프라인 시 로컬 DB 사용, 연결 복구 후 pending 변경사항 시각순 업로드
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.7, 4.8, 5.5, 5.6, 5.7, 6.3, 6.4, 6.8, 6.9, 11.1, 11.3, 11.4, 11.5_
  - [ ]* 10.2 SyncClient 단위 테스트 작성
    - 초기 동기화, 주기적 업로드, 오프라인→온라인 복구, key 업로드/다운로드 테스트
    - _Requirements: 4.1-4.8, 6.3, 6.4, 6.8, 6.9, 11.1-11.5_

- [x] 11. Checkpoint - 동기화 및 충돌 해결 검증
  - 모든 테스트 통과 확인, 질문이 있으면 사용자에게 문의.

- [x] 12. RemoteSource 구현
  - [x] 12.1 RemoteSource 모듈 구현 (`stardustlib/remote_source.py`)
    - StorageSource ABC의 모든 메서드 구현 (read, write, delete, exists, mkdir, rmdir, list_dir, get_available_space, get_total_space)
    - initialize(): GET /routing/{device_id}로 접속 주소 조회, 성공 시 활성 상태
    - 각 메서드: 대상 P2P Server에 POST 요청, auth_token 포함
    - 10초 타임아웃 시 OSError, 4xx/5xx 시 OSError (상태 코드 포함)
    - 비활성 상태에서 메서드 호출 시 OSError
    - 토큰 만료 시 갱신 후 1회 재시도
    - _Requirements: 7.1-7.11, 9.4, 9.5, 9.6_
  - [ ]* 12.2 RemoteSource 단위 테스트 작성
    - 각 메서드 성공/실패/타임아웃, 비활성 상태, 토큰 갱신 재시도 테스트
    - _Requirements: 7.1-7.11, 9.4-9.6_

- [x] 13. P2PServer 구현
  - [x] 13.1 P2PServer 모듈 구현 (`stardustlib/p2p_server.py`)
    - aiohttp 기반 HTTP 서버, p2p.port에서 시작
    - POST /p2p/read, /p2p/write, /p2p/delete, /p2p/list 엔드포인트
    - 요청마다 auth_token JWT 검증 (중앙 서버 위임)
    - 401 (토큰 무효), 403 (user_id 불일치), 400 (path traversal), 404 (파일 미존재), 413 (100MB 초과)
    - /p2p/write: 상위 디렉토리 자동 생성, 기록 바이트 수 반환
    - 포트 사용 중 시 에러 로깅 후 P2P 없이 계속 실행
    - 중앙 서버 접근 불가 시 503 반환 (5초 이내)
    - _Requirements: 8.1-8.13, 9.1-9.3, 9.7_
  - [x]* 13.2 Property 7: Path Traversal 방지 PBT 작성
    - **Property 7: Path Traversal 방지**
    - hypothesis로 임의의 physical_path에 대해 ".." 포함 또는 루트 외부 참조 시 400 반환, 유효 경로는 통과 확인
    - **Validates: Requirements 8.11**
  - [ ]* 13.3 P2PServer 단위 테스트 작성
    - 각 엔드포인트 정상/인증실패/파일미존재/payload초과/path traversal 테스트
    - _Requirements: 8.1-8.13, 9.1-9.3, 9.7_

- [x] 14. Checkpoint - P2P 통신 검증
  - 모든 테스트 통과 확인, 질문이 있으면 사용자에게 문의.

- [x] 15. WebDAV 오프라인 Placeholder 구현
  - [x] 15.1 WebDAV Provider에 오프라인 placeholder 로직 추가
    - PROPFIND 응답에서 오프라인 Remote_Source 파일을 ".offline" 확장자로 표시
    - DAV:getcontentlength=0, DAV:getlastmodified=원본 수정 시각
    - stardust:availability="offline", stardust:original-name=원본 파일명
    - GET/PUT/DELETE 요청 시 503 Service Unavailable 반환
    - 온라인 복구 시 ".offline" 제거, 실제 속성 반환
    - _Requirements: 15.1-15.10_
  - [ ]* 15.2 WebDAV Placeholder 단위 테스트 작성
    - 오프라인 표시, 503 응답, 온라인 복구, 모든 소스 온라인 시 정상 표시 테스트
    - _Requirements: 15.1-15.10_

- [x] 16. 클라이언트 시작 흐름 통합
  - [x] 16.1 stardustfs.py에 MVP2 초기화 순서 구현
    - (1) 설정 로드 → (2) 로컬 스토리지 초기화 → (3) 인증 → (4) 디바이스 등록 → (5) 메타데이터 동기화 → (6) P2P 서버 시작 → (7) WebDAV 서버 시작
    - 인증 실패 시 오프라인 모드 (4-6 건너뛰기)
    - 메타데이터 동기화 실패 시 로컬 DB만 사용
    - server 섹션 없으면 오프라인 전용 모드
    - 설정/스토리지 초기화 실패 시 종료
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_
  - [x] 16.2 오프라인 모드 동작 통합
    - 오프라인 시 로컬 소스 정상 동작, remote 소스 오류 반환
    - 연결 복구 시 pending 변경사항 시각순 업로드 후 서버 metadata 병합
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
  - [ ]* 16.3 통합 테스트 작성
    - 정상 시작 흐름, 인증 실패 시 오프라인 전환, 동기화 실패 시 로컬 모드 테스트
    - _Requirements: 12.1-12.5, 11.1-11.5_

- [x] 17. Final Checkpoint - 전체 테스트 통과 확인
  - 모든 테스트 통과 확인, 질문이 있으면 사용자에게 문의.

- [ ] 18. Tombstone GC 및 장기 오프라인 디바이스 재조정
  - [ ] 18.1 서버에 tombstone 보관기간 정책값 추가
    - app/config.py Settings에 tombstone_retention_days 추가 (기본 30, env STARDUST_TOMBSTONE_RETENTION_DAYS)
    - GET /sync/metadata/status 응답에 tombstone_retention_days 필드 포함
    - 서버는 정책값 전달만 하고 GC를 직접 수행하지 않음 (암호화 blob이라 불가)
    - _Requirements: 16.1, 16.2, 16.4_
  - [ ]* 18.2 서버 status 응답 테스트
    - tombstone_retention_days 기본값/환경변수 오버라이드 반영 확인
    - _Requirements: 16.1, 16.2_
  - [ ] 18.3 클라이언트 tombstone GC 구현
    - SyncClient._gc_tombstones(retention_days): deleted=1 AND modified_at < now-retention 레코드 물리 삭제
    - MetadataStore에 tombstone GC용 삭제 메서드 추가 (활성 레코드 보존)
    - 병합·업로드 직전 호출 (status에서 받은 retention_days 사용)
    - _Requirements: 16.3, 16.4_
  - [ ] 18.4 last_sync_at 보존 및 stale 판정 구현
    - 동기화 성공 시 {metadata_db}.syncstate.json에 last_sync_at 기록
    - SyncClient._is_stale(retention_days): (now - last_sync_at) > retention
    - 상태 파일 부재 시 non-stale (새 디바이스 경로)
    - _Requirements: 16.5, 16.6, 16.9, 16.10_
  - [ ] 18.5 stale 재조정(re-baseline) 구현
    - reconcile_if_stale(): pending 없으면 전체 재수신, pending 있으면 conflict copy 격리 후 재수신
    - 격리한 변경은 신규로 업로드
    - stardustfs.py 시작 흐름에 stale 재조정 단계 통합 (initial_sync 전후)
    - _Requirements: 16.6, 16.7, 16.8_
  - [ ]* 18.6 Property 9 PBT + 단위 테스트
    - **Property 9: Tombstone GC 및 stale 재조정 안전성**
    - GC 대상 선별 정확성(활성 레코드 미삭제), stale 판정 경계, pending 보존 검증
    - _Requirements: 16.3, 16.6, 16.7, 16.8, 16.10_

- [ ] 19. Final Checkpoint - tombstone GC 포함 전체 테스트 통과 확인
  - 모든 테스트 통과 확인, 질문이 있으면 사용자에게 문의.

## Notes

- `*` 표시된 태스크는 선택적이며 빠른 MVP 구현을 위해 건너뛸 수 있음
- 각 태스크는 특정 requirements를 참조하여 추적 가능성 보장
- Checkpoint에서 점진적 검증 수행
- Property 테스트는 hypothesis 라이브러리로 100+ 반복 실행
- 단위 테스트는 pytest + pytest-asyncio + pytest-httpx 사용
- 기존 MVP1 코드와의 하위 호환성을 항상 유지할 것

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "4.1"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.1", "4.2"] },
    { "id": 3, "tasks": ["2.2", "2.3", "2.4", "4.3", "5.1", "8.1"] },
    { "id": 4, "tasks": ["5.2", "6.1", "8.2", "8.3", "8.4", "9.1"] },
    { "id": 5, "tasks": ["6.2", "6.3", "9.2", "9.3", "9.4"] },
    { "id": 6, "tasks": ["10.1"] },
    { "id": 7, "tasks": ["10.2", "12.1"] },
    { "id": 8, "tasks": ["12.2", "13.1"] },
    { "id": 9, "tasks": ["13.2", "13.3", "15.1"] },
    { "id": 10, "tasks": ["15.2", "16.1"] },
    { "id": 11, "tasks": ["16.2"] },
    { "id": 12, "tasks": ["16.3"] }
  ]
}
```
