# Implementation Plan: StardustFS Core

## Overview

기존 FUSE 기반 구현을 완전히 폐기하고, wsgidav 기반 WebDAV 암호화 가상 파일시스템을 구현한다. 6개 핵심 컴포넌트(Config Loader, Encryption Engine, Metadata Store, Storage Source, JBOD Manager, WebDAV Server)를 순차적으로 구현하며, 각 단계에서 테스트를 통해 정확성을 검증한다.

## Tasks

- [ ] 1. 프로젝트 구조 설정 및 데이터 모델 정의
  - [ ] 1.1 프로젝트 디렉토리 구조 생성 및 의존성 설정
    - `stardustlib/` 하위에 모듈 파일 생성: `config_loader.py`, `encryption_engine.py`, `metadata_store.py`, `storage_source.py`, `jbod_manager.py`, `webdav_provider.py`, `exceptions.py`, `models.py`
    - `requirements.txt` 업데이트: wsgidav, cheroot, cryptography, pysqlcipher3, hypothesis, pytest, requests
    - 기존 FUSE 관련 코드 제거 (stardustfs.py의 FUSE 의존성)
    - _Requirements: 1.1_

  - [ ] 1.2 데이터 모델 및 예외 클래스 정의
    - `stardustlib/models.py`에 `StardustConfig`, `WebDAVConfig`, `DirectorySourceConfig`, `LoopbackSourceConfig`, `FileMetadata`, `EntryInfo`, `FileInfo`, `EncryptedFileHeader` dataclass 정의
    - `stardustlib/exceptions.py`에 `InsufficientStorageError`, `DecryptionError`, `IntegrityError`, `KeyNotFoundError`, `InvalidKeyError` 정의
    - _Requirements: 5.2, 4.5_

- [ ] 2. Config Loader 구현
  - [ ] 2.1 ConfigLoader 클래스 구현
    - JSON 설정 파일 파싱 및 `StardustConfig` 반환
    - 설정 검증: 최소 1개 소스, 절대 경로, Loopback 크기 범위(10MB~2TB), 포트 범위(1~65535)
    - `webdav.host`를 항상 `"127.0.0.1"`로 강제 (보안)
    - `load_encryption_key()`: 키 파일 우선, 없으면 환경변수(`STARDUST_KEY`)에서 로드, 32바이트 검증
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9_

  - [ ]* 2.2 ConfigLoader 단위 테스트 작성
    - 유효한 설정 파싱 테스트
    - 필수 필드 누락 시 에러 테스트
    - 잘못된 경로/크기 범위 검증 테스트
    - 키 파일/환경변수 로드 테스트
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9_

  - [ ]* 2.3 Property test: 설정 검증 거부
    - **Property 12: 설정 검증 거부**
    - **Validates: Requirements 6.3, 6.4, 6.5, 6.6, 6.7**

- [ ] 3. Encryption Engine 구현
  - [ ] 3.1 EncryptionEngine 클래스 구현
    - AES-256-GCM 암호화/복호화 구현 (`cryptography` 라이브러리 사용)
    - 파일 헤더 구조: `[MAGIC(4B)][VERSION(1B)][MODE_ID(1B)][IV(16B)][TAG(16B)][CIPHERTEXT...]`
    - CSPRNG으로 128비트 IV 생성 (`os.urandom(16)`)
    - 키 길이 검증 (정확히 32바이트)
    - 잘못된 키/변조 데이터 시 적절한 예외 발생 (`DecryptionError`, `IntegrityError`)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [ ]* 3.2 Property test: 암호화 Round-Trip 보장
    - **Property 1: 암호화 Round-Trip 보장**
    - **Validates: Requirements 4.6**

  - [ ]* 3.3 Property test: IV 고유성
    - **Property 2: IV 고유성**
    - **Validates: Requirements 4.4**

  - [ ]* 3.4 Property test: 잘못된 키 복호화 거부
    - **Property 3: 잘못된 키 복호화 거부**
    - **Validates: Requirements 4.7**

  - [ ]* 3.5 Property test: 데이터 변조 감지
    - **Property 4: 데이터 변조 감지**
    - **Validates: Requirements 4.8**

