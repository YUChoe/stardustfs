---
inclusion: manual
---

# 루프백 FAT 이미지 스토리지 — Requirements

## 배경
현재 `LoopbackSource`는 진짜 루프백이 아니다. `<path>.img`는 설정 크기의 sparse
마커(Windows/NTFS에선 sparse가 아니라 실제로 전체 크기를 점유 — 낭비)이고, 실제
데이터는 동반 디렉토리 `<path>.img.d/`에 `<uuid>_<name>` 평문 이름의 암호문 파일로
저장된다. 목표는 `mount -o loop`처럼 고정 크기 이미지 파일 안에 실제 파일시스템을
두고 파일을 이미지 내부에 저장하는 것이다(동반 디렉토리 제거, 크기로 용량 실제 한정).

구현 방향: 파일 내 FAT 이미지(pyfatfs, 순수 파이썬, 권한 불필요, 크로스플랫폼).

## Requirements

### Requirement 1: 이미지 내부 파일시스템
- WHEN LoopbackSource를 초기화하면 THE 소스 SHALL `<path>`를 고정 크기 FAT 이미지로
  보장한다(없으면 사전할당 후 mkfs, 있으면 FAT로 마운트). 동반 디렉토리(`.d`)는
  더 이상 사용하지 않는다.
- THE 모든 파일 I/O(read/write/exists/delete/mkdir/rmdir/list/chunk)는 이미지 내부
  파일시스템(PyFatFS)을 대상으로 수행한다.
- WHEN size_bytes ≤ 2GiB이면 FAT16, 초과면 FAT32로 포맷한다(최소 10MiB 유지).

### Requirement 2: 용량 실제 한정
- IF 이미지의 여유 공간보다 큰 쓰기를 시도하면 THE 소스 SHALL `OSError(insufficient
  space ...)`를 발생시킨다(현 인터페이스 계약 유지 — JBOD가 InsufficientStorageError로
  스필오버 분기). pyfatfs의 PyFATException(공간 부족)을 OSError로 변환한다.
- THE `get_available_space` SHALL 이미지 FAT의 실제 여유(free clusters × cluster
  size)를 반환하고, `get_total_space`는 이미지 size_bytes를 반환한다.

### Requirement 3: 동시 접근(작업 큐 + 조회 read-only)
GUI는 논리 구조를 메타데이터 파일로 조회하고, 업로드/다운로드는 데몬의 이미지별
작업 큐로 직렬화되므로 동시 쓰기 충돌이 발생하지 않는다(별도 OS 파일락 불요).
- THE 이미지 쓰기는 데몬(단일 라이터)에서만, 작업 큐로 직렬화한다.
- THE CLI/GUI 오프라인 세션(조회·용량 표시용)은 이미지를 read_only로 연다(파일 내용
  쓰기/읽기 전송은 데몬 위임 경로로만). read_only 핸들은 FAT를 손상시키지 않는다.

### Requirement 4: 기존 스토리지 폐기(마이그레이션 없음)
기존 스토리지는 모두 삭제한다고 가정한다(마이그레이션 미수행).
- WHEN `<path>`가 유효한 FAT 이미지가 아니면 THE 소스 SHALL 새 FAT 이미지로 포맷한다
  (기존 비-FAT `.img`/`.img.d`의 데이터는 보존하지 않는다 — 데이터 손실 허용).
- THE 신규/재포맷은 사전할당 후 mkfs로 수행하며, 기존 `.img.d` 디렉토리에 의존하지
  않는다. (배포 전 사용자가 dev-storage 등 기존 소스를 비우는 것을 전제.)

### Requirement 5: 하위호환 인터페이스
- THE LoopbackSource SHALL 기존 StorageSource 계약(read/write/write_chunk/read_chunk/
  delete/exists/mkdir/rmdir/list_dir/list_physical_files/get_*_space, is_active,
  source_id, is_remote=False)을 그대로 만족한다. 호출자(JBOD/p2p_server/sync)는 변경
  없이 동작한다.

## 비기능
- 의존성 `pyfatfs`(+ `fs`, `appdirs`) requirements.txt에 고정 추가.
- 권한 불필요, Windows/Linux 동작. 리눅스에선 산출 이미지를 `mount -o loop`로 검사 가능.
- FAT 오버헤드(예약 섹터·FAT 테이블·클러스터 반올림)로 usable < size_bytes(허용).
- at-rest 암호화 불변: 이미지 내부에 저장되는 것은 기존과 동일한 AES-GCM 암호문이며,
  FAT 메타(파일명 `<uuid>_<name>`)는 로컬에만 존재(서버 zero-knowledge와 무관).
