"""스토리지 풀 관리자.

복수의 Storage Source를 단일 논리 볼륨으로 통합하고,
파일 배치 전략을 관리한다.
"""

import logging
import re
import time
import uuid

from stardustlib import chunker
from stardustlib.encryption_engine import EncryptionEngine
from stardustlib.exceptions import InsufficientStorageError
from stardustlib.metadata_store import MetadataStore
from stardustlib.models import ChunkRef, EntryInfo, FileInfo
from stardustlib.storage_source import StorageSource

logger = logging.getLogger(__name__)

# 물리 파일명 형식: <32자리 hex UUID>_<원본 파일명>. orphan GC는 이 형식의
# 파일만 대상으로 하여 metadata DB 등 비관리 파일을 건드리지 않는다.
_MANAGED_FILE_RE = re.compile(r"^[0-9a-f]{32}_")

# 청크 네이티브 저장의 평문 청크 크기. 전송 계층 청크(4 MiB)와 맞춘다.
CHUNK_SIZE = chunker.DEFAULT_CHUNK_SIZE


class StoragePool:
    """스토리지 풀 관리자.

    모든 활성 Storage Source의 파일을 단일 네임스페이스로 통합하고,
    파일 배치 전략(Most-Available-Space)을 적용한다.
    """

    def __init__(
        self,
        sources: list[StorageSource],
        metadata_store: MetadataStore,
        encryption_engine: EncryptionEngine | None = None,
        device_id: str | None = None,
    ) -> None:
        """StoragePool 초기화.

        Args:
            sources: Storage Source 목록.
            metadata_store: 메타데이터 저장소.
            encryption_engine: 암호화 엔진 (None이면 암호화 비활성).
            device_id: 이 클라이언트의 디바이스 ID (파일 변경 추적용, 선택).
        """
        self.sources = sources
        self.metadata_store = metadata_store
        self.encryption_engine = encryption_engine
        self.device_id = device_id
        # 개선 1: source_id → StorageSource dict (O(1) 조회)
        self._source_map: dict[str, StorageSource] = {
            s.source_id: s for s in sources
        }
        # device_id → 원격 디바이스 프록시 (RemoteSource). 크로스 디바이스 읽기 라우팅용.
        self._remote_devices: dict = {}
        # orphan GC 디바운스: 소유권 이전/병합 감지 시 set, 사이클당 1회 스캔
        self._gc_needed: bool = False
        # 축출(evicted) 파일 재구체화 콜백: recover_fn(virtual_path) → 복제 홀더에서
        # 복구해 로컬에 재기록(쓰기로 evicted 해제). 온라인 세션/데몬에서 주입.
        self._recover_fn = None

    def mark_gc_needed(self) -> None:
        """orphan GC가 필요함을 표시한다 (다음 사이클에 1회 스캔).

        소유권 이전 또는 동기화 병합에서 소유권 변경을 감지했을 때 호출한다.
        """
        self._gc_needed = True

    def register_remote_device(self, device_id: str, remote) -> None:
        """원격 디바이스 프록시(RemoteSource)를 device_id로 등록한다.

        read_file이 원격 소유 파일을 이 프록시로 라우팅한다.
        """
        self._remote_devices[device_id] = remote

    # --- 소스 관리 ---

    def select_source(
        self, file_size: int, exclude_ids: tuple[str, ...] = ()
    ) -> StorageSource:
        """파일 크기 이상의 여유 공간을 가진 소스 중 가용 공간이 가장 많은 소스 선택.

        Args:
            file_size: 저장할 파일 크기 (바이트).
            exclude_ids: 제외할 source_id 목록(evacuate 시 원본 소스 제외).

        Returns:
            선택된 StorageSource.

        Raises:
            InsufficientStorageError: 조건을 만족하는 소스가 없을 때.
        """
        best_source: StorageSource | None = None
        best_space: int = -1

        for source in self.sources:
            if not source.is_active or source.is_remote:
                continue
            if source.source_id in exclude_ids:
                continue
            available = source.get_available_space()
            if available >= file_size and available > best_space:
                best_source = source
                best_space = available

        if best_source is None:
            raise InsufficientStorageError(
                f"모든 활성 소스의 여유 공간이 {file_size} 바이트 미만"
            )
        return best_source

    def _get_source_by_id(self, source_id: str) -> StorageSource | None:
        """source_id로 소스를 찾는다. O(1)."""
        return self._source_map.get(source_id)

    def add_source(self, source: StorageSource) -> None:
        """소스를 동적으로 추가한다 (예: 인증 후 RemoteSource 마운트).

        동일 source_id가 이미 있으면 교체한다.
        """
        # 기존 동일 id 소스 제거 후 추가 (중복 방지)
        self.sources = [s for s in self.sources if s.source_id != source.source_id]
        self.sources.append(source)
        self._source_map[source.source_id] = source

    def replace_local_sources(self, new_local_sources: list[StorageSource]) -> None:
        """로컬 소스를 새 목록으로 in-place 교체한다(config 리로드용).

        원격 소스(is_remote=True)와 _remote_devices, _recover_fn은 보존하고 로컬
        소스만 교체한다. 같은 StoragePool 객체를 갱신하므로 p2p_server/sync_client 등
        기존 참조가 즉시 새 소스를 사용한다(객체 교체 아님).
        """
        remote = [s for s in self.sources if getattr(s, "is_remote", False)]
        old_local = [s for s in self.sources if not getattr(s, "is_remote", False)]
        self.sources = list(new_local_sources) + remote
        self._source_map = {s.source_id: s for s in self.sources}
        # 옛 로컬 소스(FAT 이미지 핸들 등)를 깨끗이 닫는다.
        for s in old_local:
            try:
                s.close()
            except Exception:  # noqa: BLE001 — 종료 경로
                pass

    def close_local_sources(self) -> None:
        """로컬 소스 핸들을 정리한다(종료/세션 close 시 FAT 이미지 클린 언마운트)."""
        for s in self.sources:
            if not getattr(s, "is_remote", False):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass

    def _generate_physical_path(self, virtual_path: str) -> str:
        """가상 경로에서 물리 경로를 생성한다.

        UUID 기반으로 충돌 없는 물리 경로를 생성한다.
        """
        # 가상 경로의 디렉토리 구조를 유지하면서 파일명에 UUID 추가
        parts = virtual_path.strip("/").split("/")
        if len(parts) > 1:
            dir_part = "/".join(parts[:-1])
            file_part = f"{uuid.uuid4().hex}_{parts[-1]}"
            return f"{dir_part}/{file_part}"
        return f"{uuid.uuid4().hex}_{parts[0]}"

    def evacuate_source(self, source_id: str) -> dict:
        """소스의 활성 파일을 남은 로컬 소스로 이동한다(detach 전 evacuate, 로컬).

        각 파일: 대상 로컬 소스 선택(원본 제외) → at-rest 암호문을 raw로 복사 →
        메타 source_id/physical_path 갱신 → 기록 성공 후 원본 블록 삭제(무손실).
        용량 부족(로컬 대상 없음)은 unmoved(추후 리모트 evacuate, Phase 3).

        반환: {"ok": bool(미이동 0), "moved": [vpath...], "unmoved": [vpath...]}.
        """
        src = self._get_source_by_id(source_id)
        if src is None:
            return {"ok": False, "moved": [], "unmoved": [], "error": "no source"}
        moved: list[str] = []
        unmoved: list[str] = []
        for meta in self._files_to_evacuate(source_id):
            # 로컬 소유(또는 레거시 NULL)만 이동. 원격 소유는 그 디바이스가 보관.
            if meta.device_id is not None and meta.device_id != self.device_id:
                continue
            # 청크 표현은 청크 단위로 옮긴다(레거시 blob과 이동 단위가 다르다).
            if self.metadata_store.get_chunks(meta.virtual_path):
                if self._evacuate_chunks(meta, source_id):
                    moved.append(meta.virtual_path)
                else:
                    unmoved.append(meta.virtual_path)
                continue
            try:
                target = self.select_source(
                    meta.file_size, exclude_ids=(source_id,)
                )
            except InsufficientStorageError:
                target = None
            try:
                if target is not None:
                    # 로컬 이동
                    blob = src.read(meta.physical_path)
                    new_phys = self._generate_physical_path(meta.virtual_path)
                    target.write(new_phys, blob)  # 대상 기록 먼저
                    self.metadata_store.update(
                        meta.virtual_path, file_size=meta.file_size,
                        modified_at=meta.modified_at, device_id=self.device_id,
                        source_id=target.source_id, physical_path=new_phys,
                    )
                    try:
                        src.delete(meta.physical_path)  # 성공 후 원본 삭제
                    except OSError:
                        pass
                    moved.append(meta.virtual_path)
                elif self._evacuate_to_remote(meta, src):
                    moved.append(meta.virtual_path)
                else:
                    unmoved.append(meta.virtual_path)
            except Exception as e:  # noqa: BLE001 — 파일 단위 격리(원본 보존)
                logger.error("evacuate 실패: %s: %s", meta.virtual_path, e)
                unmoved.append(meta.virtual_path)
        return {"ok": not unmoved, "moved": moved, "unmoved": unmoved}

    def _files_to_evacuate(self, source_id: str) -> list:
        """소스를 비우기 위해 옮겨야 할 활성 파일 목록을 모은다.

        files.source_id로 잡히는 파일(레거시 blob·첫 청크 기준)과, 첫 청크는 다른
        소스에 있지만 이 소스에 청크를 남긴 파일을 합친다. 후자를 빠뜨리면 detach 시
        청크가 소스에 남아 사라진다.
        """
        metas = list(self.metadata_store.list_files_in_source(source_id))
        seen = {m.virtual_path for m in metas}
        for vpath in self.metadata_store.list_chunked_paths_in_source(source_id):
            if vpath in seen:
                continue
            meta = self.metadata_store.lookup(vpath)
            if meta is not None:
                metas.append(meta)
                seen.add(vpath)
        return metas

    def _evacuate_chunks(self, meta, source_id: str) -> bool:
        """청크 표현 파일의 청크를 남은 로컬 소스로 옮긴다(무손실).

        청크마다 대상 소스를 고르고, 기록에 성공한 뒤에만 원본 청크를 지우며
        매니페스트를 갱신한다. 하나라도 옮길 수 없으면 이미 옮긴 청크는 그대로 두고
        False를 반환한다(데이터는 온전하며 매니페스트가 실제 위치를 가리킨다).
        """
        src = self._get_source_by_id(source_id)
        if src is None:
            return False
        for chunk in self.metadata_store.get_chunks(meta.virtual_path):
            if chunk.source_id != source_id:
                continue  # 이미 다른 소스에 있다
            try:
                target = self.select_source(
                    chunk.size, exclude_ids=(source_id,)
                )
            except InsufficientStorageError:
                target = None
            try:
                blob = src.read(chunk.chunk_ref)
                if target is not None:
                    target.write(chunk.chunk_ref, blob)  # 대상 기록 먼저
                    new_source_id = target.source_id
                    new_device_id = self.device_id
                else:
                    # 남은 로컬 소스가 없다 → 온라인 원격 기기로 청크를 옮긴다.
                    placed = self._push_chunk_to_remote(chunk.chunk_ref, blob)
                    if placed is None:
                        logger.warning(
                            "청크 evacuate 대상 없음(로컬·원격): %s chunk_index=%d",
                            meta.virtual_path, chunk.index,
                        )
                        return False
                    _remote, new_source_id, new_device_id = placed
                self.metadata_store.update_chunk_location(
                    meta.virtual_path, chunk.index,
                    new_source_id, new_device_id,
                )
                try:
                    src.delete(chunk.chunk_ref)  # 성공 후 원본 삭제
                except OSError:
                    pass
            except Exception as e:  # noqa: BLE001 — 원본 보존
                logger.error(
                    "청크 evacuate 실패: %s chunk_index=%d: %s",
                    meta.virtual_path, chunk.index, e,
                )
                return False
        # files의 레거시 위치 컬럼도 첫 청크 기준으로 맞춘다.
        chunks = self.metadata_store.get_chunks(meta.virtual_path)
        if chunks:
            self.metadata_store.update(
                meta.virtual_path, file_size=meta.file_size,
                modified_at=meta.modified_at,
                device_id=self.device_id,   # 파일 레코드 소유자는 그대로
                source_id=chunks[0].source_id,
                physical_path=chunks[0].chunk_ref,
            )
        return True

    @staticmethod
    def _ensure_remote_active(remote) -> bool:
        """리모트가 활성이면 True. 비활성이면 refresh(재라우팅)로 재활성화를 시도한다.

        RemoteSource는 시작 시 1회 마운트되므로, 그때 오프라인이던 디바이스가 이후
        온라인이 돼도 비활성으로 남는다. 전송 직전 refresh로 재라우팅해 살린다.
        """
        if getattr(remote, "is_active", False):
            return True
        refresh = getattr(remote, "refresh", None)
        if refresh is None:
            return False
        try:
            return bool(refresh(force=True))
        except Exception:  # noqa: BLE001 — 재라우팅 실패 시 비활성 취급
            return False

    def _evacuate_to_remote(self, meta, src) -> bool:
        """로컬 용량 부족 파일을 온라인 리모트 디바이스로 옮긴다(같은 사용자).

        암호문 블록을 원격에 push → 메타데이터를 원격 소유로 갱신 → 원본 블록 삭제.
        도달 가능한 리모트가 없으면 False(원본 보존, 미이동).
        """
        if not self._remote_devices:
            return False
        blob = src.read(meta.physical_path)
        for device_id, remote in self._remote_devices.items():
            if not self._ensure_remote_active(remote):
                continue
            try:
                new_phys = self._generate_physical_path(meta.virtual_path)
                remote_src_id = remote.push_blob(new_phys, blob)
                if not remote_src_id:
                    continue
                self.metadata_store.update(
                    meta.virtual_path, file_size=meta.file_size,
                    modified_at=meta.modified_at, device_id=device_id,
                    source_id=remote_src_id, physical_path=new_phys,
                )
                try:
                    src.delete(meta.physical_path)  # 원격 기록 성공 후 원본 삭제
                except OSError:
                    pass
                logger.info("evacuate→리모트: %s → device=%s",
                            meta.virtual_path, device_id)
                return True
            except Exception as e:  # noqa: BLE001 — 다음 리모트 시도
                logger.warning("리모트 evacuate 실패(%s): %s", device_id, e)
                continue
        return False

    def _write_to_remote(
        self, virtual_path: str, encrypted: bytes, file_size: int
    ) -> bool:
        """로컬 만석 시 신규 파일을 온라인 리모트 디바이스(같은 계정)에 기록한다.

        암호문을 원격에 push → 메타데이터를 그 디바이스 소유로 insert. 도달 가능한
        온라인 리모트가 없거나 모두 실패하면 False(미기록). _evacuate_to_remote의
        신규 insert판(원본 삭제 단계 없음).
        """
        if not self._remote_devices:
            return False
        for device_id, remote in self._remote_devices.items():
            if not self._ensure_remote_active(remote):
                continue
            try:
                new_phys = self._generate_physical_path(virtual_path)
                remote_src_id = remote.push_blob(new_phys, encrypted)
                if not remote_src_id:
                    continue
                now = time.time()
                self.metadata_store.insert(
                    virtual_path=virtual_path,
                    source_id=remote_src_id,
                    physical_path=new_phys,
                    file_size=file_size,
                    created_at=now,
                    modified_at=now,
                    device_id=device_id,
                )
                logger.info("스필오버→리모트: %s → device=%s",
                            virtual_path, device_id)
                return True
            except Exception as e:  # noqa: BLE001 — 다음 리모트 시도
                logger.warning("리모트 스필오버 실패(%s): %s", device_id, e)
                continue
        return False

    def _materialize_evicted(self, metadata):
        """축출된 파일을 복제 홀더에서 복구해 로컬에 재구체화하고 갱신된 메타를 반환한다.

        _recover_fn(virtual_path)이 복구→write_file(로컬 재기록, evicted 해제)을 수행한다.
        콜백이 없으면(오프라인) 읽을 수 없으므로 OSError. 재구체화 후 lookup이
        evicted=0인 메타를 돌려줘야 한다.
        """
        if self._recover_fn is None:
            raise OSError(
                f"축출된 파일은 온라인 복구가 필요합니다(복구 콜백 없음): "
                f"{metadata.virtual_path}"
            )
        self._recover_fn(metadata.virtual_path)
        fresh = self.metadata_store.lookup(metadata.virtual_path)
        if fresh is None or getattr(fresh, "evicted", False):
            raise OSError(
                f"축출 파일 복구 실패: {metadata.virtual_path}"
            )
        return fresh

    def evict_cold(self, is_safe, bytes_to_free: int) -> dict:
        """복제본이 충분한 콜드 파일의 로컬 원본을 비워 공간을 회수한다.

        replicated·미축출·로컬 소유 파일을 오래된 순으로 순회하며, is_safe(virtual_path)이
        True(현재 온라인 복제본 수 ≥ min_replicas 실측)인 것만 로컬 블록을 삭제하고
        mark_evicted한다. 누적 회수량이 bytes_to_free 이상이면 멈춘다. 대상을 확보한
        뒤에만 삭제하므로 무손실이다(복제본이 없으면 건너뜀).

        반환: {"evicted": [virtual_path...], "freed": int}.
        """
        evicted: list[str] = []
        freed = 0
        for meta in self.metadata_store.list_eviction_candidates():
            if freed >= bytes_to_free:
                break
            # 로컬 소유 + 로컬 소스에 실제 존재하는 것만 대상
            owner = meta.device_id
            if owner is not None and owner != self.device_id:
                continue
            source = self._get_source_by_id(meta.source_id)
            if source is None or getattr(source, "is_remote", False):
                continue
            if not is_safe(meta.virtual_path):
                continue  # 온라인 복제본 미달 → 보존(스테일 플래그 삭제 금지)
            try:
                # 청크 표현이면 청크 전부, 레거시면 단일 블록을 비운다.
                self._delete_local_blocks(meta)
                self.metadata_store.mark_evicted(meta.virtual_path)
                evicted.append(meta.virtual_path)
                freed += meta.file_size
                logger.info("콜드 축출: %s (%d bytes)",
                            meta.virtual_path, meta.file_size)
            except OSError as e:
                logger.warning("축출 실패(%s): %s", meta.virtual_path, e)
                continue
        return {"evicted": evicted, "freed": freed}

    # --- 파일 작업 ---

    def _resolve_metadata(self, virtual_path: str):
        """파일 메타데이터를 조회하고 축출된 파일은 재구체화한 뒤 반환한다.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
        """
        metadata = self.metadata_store.lookup(virtual_path)
        if metadata is None:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {virtual_path}")
        # 축출된(복제본 전용) 파일: 복제 홀더에서 복구해 로컬에 재구체화한 뒤 읽는다.
        if getattr(metadata, "evicted", False):
            metadata = self._materialize_evicted(metadata)
        return metadata

    def _is_local_owner(self, metadata) -> bool:
        """로컬 소유(또는 레거시 NULL) 파일인지 판정한다."""
        owner = metadata.device_id
        return owner is None or owner == self.device_id

    def read_file(self, virtual_path: str) -> bytes:
        """파일을 읽어 복호화된 데이터를 반환한다.

        청크 표현(매니페스트 존재)이면 청크를 순서대로 가져와 각각 복호화한 뒤
        이어붙인다. 레거시 통짜 blob은 기존 단일 경로로 읽는다.

        Args:
            virtual_path: 가상 파일 경로.

        Returns:
            복호화된 파일 데이터.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
            OSError: 소스가 비활성 상태이거나 청크가 누락됐을 때.
        """
        metadata = self._resolve_metadata(virtual_path)
        # 매니페스트가 있으면 청크별로 라우팅한다(청크가 여러 기기에 흩어져 있어도
        # 각각 제 보관처에서 가져온다). 파일 단위 device_id는 참고값일 뿐이다.
        chunks = self.metadata_store.get_chunks(virtual_path)
        if chunks:
            return b"".join(
                self._decrypt_chunk(virtual_path, c) for c in chunks
            )
        encrypted_data = self._read_resolved_ciphertext(metadata)
        if self.encryption_engine is not None:
            return self.encryption_engine.decrypt(encrypted_data)
        return encrypted_data

    def read_range(
        self, virtual_path: str, offset: int, length: int
    ) -> bytes:
        """파일의 [offset, offset+length) 범위만 읽어 반환한다(부분 읽기).

        청크 표현이면 그 범위를 덮는 청크만 가져와 복호화한다. 레거시 통짜 blob은
        전체를 읽은 뒤 잘라낸다(단일 blob은 부분 복호화가 불가능하다).

        요청 범위가 파일 끝을 넘으면 존재하는 부분까지만 반환한다.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
            ValueError: offset/length가 음수일 때.
            OSError: 소스가 비활성이거나 청크가 누락됐을 때.
        """
        if offset < 0 or length < 0:
            raise ValueError("offset/length는 0 이상이어야 합니다")
        if length == 0:
            return b""

        self._resolve_metadata(virtual_path)
        chunks = self.metadata_store.get_chunks(virtual_path)
        if not chunks:
            return self.read_file(virtual_path)[offset:offset + length]

        by_index = {c.index: c for c in chunks}
        wanted = chunker.chunk_range(offset, length, CHUNK_SIZE)
        parts: list[bytes] = []
        for idx in wanted:
            chunk = by_index.get(idx)
            if chunk is None:
                break  # 파일 끝을 넘는 범위 — 존재하는 부분까지만 반환
            parts.append(self._decrypt_chunk(virtual_path, chunk))
        if not parts:
            return b""
        blob = b"".join(parts)
        start = offset - wanted[0] * CHUNK_SIZE
        return blob[start:start + length]

    def read_ciphertext(self, virtual_path: str) -> bytes:
        """복호화하지 않은 at-rest 암호문을 반환한다.

        복제(replicate)에서 사용한다. 저장된 암호문을 그대로 청크로 나눠 복제하므로
        백업 표현이 at-rest 표현과 동일해지고, 백업마다 복호화→재암호화하는 비용이
        사라진다. 청크 표현이면 청크 암호문을 인덱스 순으로 이어붙인 것이 at-rest
        표현이다.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
            OSError: 소스가 비활성이거나 원격 디바이스에 도달할 수 없을 때.
        """
        metadata = self._resolve_metadata(virtual_path)
        chunks = self.metadata_store.get_chunks(virtual_path)
        if chunks:
            return b"".join(
                self._read_chunk_bytes(virtual_path, c) for c in chunks
            )
        return self._read_resolved_ciphertext(metadata)

    def read_chunks(
        self, virtual_path: str, local_only: bool = False, on_progress=None
    ) -> list:
        """at-rest 청크 암호문을 (chunk_index, bytes) 목록으로 반환한다.

        복제(replicate)에서 재분할 없이 그대로 복제 청크로 쓰기 위한 경로다. 저장된
        청크 경계를 그대로 넘겨주므로 at-rest·복제 표현이 완전히 일치한다.
        청크 레코드가 없는 파일은 빈 목록을 반환한다.

        local_only=True면 이 device가 보관한 청크만 읽는다. 다른 device 보관 청크를
        읽으려면 원격 전송(릴레이)이 필요한데, 백업에서는 그 청크를 보관 기기가
        직접 올리므로 왕복이 무의미하다.

        on_progress(done, total)를 주면 청크를 읽을 때마다 호출한다(대용량 파일의
        읽기 구간이 로그 공백이 되지 않게).

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
            OSError: 청크가 누락됐거나 보관처에 도달할 수 없을 때.
        """
        self._resolve_metadata(virtual_path)
        chunks = self.metadata_store.get_chunks(virtual_path)
        if local_only:
            chunks = [
                c for c in chunks
                if c.device_id is None or c.device_id == self.device_id
            ]
        total = len(chunks)
        parts = []
        for done, chunk in enumerate(chunks, start=1):
            parts.append((chunk.index, self._read_chunk_bytes(virtual_path, chunk)))
            if on_progress is not None:
                on_progress(done, total)
        return parts

    def migrate_to_chunks(self, virtual_path: str) -> bool:
        """레거시 통짜 blob 파일을 청크 표현으로 전환한다(무손실).

        전환 순서가 무손실을 보장한다: 원본 blob을 읽어 평문을 확보하고, 청크를 모두
        기록·커밋한 뒤에야 원본 blob을 지운다. 중간에 실패하면 원본 blob과 메타데이터가
        그대로 남아 파일을 계속 읽을 수 있다(청크 쪽 잔여물은 orphan GC가 회수).

        Args:
            virtual_path: 전환할 파일의 가상 경로.

        Returns:
            전환했으면 True. 이미 청크 표현이면 False(할 일 없음).

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
            OSError: 원격 소유 파일이거나 소스가 비활성일 때.
            InsufficientStorageError: 청크를 놓을 공간이 없을 때(원본은 보존).
        """
        metadata = self._resolve_metadata(virtual_path)
        if self.metadata_store.get_chunks(virtual_path):
            return False  # 이미 청크 표현
        if not self._is_local_owner(metadata):
            raise OSError(
                f"원격 소유 파일은 그 기기가 전환합니다: {virtual_path}"
            )

        # 평문을 먼저 확보한다(원본 blob은 아직 그대로 둔다).
        data = self.read_file(virtual_path)
        self._store_chunks(virtual_path, self._encrypt_to_chunks(data), len(data))
        # 원본 blob 삭제는 _store_chunks가 커밋 후에 수행한다
        # (_discard_previous_representation). 실패해도 고아 블록은 orphan GC가 회수한다.
        logger.info("blob→청크 전환 완료: %s", virtual_path)
        return True

    def _encrypt_to_chunks(self, data: bytes) -> list:
        """평문을 CHUNK_SIZE로 나눠 청크별로 암호화한다.

        빈 파일도 청크 1개(빈 평문)로 표현해 읽기 경로를 단일화한다.
        """
        cipher_chunks = [
            self._encrypt_chunk(part)
            for _idx, part in chunker.split(data, CHUNK_SIZE)
        ]
        return cipher_chunks or [self._encrypt_chunk(b"")]

    def write_chunks(
        self, virtual_path: str, cipher_chunks: list, plain_size: int
    ) -> None:
        """at-rest 청크 암호문들을 재암호화 없이 그대로 저장한다.

        복제본 복구(recover)에서 사용한다. 홀더가 보관한 청크 바이트가 곧 at-rest
        표현이므로 그대로 기록하면 등록된 청크 해시가 복구 후에도 유효하다.

        Args:
            virtual_path: 가상 파일 경로.
            cipher_chunks: 인덱스 순으로 정렬된 청크 암호문 목록.
            plain_size: 메타데이터에 기록할 평문 크기.
        """
        self._store_chunks(virtual_path, cipher_chunks, plain_size)

    def _read_resolved_ciphertext(self, metadata) -> bytes:
        """이미 해석된 메타데이터로 레거시 단일 blob 암호문을 읽는다."""
        if self._is_local_owner(metadata):
            return self._read_local_ciphertext(metadata)
        # 원격 소유 → 디바이스 프록시로 라우팅
        return self._read_remote_ciphertext(metadata)

    # --- 청크 저장/조회 ---

    def _cipher_overhead(self) -> int:
        """청크 하나당 암호문 오버헤드(헤더+IV+태그). 암호화 비활성이면 0."""
        if self.encryption_engine is None:
            return 0
        return EncryptionEngine.HEADER_SIZE

    def _encrypt_chunk(self, plain: bytes) -> bytes:
        """청크 하나를 독립적으로 암호화한다(청크별 IV·인증 태그)."""
        if self.encryption_engine is None:
            return plain
        return self.encryption_engine.encrypt(plain)

    def _read_chunk_bytes(self, virtual_path: str, chunk: ChunkRef) -> bytes:
        """청크 암호문을 확보하고 해시를 검증한다(청크별 로컬/원격 라우팅).

        청크마다 보관 기기가 다를 수 있으므로 파일 단위 소유자가 아니라 청크의
        device_id로 라우팅한다.
        """
        if chunk.device_id is not None and chunk.device_id != self.device_id:
            data = self._read_remote_chunk(virtual_path, chunk)
        else:
            data = self._read_local_chunk(virtual_path, chunk)
        if chunk.hash and chunker.chunk_hash(data) != chunk.hash:
            raise OSError(
                f"청크 해시 불일치: file={virtual_path} "
                f"chunk_index={chunk.index} ref={chunk.chunk_ref}"
            )
        return data

    def _read_local_chunk(self, virtual_path: str, chunk: ChunkRef) -> bytes:
        """로컬 소스에서 청크 암호문을 읽는다."""
        source = self._get_source_by_id(chunk.source_id)
        if source is None or not source.is_active:
            raise OSError(
                f"청크가 위치한 소스가 비활성 상태입니다: {chunk.source_id} "
                f"(file={virtual_path} chunk_index={chunk.index})"
            )
        try:
            return source.read(chunk.chunk_ref)
        except FileNotFoundError as e:
            raise OSError(
                f"청크 누락: file={virtual_path} chunk_index={chunk.index} "
                f"ref={chunk.chunk_ref}"
            ) from e

    def _read_remote_chunk(self, virtual_path: str, chunk: ChunkRef) -> bytes:
        """청크를 보관한 원격 기기에서 암호문을 읽는다.

        같은 계정이면 master_key가 동일하므로 호출자가 로컬 엔진으로 복호화할 수 있다.
        비활성(오프라인) 프록시는 재라우팅을 1회 시도한다.
        """
        remote = self._remote_devices.get(chunk.device_id)
        if remote is None:
            raise OSError(
                f"청크 보관 기기 미마운트: device={chunk.device_id} "
                f"(file={virtual_path} chunk_index={chunk.index})"
            )
        if not self._ensure_remote_active(remote):
            raise OSError(
                f"청크 보관 기기 오프라인: device={chunk.device_id} "
                f"(file={virtual_path} chunk_index={chunk.index})"
            )
        try:
            # 청크 암호문은 전송 청크 한계 안에 들어오므로 단일 read로 받는다.
            return remote.read_from_source(chunk.chunk_ref, chunk.source_id)
        except FileNotFoundError as e:
            raise OSError(
                f"청크 누락(원격): file={virtual_path} "
                f"chunk_index={chunk.index} device={chunk.device_id}"
            ) from e

    def _decrypt_chunk(self, virtual_path: str, chunk: ChunkRef) -> bytes:
        """청크 암호문을 읽어 단독 복호화한다."""
        data = self._read_chunk_bytes(virtual_path, chunk)
        if self.encryption_engine is None:
            return data
        return self.encryption_engine.decrypt(data)

    def _store_chunks(
        self, virtual_path: str, cipher_chunks: list, plain_size: int
    ) -> None:
        """암호문 청크들을 소스에 기록하고 매니페스트를 커밋한다(원자적).

        모든 청크 기록이 성공한 뒤에만 메타데이터를 커밋하므로 파일이 반쯤 저장된
        상태로 남지 않는다. 중간 실패 시 이미 기록한 청크를 정리하고 규격 에러를
        올린다. 커밋 후에는 이전 표현(레거시 blob 또는 옛 청크)을 정리한다.

        Raises:
            InsufficientStorageError: 로컬·원격 어디에도 공간이 없을 때.
        """
        existing = self.metadata_store.lookup(virtual_path)
        # 원격 소유 파일 수정 → 로컬 소유권 이전(takeover). 원래 소유 디바이스의
        # 물리 블록은 그 디바이스가 동기화 후 정리하므로 여기서 지우지 않는다.
        takeover = existing is not None and not self._is_local_owner(existing)

        old_chunks = self.metadata_store.get_chunks(virtual_path)
        refs, written = self._place_chunks(cipher_chunks, existing, takeover)

        # 첫 청크 참조를 files의 물리 위치로 둔다(NOT NULL 충족). 청크 파일의
        # 정본은 file_chunks이며, 이 값은 레거시 컬럼 호환용이다.
        # files.device_id는 파일 레코드의 소유자(이 파일을 관리하는 기기)이고,
        # 청크의 보관 기기는 청크마다 따로 기록된다. 청크가 원격에 놓였다고 해서
        # 파일 소유권이 넘어가는 것은 아니다.
        head = refs[0]
        now = time.time()
        self.metadata_store.begin_transaction()
        try:
            if existing is None:
                self.metadata_store.insert(
                    virtual_path=virtual_path,
                    source_id=head.source_id,
                    physical_path=head.chunk_ref,
                    file_size=plain_size,
                    created_at=now,
                    modified_at=now,
                    device_id=self.device_id,
                )
            else:
                self.metadata_store.update(
                    virtual_path,
                    file_size=plain_size,
                    modified_at=now,
                    device_id=self.device_id,
                    source_id=head.source_id,
                    physical_path=head.chunk_ref,
                )
            self.metadata_store.put_chunks(virtual_path, refs)
            self.metadata_store.commit()
        except Exception:
            self.metadata_store.rollback()
            self._cleanup_chunks(written)
            raise

        if takeover:
            # 사이클당 1회 GC를 위해 플래그만 set (파일마다 스캔하지 않음)
            self._gc_needed = True
            logger.info(
                "소유권 이전 완료: %s → device=%s source=%s",
                virtual_path, self.device_id, head.source_id,
            )
        else:
            self._discard_previous_representation(existing, old_chunks, written)

    def _place_chunks(self, cipher_chunks: list, existing, takeover: bool):
        """청크를 로컬 우선으로 배치하고, 로컬이 부족하면 원격으로 스필오버한다.

        청크마다 보관처를 따로 정하므로 한 파일의 청크가 여러 소스·기기에 분산될 수
        있다. 하나라도 놓을 곳이 없으면 이미 기록한 청크를 정리하고 규격 에러를 낸다
        (반쯤 저장된 상태로 남기지 않는다).

        Returns:
            (ChunkRef 목록, 정리 대상 (target, ref) 목록).

        Raises:
            InsufficientStorageError: 로컬·원격 어디에도 놓을 수 없을 때.
        """
        total = sum(len(c) for c in cipher_chunks)
        try:
            preferred = self._select_chunk_source(existing, takeover, total)
        except InsufficientStorageError:
            preferred = None  # 전체를 한 소스에 담을 수는 없다 → 청크별로 배치

        refs: list[ChunkRef] = []
        written: list = []
        try:
            for index, cipher in enumerate(cipher_chunks):
                digest = chunker.chunk_hash(cipher)
                ref = self._chunk_path(preferred, digest, index)
                placed = self._place_one_chunk(
                    ref, cipher, preferred, index, len(cipher_chunks)
                )
                target, source_id, device_id = placed
                written.append((target, ref))
                refs.append(
                    ChunkRef(
                        index=index, chunk_ref=ref, source_id=source_id,
                        device_id=device_id, size=len(cipher), hash=digest,
                    )
                )
        except OSError as e:
            self._cleanup_chunks(written)
            if "insufficient space" in str(e).lower():
                raise InsufficientStorageError(str(e)) from e
            raise
        except Exception:
            self._cleanup_chunks(written)
            raise
        return refs, written

    @staticmethod
    def _chunk_path(source, digest: str, index: int) -> str:
        """청크의 소스 내 물리 경로 `<샤드…>/<chunk_ref>`를 만든다.

        샤딩 깊이는 소스 용량에서 정한다. 큰 볼륨은 한 단계 더 깊게 나눠 디렉토리당
        엔트리 수를 실측 임계치 아래로 유지하고, 작은 볼륨은 1단계로 둬 서브디렉토리
        클러스터 낭비를 피한다. 경로는 매니페스트에 그대로 저장되므로 깊이가 소스마다
        달라도(또는 나중에 바뀌어도) 읽기에는 영향이 없다.
        """
        depth = 1
        if source is not None:
            try:
                depth = chunker.shard_depth_for(
                    source.get_total_space(), CHUNK_SIZE
                )
            except Exception:  # noqa: BLE001 — 용량을 모르면 기본 깊이
                depth = 1
        return (
            f"{chunker.shard_prefix(digest, depth=depth)}/"
            f"{chunker.chunk_ref(index)}"
        )

    def _place_one_chunk(
        self, ref: str, cipher: bytes, preferred, index: int, count: int
    ):
        """청크 하나를 로컬(우선) 또는 원격에 기록하고 배치 정보를 반환한다.

        Returns:
            (기록한 대상 객체, source_id, device_id).

        Raises:
            InsufficientStorageError: 로컬·원격 어디에도 놓을 수 없을 때.
        """
        target = preferred
        if target is None or target.get_available_space() < len(cipher):
            try:
                target = self.select_source(len(cipher))
            except InsufficientStorageError:
                target = None
        if target is not None:
            target.write(ref, cipher)
            return target, target.source_id, self.device_id

        # 로컬에 놓을 수 없다 → 온라인 원격 기기(같은 계정)로 청크 스필오버
        placed = self._push_chunk_to_remote(ref, cipher)
        if placed is None:
            raise InsufficientStorageError(
                f"청크를 놓을 로컬·원격 공간이 없습니다 "
                f"(chunk_index={index}/{count}, size={len(cipher)})"
            )
        remote, source_id, device_id = placed
        logger.info(
            "청크 스필오버→리모트: chunk_index=%d device=%s source=%s",
            index, device_id, source_id,
        )
        return remote, source_id, device_id

    def _push_chunk_to_remote(self, ref: str, cipher: bytes):
        """청크 암호문을 도달 가능한 원격 기기에 기록한다.

        Returns:
            (RemoteSource, 원격 source_id, device_id) 또는 실패 시 None.
        """
        for device_id, remote in self._remote_devices.items():
            if not self._ensure_remote_active(remote):
                continue
            try:
                remote_src_id = remote.push_blob(ref, cipher)
                if not remote_src_id:
                    continue
                return remote, remote_src_id, device_id
            except Exception as e:  # noqa: BLE001 — 다음 원격 기기 시도
                logger.warning("청크 원격 배치 실패(%s): %s", device_id, e)
                continue
        return None

    def _select_chunk_source(
        self, existing, takeover: bool, total: int
    ) -> StorageSource:
        """청크를 기록할 로컬 소스를 고른다.

        기존 로컬 파일을 덮어쓰는 경우에는 지금 있는 소스를 우선한다(여유가 있으면).
        덮어쓰기가 파일을 다른 소스로 옮기지 않게 해 배치를 안정적으로 유지한다.
        여유가 없거나 신규·소유권 이전이면 일반 배치 규칙(여유 최대)을 따른다.

        Raises:
            InsufficientStorageError: 조건을 만족하는 로컬 소스가 없을 때.
        """
        if existing is not None and not takeover:
            current = self._get_source_by_id(existing.source_id)
            if (
                current is not None
                and current.is_active
                and not current.is_remote
                and current.get_available_space() >= total
            ):
                return current
        return self.select_source(total)

    @staticmethod
    def _cleanup_chunks(written: list) -> None:
        """기록에 실패한 청크 파일을 정리한다(베스트에포트).

        written은 (대상, ref) 목록이며 대상은 로컬 소스 또는 원격 프록시다.
        정리에 실패해도 데이터 손실은 없다(고아 블록은 orphan GC가 회수).
        """
        for target, ref in written:
            try:
                target.delete(ref)
            except Exception:  # noqa: BLE001 — 정리 실패는 orphan GC가 회수
                pass

    def _discard_previous_representation(
        self, existing, old_chunks: list, written: list
    ) -> None:
        """커밋 후 이전 표현(레거시 blob 또는 옛 청크)을 삭제한다.

        실패해도 데이터 손실은 없다(고아 블록은 orphan GC가 회수).
        """
        if existing is None:
            return
        keep = {ref for _target, ref in written}
        if old_chunks:
            for chunk in old_chunks:
                if chunk.chunk_ref in keep:
                    continue
                self._delete_chunk_block(chunk, best_effort=True)
            return
        # 이전엔 레거시 통짜 blob이었다.
        if existing.physical_path in keep:
            return
        src = self._get_source_by_id(existing.source_id)
        if src is None or not src.is_active:
            return
        try:
            src.delete(existing.physical_path)
        except Exception:  # noqa: BLE001
            pass

    def _read_local_ciphertext(self, metadata) -> bytes:
        """로컬 소스에서 at-rest 암호문을 읽는다(복호화하지 않음)."""
        source = self._get_source_by_id(metadata.source_id)
        if source is None or not source.is_active:
            raise OSError(
                f"파일이 위치한 소스가 비활성 상태입니다: {metadata.source_id}"
            )
        return source.read(metadata.physical_path)

    def _read_remote_ciphertext(self, metadata) -> bytes:
        """원격 디바이스의 P2P 서버에서 at-rest 암호문을 읽는다(복호화하지 않음).

        같은 계정이면 master_key가 동일하므로 호출자가 로컬 encryption_engine으로
        복호화할 수 있다.

        비활성(오프라인) 프록시는 routing 재조회(refresh)로 한 번 재네고시에이션을
        시도한다. 디바이스가 그사이 온라인이 되었으면 활성으로 전환되어 읽기가
        진행된다.
        """
        remote = self._remote_devices.get(metadata.device_id)
        if remote is None:
            raise OSError(
                f"원격 디바이스 미마운트: {metadata.device_id}"
            )

        # 비활성이면 재네고시에이션 1회 시도 (디바이스 재온라인 대응)
        if not remote.is_active:
            refresh = getattr(remote, "refresh", None)
            if callable(refresh):
                logger.info(
                    "원격 디바이스 비활성 — 재네고시에이션 시도: %s",
                    metadata.device_id,
                )
                refresh()
            if not remote.is_active:
                raise OSError(
                    f"원격 디바이스 오프라인: {metadata.device_id}"
                )

        return remote.read_from_source(
            metadata.physical_path, metadata.source_id, metadata.file_size
        )

    def write_file(self, virtual_path: str, data: bytes) -> None:
        """파일을 청크로 나눠 각각 암호화해 저장하고 메타데이터를 기록한다.

        원자적 트랜잭션을 보장한다: 모든 청크 기록이 성공한 뒤에만 메타데이터를
        커밋하고, 실패 시 기록한 청크를 정리한다.

        Args:
            virtual_path: 가상 파일 경로.
            data: 저장할 원본 데이터.

        Raises:
            InsufficientStorageError: 공간 부족 시.
        """
        # 레거시 통짜 blob 파일을 수정하면 청크 표현으로 전환된다. 원본 blob은 청크
        # 커밋이 성공한 뒤에 정리되므로 중간 실패에도 데이터를 잃지 않는다.
        self._store_chunks(
            virtual_path, self._encrypt_to_chunks(data), len(data)
        )

    def write_ciphertext(
        self, virtual_path: str, encrypted: bytes, plain_size: int
    ) -> None:
        """이미 암호화된 at-rest 표현을 재암호화 없이 그대로 저장한다.

        복제본 복구(recover)에서 사용한다. 홀더가 보관한 암호문이 곧 at-rest 표현이므로
        복호화→재암호화를 거치지 않고 원본 바이트를 그대로 복원한다(등록된 청크 해시가
        복구 후에도 유효하게 유지된다).

        at-rest 표현은 두 형태가 있다. 청크 표현은 청크 암호문을 이어붙인 것이고,
        레거시는 파일 전체를 한 번 암호화한 단일 blob이다. 청크 크기와 청크당 오버헤드가
        고정이라 총 길이로 두 형태를 구분할 수 있으므로, 받은 바이트를 원래 형태 그대로
        복원한다(바이트 동일성 유지).

        Args:
            virtual_path: 가상 파일 경로.
            encrypted: at-rest 암호문.
            plain_size: 메타데이터에 기록할 평문 크기.
        """
        sizes = self._expected_chunk_cipher_sizes(plain_size)
        if len(encrypted) == sum(sizes) and len(sizes) > 1:
            parts: list[bytes] = []
            pos = 0
            for size in sizes:
                parts.append(encrypted[pos:pos + size])
                pos += size
            self._store_chunks(virtual_path, parts, plain_size)
            return
        if len(encrypted) == plain_size + self._cipher_overhead():
            # 레거시 단일 blob(또는 청크 1개 — 두 표현이 동일하다).
            self._store_chunks(virtual_path, [encrypted], plain_size)
            return
        # 형태를 판정할 수 없는 암호문은 단일 블록으로 보존한다(무손실 우선).
        self._store_encrypted(virtual_path, encrypted, plain_size)

    def _expected_chunk_cipher_sizes(self, plain_size: int) -> list:
        """평문 크기로부터 청크 표현의 청크별 암호문 크기를 계산한다."""
        overhead = self._cipher_overhead()
        if plain_size <= 0:
            return [overhead]
        sizes = []
        remaining = plain_size
        while remaining > 0:
            take = min(CHUNK_SIZE, remaining)
            sizes.append(take + overhead)
            remaining -= take
        return sizes

    def _store_encrypted(
        self, virtual_path: str, encrypted: bytes, plain_size: int
    ) -> None:
        """암호문 블록을 소스에 저장하고 메타데이터를 기록한다(원자적).

        레거시 단일 blob 표현으로 저장한다. 대상이 청크 표현이었다면 청크 블록과
        매니페스트를 먼저 비워, 읽기 경로가 낡은 매니페스트를 집지 않게 한다.
        """
        existing = self.metadata_store.lookup(virtual_path)
        if existing is not None and self._is_local_owner(existing):
            if self.metadata_store.get_chunks(virtual_path):
                self._delete_local_blocks(existing)

        if existing is not None:
            # 원격 디바이스 소유 파일 수정 → 로컬 소유권 이전(takeover, 3a)
            owner = existing.device_id
            if owner is not None and owner != self.device_id:
                self._takeover_write(virtual_path, encrypted, plain_size)
                return
            # 기존 파일 덮어쓰기 (로컬 소유 또는 레거시 NULL)
            source = self._get_source_by_id(existing.source_id)
            if source is None or not source.is_active:
                raise OSError(
                    f"파일이 위치한 소스가 비활성 상태입니다: {existing.source_id}"
                )
            self.metadata_store.begin_transaction()
            try:
                source.write(existing.physical_path, encrypted)
                self.metadata_store.update(
                    virtual_path,
                    file_size=plain_size,
                    modified_at=time.time(),
                    device_id=self.device_id,
                )
                self.metadata_store.commit()
            except OSError as e:
                self.metadata_store.rollback()
                if "insufficient space" in str(e).lower():
                    raise InsufficientStorageError(str(e)) from e
                raise
            except Exception:
                self.metadata_store.rollback()
                raise
        else:
            # 새 파일 생성. 로컬 소스가 모두 만석이면 온라인 리모트(같은 계정)로
            # 스필오버한다(스토리지 풀을 로컬+리모트로 확장). 리모트도 불가하면 무손실 에러.
            try:
                source = self.select_source(len(encrypted))
            except InsufficientStorageError:
                if self._write_to_remote(virtual_path, encrypted, plain_size):
                    return
                raise
            physical_path = self._generate_physical_path(virtual_path)

            self.metadata_store.begin_transaction()
            try:
                source.write(physical_path, encrypted)
                now = time.time()
                self.metadata_store.insert(
                    virtual_path=virtual_path,
                    source_id=source.source_id,
                    physical_path=physical_path,
                    file_size=plain_size,
                    created_at=now,
                    modified_at=now,
                    device_id=self.device_id,
                )
                self.metadata_store.commit()
            except Exception:
                self.metadata_store.rollback()
                # 부분 기록된 파일 정리
                if source.exists(physical_path):
                    source.delete(physical_path)
                raise

    def _takeover_write(
        self, virtual_path: str, encrypted: bytes, plain_size: int
    ) -> None:
        """원격 소유 파일 수정 시 로컬 소유권 이전(3a).

        로컬 소스에 새 내용을 기록하고 metadata의 device_id/source_id/
        physical_path를 로컬로 갱신한다. 가상 경로는 유지된다. 원래 소유
        디바이스의 물리 파일은 orphan이 되며 그 디바이스가 동기화 후 정리한다.

        Raises:
            InsufficientStorageError: 로컬 소스 공간 부족 시.
        """
        source = self.select_source(len(encrypted))
        new_physical_path = self._generate_physical_path(virtual_path)

        self.metadata_store.begin_transaction()
        try:
            source.write(new_physical_path, encrypted)
            self.metadata_store.update(
                virtual_path,
                file_size=plain_size,
                modified_at=time.time(),
                device_id=self.device_id,
                source_id=source.source_id,
                physical_path=new_physical_path,
            )
            self.metadata_store.commit()
        except OSError as e:
            self.metadata_store.rollback()
            if source.exists(new_physical_path):
                source.delete(new_physical_path)
            if "insufficient space" in str(e).lower():
                raise InsufficientStorageError(str(e)) from e
            raise
        except Exception:
            self.metadata_store.rollback()
            if source.exists(new_physical_path):
                source.delete(new_physical_path)
            raise

        # 사이클당 1회 GC를 위해 플래그만 set (파일마다 스캔하지 않음)
        self._gc_needed = True
        logger.info(
            "소유권 이전 완료: %s → device=%s source=%s",
            virtual_path, self.device_id, source.source_id,
        )

    def gc_orphan_files_if_needed(self) -> int:
        """이전(takeover)/병합 감지로 플래그가 섰을 때만 orphan GC를 1회 수행한다.

        다중 파일 동시 수정 시에도 사이클당 전체 스캔은 1회뿐이다.
        """
        if not self._gc_needed:
            return 0
        self._gc_needed = False
        return self.gc_orphan_files()

    def gc_orphan_files(self) -> int:
        """로컬 소스의 고아 물리 파일을 삭제한다 (orphan GC).

        활성 metadata가 현재 디바이스 소유(또는 레거시 NULL)로 참조하는 물리
        파일은 보존하고, 그 외(소유권이 다른 디바이스로 이전되어 더 이상 참조되지
        않는) 물리 파일을 삭제한다.

        안전장치: device_id가 없으면(None) 보존 집합을 신뢰할 수 없으므로 전체를
        건너뛴다(전체 삭제 위험 방지). 원격 소스는 스캔하지 않는다.

        Returns:
            삭제한 물리 파일 수.
        """
        if self.device_id is None:
            logger.debug("device_id 없음, orphan GC 건너뜀")
            return 0

        live = self.metadata_store.live_physical_paths_for_device(self.device_id)
        # 청크 파일도 활성 참조다. 매니페스트가 가리키는 (source_id, chunk_ref)를
        # 보존 집합에 합치지 않으면 청크가 전부 고아로 오인되어 삭제된다.
        live = live | self.metadata_store.live_chunk_paths_for_device(
            self.device_id
        )
        removed = 0
        for source in self.sources:
            if not source.is_active or source.is_remote:
                continue
            try:
                names = source.list_physical_files()
            except Exception as e:
                logger.warning(
                    "orphan GC: 물리 파일 목록 조회 실패 (%s): %s",
                    source.source_id, e,
                )
                continue
            for name in names:
                # 우리가 만든 관리 파일(<hex32>_...)만 GC 대상. metadata DB,
                # 사용자 직접 파일 등 비관리 파일은 절대 건드리지 않는다.
                # 청크는 샤드 디렉토리 아래(`<hh>/<hex32>_cNNNN`)에 있으므로
                # 마지막 경로 요소로 판정한다.
                if not _MANAGED_FILE_RE.match(name.rsplit("/", 1)[-1]):
                    continue
                if (source.source_id, name) in live:
                    continue
                try:
                    source.delete(name)
                    removed += 1
                    logger.info(
                        "orphan 물리 파일 삭제: source=%s file=%s",
                        source.source_id, name,
                    )
                except Exception as e:
                    logger.warning(
                        "orphan 파일 삭제 실패 (%s/%s): %s",
                        source.source_id, name, e,
                    )
        if removed:
            logger.info("orphan GC 완료: %d개 물리 파일 삭제", removed)
        return removed

    def delete_file(self, virtual_path: str) -> None:
        """파일을 삭제하고 메타데이터를 제거한다.

        Args:
            virtual_path: 삭제할 파일의 가상 경로.

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때.
        """
        metadata = self.metadata_store.lookup(virtual_path)
        if metadata is None:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {virtual_path}")

        owner = metadata.device_id
        # 원격 디바이스 소유 파일은 물리 삭제하지 않고 로컬 metadata만 tombstone 처리한다.
        # (실제 물리 삭제는 소유 디바이스가 자신의 tombstone 동기화 시 수행)
        if owner is not None and owner != self.device_id:
            self.metadata_store.delete(virtual_path)
            return

        self._delete_local_blocks(metadata)
        self.metadata_store.delete(virtual_path)

    def _delete_chunk_block(self, chunk: ChunkRef, best_effort: bool = False) -> None:
        """청크 하나의 물리 블록을 삭제한다(로컬·원격 공통).

        원격 보관 청크는 그 기기의 프록시로 삭제 요청을 보낸다. 도달할 수 없으면
        블록이 고아로 남으며 그 기기의 orphan GC가 회수한다.
        """
        if chunk.device_id is not None and chunk.device_id != self.device_id:
            remote = self._remote_devices.get(chunk.device_id)
            if remote is None or not self._ensure_remote_active(remote):
                return
            try:
                remote.delete(chunk.chunk_ref)
            except Exception as e:  # noqa: BLE001 — 도달 불가 시 고아로 남김
                if not best_effort:
                    logger.warning(
                        "원격 청크 삭제 실패(device=%s ref=%s): %s",
                        chunk.device_id, chunk.chunk_ref, e,
                    )
            return

        src = self._get_source_by_id(chunk.source_id)
        if src is None or not src.is_active:
            return
        try:
            src.delete(chunk.chunk_ref)
        except FileNotFoundError:
            if not best_effort:
                logger.warning("청크가 이미 삭제됨: %s", chunk.chunk_ref)
        except OSError as e:
            if not best_effort:
                logger.warning("청크 삭제 실패(%s): %s", chunk.chunk_ref, e)

    def _delete_local_blocks(self, metadata) -> None:
        """파일의 물리 블록을 삭제한다(청크 표현이면 청크 전부, 원격 청크 포함).

        청크 표현이면 매니페스트도 함께 비운다(chunked=0으로 복귀).
        """
        chunks = self.metadata_store.get_chunks(metadata.virtual_path)
        if chunks:
            for chunk in chunks:
                self._delete_chunk_block(chunk)
            self.metadata_store.delete_chunks(metadata.virtual_path)
            return

        source = self._get_source_by_id(metadata.source_id)
        if source is not None and source.is_active:
            try:
                source.delete(metadata.physical_path)
            except FileNotFoundError:
                logger.warning(
                    "물리 파일이 이미 삭제됨: %s", metadata.physical_path
                )

    def move_file(self, src_path: str, dst_path: str) -> None:
        """파일을 이동(이름 변경)한다.

        동일 소스 내에서 메타데이터의 Virtual_Path만 갱신한다.

        Args:
            src_path: 원본 가상 경로.
            dst_path: 대상 가상 경로.

        Raises:
            FileNotFoundError: 원본 파일이 존재하지 않을 때.
        """
        metadata = self.metadata_store.lookup(src_path)
        if metadata is None:
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {src_path}")

        self.metadata_store.rename_path(src_path, dst_path)

    def copy_file(self, src_path: str, dst_path: str) -> None:
        """파일을 복사한다.

        원본 파일을 읽어 새 경로에 쓴다.

        Args:
            src_path: 원본 가상 경로.
            dst_path: 대상 가상 경로.

        Raises:
            FileNotFoundError: 원본 파일이 존재하지 않을 때.
        """
        data = self.read_file(src_path)
        self.write_file(dst_path, data)

    def file_exists(self, virtual_path: str) -> bool:
        """파일 존재 여부를 확인한다."""
        return self.metadata_store.lookup(virtual_path) is not None

    def get_file_info(self, virtual_path: str) -> FileInfo | None:
        """파일 상세 정보를 반환한다."""
        metadata = self.metadata_store.lookup(virtual_path)
        if metadata is None:
            return None
        return FileInfo(
            virtual_path=metadata.virtual_path,
            source_id=metadata.source_id,
            file_size=metadata.file_size,
            created_at=metadata.created_at,
            modified_at=metadata.modified_at,
            is_directory=False,
        )

    # --- 디렉토리 작업 ---

    def list_directory(self, virtual_path: str) -> list[EntryInfo]:
        """메타데이터 기반 디렉토리 목록을 조회한다.

        파일과 디렉토리를 통합하여 중복 제거된 엔트리 목록을 반환한다.

        Args:
            virtual_path: 조회할 디렉토리의 가상 경로.

        Returns:
            해당 디렉토리의 직접 하위 엔트리 목록.
        """
        entries = self.metadata_store.list_entries(virtual_path)
        return entries

    def create_directory(self, virtual_path: str) -> None:
        """모든 활성 소스에 디렉토리를 생성한다.

        일부 소스에서 생성이 실패하면 로그에 기록하고,
        성공한 소스에서는 디렉토리를 유지한다 (Req 2.9).

        Args:
            virtual_path: 생성할 디렉토리의 가상 경로.
        """
        physical_path = virtual_path.lstrip("/")

        for source in self.sources:
            if not source.is_active or source.is_remote:
                continue
            try:
                source.mkdir(physical_path)
            except Exception as e:
                logger.error(
                    "Failed to create directory in source %s: %s",
                    source.source_id,
                    e,
                )

        # 메타데이터에 디렉토리 기록
        now = time.time()
        # 경로 정규화: 슬래시로 끝나지 않도록 저장
        dir_path = virtual_path.rstrip("/")
        self.metadata_store.insert_directory(dir_path, now)

    def delete_directory(self, virtual_path: str) -> None:
        """디렉토리 및 하위 파일을 재귀적으로 삭제한다.

        Args:
            virtual_path: 삭제할 디렉토리의 가상 경로.
        """
        # 하위 엔트리 조회 및 재귀 삭제
        entries = self.metadata_store.list_entries(virtual_path)
        for entry in entries:
            child_path = virtual_path.rstrip("/") + "/" + entry.name
            if entry.is_directory:
                self.delete_directory(child_path)
            else:
                try:
                    self.delete_file(child_path)
                except FileNotFoundError:
                    logger.warning(
                        "삭제 대상 파일이 이미 없음: %s", child_path
                    )

        # 모든 활성 로컬 소스에서 물리 디렉토리 삭제 (원격은 읽기 전용)
        physical_path = virtual_path.lstrip("/")
        for source in self.sources:
            if not source.is_active or source.is_remote:
                continue
            try:
                source.rmdir(physical_path)
            except Exception:
                pass

        # 메타데이터에서 디렉토리 제거
        dir_path = virtual_path.rstrip("/")
        self.metadata_store.delete_directory_entry(dir_path)

    def move_directory(self, src_path: str, dst_path: str) -> None:
        """디렉토리를 이동하고 하위 파일 경로를 일괄 갱신한다.

        Args:
            src_path: 원본 디렉토리 가상 경로.
            dst_path: 대상 디렉토리 가상 경로.
        """
        # 접두사 정규화: 슬래시로 끝나도록
        old_prefix = src_path if src_path.endswith("/") else src_path + "/"
        new_prefix = dst_path if dst_path.endswith("/") else dst_path + "/"

        # 메타데이터 일괄 갱신 (files + directories 테이블)
        self.metadata_store.rename_directory(old_prefix, new_prefix)

        # 디렉토리 자체의 메타데이터도 갱신
        old_dir = src_path.rstrip("/")
        new_dir = dst_path.rstrip("/")
        self.metadata_store._conn.execute(
            "UPDATE directories SET virtual_path = ? WHERE virtual_path = ?",
            (new_dir, old_dir),
        )
        self.metadata_store._conn.commit()

        # 물리 디렉토리 이동 (각 활성 로컬 소스, 원격은 읽기 전용)
        src_physical = src_path.lstrip("/")
        dst_physical = dst_path.lstrip("/")
        for source in self.sources:
            if not source.is_active or source.is_remote:
                continue
            try:
                # 대상 디렉토리 생성 후 원본 삭제 방식
                source.mkdir(dst_physical)
                # 원본 물리 디렉토리는 파일이 이미 이동되었으므로 삭제 시도
                if source.exists(src_physical):
                    source.rmdir(src_physical)
            except Exception as e:
                logger.error(
                    "Failed to move directory in source %s: %s",
                    source.source_id,
                    e,
                )

    # --- 용량 정보 ---

    def get_total_space(self) -> int:
        """모든 활성 로컬 소스의 전체 공간 합계를 반환한다.

        원격 소스(is_remote)는 읽기 전용 프록시이므로 로컬 용량에서 제외한다.
        """
        total = 0
        for source in self.sources:
            if source.is_active and not source.is_remote:
                total += source.get_total_space()
        return total

    def get_available_space(self) -> int:
        """모든 활성 로컬 소스의 가용 공간 합계를 반환한다.

        원격 소스(is_remote)는 읽기 전용 프록시이므로 로컬 용량에서 제외한다.
        """
        available = 0
        for source in self.sources:
            if source.is_active and not source.is_remote:
                available += source.get_available_space()
        return available

    def deactivate_source(self, source_id: str) -> None:
        """소스를 비활성 상태로 전환한다.

        Raises:
            ValueError: 해당 source_id의 소스가 존재하지 않을 때.
        """
        source = self._get_source_by_id(source_id)
        if source is None:
            raise ValueError(f"소스를 찾을 수 없습니다: {source_id}")
        source._deactivate(f"Manually deactivated: {source_id}")