- [ ] 4. Checkpoint - 암호화 엔진 검증
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Metadata Store 구현
  - [ ] 5.1 MetadataStore 클래스 구현
    - SQLCipher(pysqlcipher3) 기반 암호화 SQLite DB 초기화 및 스키마 생성
    - `PRAGMA key`로 암호화 키 설정
    - `files` 테이블 및 `directories` 테이블 생성 (인덱스 포함)
    - CRUD 메서드: `insert`, `update`, `delete`, `lookup`
    - 디렉토리 작업: `list_entries`, `rename_path`, `rename_directory`
    - 트랜잭션 관리: `begin_transaction`, `commit`, `rollback`
    - 연결 타임아웃 10초 설정
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9_

  - [ ]* 5.2 Property test: 메타데이터 조회 일관성
    - **Property 7: 메타데이터 조회 일관성**
    - **Validates: Requirements 5.3**

  - [ ]* 5.3 Property test: Virtual_Path 유일성
    - **Property 11: Virtual_Path 유일성**
    - **Validates: Requirements 5.2**

  - [ ]* 5.4 MetadataStore 단위 테스트 작성
    - CRUD 작업 테스트
    - 디렉토리 이동 시 하위 파일 경로 일괄 갱신 테스트
    - 존재하지 않는 경로 조회 시 None 반환 테스트
    - DB 미존재 시 스키마 자동 생성 테스트
    - _Requirements: 5.1, 5.5, 5.6, 5.7, 5.8, 5.9_

- [ ] 6. Storage Source 구현
  - [ ] 6.1 StorageSource 추상 클래스 및 DirectorySource 구현
    - `StorageSource` ABC 정의: `initialize`, `read`, `write`, `delete`, `exists`, `mkdir`, `rmdir`, `list_dir`, `get_available_space`, `get_total_space`
    - `DirectorySource` 구현: 로컬 디렉토리 기반 읽기/쓰기/삭제
    - 경로 존재/권한 검증, 접근 불가 시 비활성 상태 전환
    - 용량 정보 제공 (`shutil.disk_usage`)
    - _Requirements: 3.1, 3.5, 3.6_

  - [ ] 6.2 LoopbackSource 구현
    - 고정 크기 파일 생성 (10MB~2TB)
    - 내부 파일시스템 구조 초기화 (디렉토리 기반 논리적 구조)
    - 논리적 마운트/언마운트
    - 기존 파일 존재 시 덮어쓰지 않고 활성화
    - 초기화 실패 시 부분 파일 삭제 및 비활성 처리
    - _Requirements: 3.2, 3.3, 3.4, 3.7, 3.8_

  - [ ]* 6.3 StorageSource 단위 테스트 작성
    - DirectorySource: 읽기/쓰기/삭제/용량 조회 테스트
    - LoopbackSource: 파일 생성/초기화/활성화 테스트
    - 접근 불가 시 비활성 전환 테스트
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

- [ ] 7. JBOD Manager 구현
  - [ ] 7.1 JBODManager 핵심 파일 작업 구현
    - `read_file`: 메타데이터 조회 → 소스에서 읽기 → 복호화 → 반환
    - `write_file`: 암호화 → 소스 선택/기존 소스 → 저장 → 메타데이터 기록 (원자적 트랜잭션)
    - `delete_file`: 메타데이터 조회 → 소스에서 삭제 → 메타데이터 제거
    - `move_file`, `copy_file`: 동일 소스 내 이동/복사 + 메타데이터 갱신
    - `select_source`: 여유 공간 최대 소스 선택 (Most-Available-Space)
    - 쓰기 실패 시 부분 파일 삭제 + 메타데이터 롤백
    - _Requirements: 2.1, 2.3, 2.4, 2.6, 2.7, 2.8, 8.1, 8.2, 8.4, 8.5_

  - [ ] 7.2 JBODManager 디렉토리 작업 구현
    - `list_directory`: 메타데이터 기반 엔트리 목록 조회 (파일 + 디렉토리 통합, 중복 제거)
    - `create_directory`: 모든 활성 소스에 디렉토리 복제, 일부 실패 시 로그 기록
    - `delete_directory`: 디렉토리 및 하위 파일 삭제
    - `move_directory`: 디렉토리 이동 + 하위 파일 경로 일괄 갱신
    - _Requirements: 2.2, 2.5, 2.9_

  - [ ]* 7.3 Property test: 소스 선택 최적성
    - **Property 5: 소스 선택 최적성**
    - **Validates: Requirements 2.3**

  - [ ]* 7.4 Property test: 공간 부족 시 거부
    - **Property 6: 공간 부족 시 거부**
    - **Validates: Requirements 2.6**

  - [ ]* 7.5 Property test: 쓰기 실패 시 원자적 롤백
    - **Property 10: 쓰기 실패 시 원자적 롤백**
    - **Validates: Requirements 8.2**

  - [ ]* 7.6 Property test: 디렉토리 목록 완전성 및 중복 제거
    - **Property 8: 디렉토리 목록 완전성 및 중복 제거**
    - **Validates: Requirements 2.2**

  - [ ]* 7.7 Property test: 디렉토리 복제 일관성
    - **Property 9: 디렉토리 복제 일관성**
    - **Validates: Requirements 2.5**

  - [ ]* 7.8 Property test: 비활성 소스 파일 접근 거부
    - **Property 13: 비활성 소스 파일 접근 거부**
    - **Validates: Requirements 8.5**

