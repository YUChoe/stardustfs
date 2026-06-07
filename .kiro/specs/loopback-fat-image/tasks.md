---
inclusion: manual
---

# 루프백 FAT 이미지 스토리지 — Tasks

## Phase 0: 의존성
- [x] 0.1 requirements.txt에 pyfatfs==1.1.0(+ fs==2.4.16, appdirs==1.4.4) 고정 추가.

## Phase 1: 포맷/마운트 + 기본 I/O
- [x] 1.1 LoopbackSource를 FAT 이미지 기반으로 재구현: initialize(사전할당 truncate+
      mkfs/마운트, fat_type=size로 FAT16/FAT32 선택), 단일 PyFatFS 핸들, _ipath(→/내부).
- [x] 1.2 read/write/exists/delete/mkdir/rmdir/list_dir/list_physical_files를 PyFatFS로.
      write는 _ensure_parent(makedirs). PyFATException→OSError, ResourceNotFound→FNF.
- [x] 1.3 get_available_space(size_bytes - 내부 파일 크기 합, 근사)/get_total_space.
      실제 한정은 쓰기 시 FAT 예외로 집행. close()로 클린 언마운트.
- [x] 1.4 단위 테스트(test_loopback_fat: 포맷/라운드트립/목록/삭제/공간/.d 없음).

## Phase 2: 청크 + 용량 + read_only
- [x] 2.1 write_chunk(offset=0 용량검사+생성, offset>0 r+b seek)/read_chunk(rb seek).
      용량 초과 OSError(insufficient) + 부분 파일 롤백.
- [x] 2.2 LoopbackSource read_only 인자 + _build_core/_build_local_sources 전달.
      오프라인 세션은 evacuate(쓰기) 때문에 현행 read-write 유지, 전송은 데몬 큐 직렬화
      (read_only는 opt-in 제공).
- [x] 2.3 테스트: 청크 라운드트립, 용량 초과, read_only 쓰기 거부.

## Phase 3: 회귀/문서
- [x] 3.1 회귀 592 passed/1 skip(storage/jbod/p2p_server/remote_chunked/detach 그린).
- [x] 3.2 ARCHITECTURE 문서 갱신(루프백=FAT 이미지, `.d` 폐지).

## 비범위
- 마이그레이션(기존 스토리지 전량 삭제 가정).
- VHD/VHDX OS 마운트(권한 필요). FAT 외 파일시스템.
- 이미지 동적 리사이즈(고정 크기 유지).