- [ ] 8. Checkpoint - JBOD 및 스토리지 검증
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. WebDAV Server 구현
  - [ ] 9.1 StardustDAVProvider 및 리소스 클래스 구현
    - `StardustDAVProvider(DAVProvider)`: `get_resource_inst()` 구현
    - `StardustFileResource(DAVNonCollection)`: `get_content`, `begin_write`, `delete`, `copy_move_single`, 속성 메서드
    - `StardustDirectoryResource(DAVCollection)`: `get_member_names`, `get_member`, `create_empty_resource`, `create_collection`, `delete`
    - 에러를 HTTP 상태 코드로 변환: 404, 401, 507, 500
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 8.6_

  - [ ] 9.2 WebDAV 앱 생성 및 인증 설정
    - `create_webdav_app()`: wsgidav WSGI 앱 생성
    - HTTP Basic Authentication 설정 (설정 파일의 username/password 사용)
    - 인증 실패 시 401 반환 및 로그 기록
    - _Requirements: 1.1, 1.7, 1.8, 1.9_

  - [ ]* 9.3 WebDAV 통합 테스트 작성
    - GET/PUT/DELETE/MKCOL/MOVE/COPY/PROPFIND 메서드 테스트
    - 인증 성공/실패 테스트
    - 존재하지 않는 경로 접근 시 404 테스트
    - 공간 부족 시 507 테스트
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 8.6_

- [ ] 10. 시스템 초기화 및 메인 엔트리포인트 구현
  - [ ] 10.1 시스템 초기화 로직 구현
    - 순차적 검증: 설정 로드 → 스토리지 검증 → DB 초기화 → 키 검증 → WebDAV 시작
    - HKDF로 마스터 키에서 DB 전용 키 파생
    - 모든 검증 실패 원인 수집 후 일괄 로그 기록
    - 검증 실패 시 0이 아닌 종료 코드로 중단
    - 성공 시 "StardustFS 준비 완료" 로그 기록
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ] 10.2 메인 엔트리포인트 작성
    - `stardustfs.py` 또는 `__main__.py`에서 `--config` 인자 처리
    - `initialize_system()` 호출 후 cheroot 서버 시작
    - _Requirements: 1.1, 7.5_

  - [ ]* 10.3 시스템 초기화 통합 테스트 작성
    - 유효한 설정으로 정상 시작 테스트
    - 잘못된 설정으로 시작 실패 테스트
    - DB 타임아웃 시 실패 처리 테스트
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

- [ ] 11. Final checkpoint - 전체 시스템 검증
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Python 3.13 환경, hypothesis 라이브러리를 property-based testing에 사용
- pysqlcipher3를 사용하여 메타데이터 DB를 AES-256으로 암호화
- 기존 FUSE 코드(stardustfs.py, stardustlib/)는 새 구현으로 대체

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "3.3", "3.4", "3.5"] },
    { "id": 3, "tasks": ["5.1", "6.1", "6.2"] },
    { "id": 4, "tasks": ["5.2", "5.3", "5.4", "6.3"] },
    { "id": 5, "tasks": ["7.1", "7.2"] },
    { "id": 6, "tasks": ["7.3", "7.4", "7.5", "7.6", "7.7", "7.8"] },
    { "id": 7, "tasks": ["9.1", "9.2"] },
    { "id": 8, "tasks": ["9.3", "10.1"] },
    { "id": 9, "tasks": ["10.2", "10.3"] }
  ]
}
```
