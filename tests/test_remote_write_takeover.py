"""원격 파일 수정 시 로컬 소유권 이전(3a) + orphan GC 단위 테스트."""
from __future__ import annotations

import os
import shutil
import tempfile
import time

import pytest

from stardustlib.jbod_manager import JBODManager
from stardustlib.metadata_store import MetadataStore
from stardustlib.storage_source import DirectorySource


def _make_jbod(tmp, device_id, source_id="loop-001"):
    src = DirectorySource(source_id, tmp)
    src.initialize()
    store = MetadataStore(os.path.join(tmp, ".m.db"), b"\x00" * 32)
    store.initialize()
    jbod = JBODManager([src], store, encryption_engine=None, device_id=device_id)
    return jbod, store, src


class TestTakeover:
    """소유권 이전 동작."""

    def test_remote_owned_write_transfers_ownership(self):
        d = tempfile.mkdtemp()
        jbod, store, _src = _make_jbod(d, "dev-A")
        try:
            now = time.time()
            store.insert("/f.txt", "loop-001", "remote_phys.txt", 5, now, now,
                         device_id="dev-B")
            v_before = store.lookup_any("/f.txt").version

            jbod.write_file("/f.txt", b"edited on A")

            rec = store.lookup("/f.txt")
            assert rec.device_id == "dev-A"
            assert rec.sync_status == "pending"
            assert rec.version == v_before + 1
            assert jbod.read_file("/f.txt") == b"edited on A"
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_local_owned_write_keeps_position(self):
        """로컬 소유 파일은 기존 덮어쓰기(같은 위치) 동작 유지."""
        d = tempfile.mkdtemp()
        jbod, store, _src = _make_jbod(d, "dev-A")
        try:
            jbod.write_file("/local.txt", b"v1")
            rec1 = store.lookup("/local.txt")
            jbod.write_file("/local.txt", b"v2-longer")
            rec2 = store.lookup("/local.txt")
            # 같은 물리 위치 유지
            assert rec1.physical_path == rec2.physical_path
            assert rec2.device_id == "dev-A"
            assert jbod.read_file("/local.txt") == b"v2-longer"
            # 로컬 수정은 GC 불필요
            assert jbod._gc_needed is False
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_legacy_null_owner_write_overwrites(self):
        """device_id NULL 레거시 레코드 수정은 덮어쓰기(이전 아님)."""
        d = tempfile.mkdtemp()
        jbod, store, _src = _make_jbod(d, "dev-A")
        try:
            now = time.time()
            # 레거시: device_id NULL, 실제 물리 파일도 생성
            jbod.sources[0].write("legacy_phys.txt", b"old")
            store.insert("/legacy.txt", "loop-001", "legacy_phys.txt", 3,
                         now, now, device_id=None)

            jbod.write_file("/legacy.txt", b"new")

            rec = store.lookup("/legacy.txt")
            # 같은 위치 덮어쓰기 (takeover 아님)
            assert rec.physical_path == "legacy_phys.txt"
            assert jbod.read_file("/legacy.txt") == b"new"
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)


class TestOrphanGC:
    """orphan 물리 파일 정리."""

    def test_gc_removes_unreferenced_file(self):
        """metadata가 가리키지 않는 물리 파일을 삭제한다."""
        d = tempfile.mkdtemp()
        jbod, store, src = _make_jbod(d, "dev-A")
        try:
            # 활성 레코드 1개 (보존 대상)
            jbod.write_file("/keep.txt", b"keep")
            keep_rec = store.lookup("/keep.txt")

            # 디스크에만 있는 고아 파일 (metadata 없음, 관리 파일 형식)
            orphan = "0" * 32 + "_orphan.txt"
            src.write(orphan, b"orphan")
            assert src.exists(orphan)

            removed = jbod.gc_orphan_files()

            assert removed == 1
            assert not src.exists(orphan)
            # 보존 대상은 그대로
            assert src.exists(keep_rec.physical_path)
            assert jbod.read_file("/keep.txt") == b"keep"
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_gc_ignores_non_managed_files(self):
        """관리 파일 형식(<hex32>_)이 아닌 파일은 GC 대상이 아니다(metadata DB 등)."""
        d = tempfile.mkdtemp()
        jbod, store, src = _make_jbod(d, "dev-A")
        try:
            jbod.mark_gc_needed()
            src.write("important.db", b"system file")
            src.write("user_file.txt", b"user data")

            removed = jbod.gc_orphan_files()

            assert removed == 0
            assert src.exists("important.db")
            assert src.exists("user_file.txt")
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_gc_preserves_legacy_null_owner(self):
        """device_id NULL(레거시) 레코드가 참조하는 파일은 보존한다."""
        d = tempfile.mkdtemp()
        jbod, store, src = _make_jbod(d, "dev-A")
        try:
            now = time.time()
            legacy = "1" * 32 + "_legacy.txt"
            src.write(legacy, b"legacy")
            store.insert("/legacy.txt", "loop-001", legacy, 6,
                         now, now, device_id=None)

            removed = jbod.gc_orphan_files()

            assert removed == 0
            assert src.exists(legacy)
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_gc_skips_when_device_id_none(self):
        """device_id가 None이면 GC를 건너뛴다(전체 삭제 방지)."""
        d = tempfile.mkdtemp()
        jbod, store, src = _make_jbod(d, None)
        try:
            some = "2" * 32 + "_data.txt"
            src.write(some, b"data")
            removed = jbod.gc_orphan_files()
            assert removed == 0
            assert src.exists(some)
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_gc_if_needed_debounce(self):
        """gc_orphan_files_if_needed는 플래그가 섰을 때만 1회 동작한다."""
        d = tempfile.mkdtemp()
        jbod, store, src = _make_jbod(d, "dev-A")
        try:
            orphan1 = "3" * 32 + "_o1.txt"
            orphan2 = "4" * 32 + "_o2.txt"
            # 플래그 없음 → 동작 안 함
            src.write(orphan1, b"x")
            assert jbod.gc_orphan_files_if_needed() == 0
            assert src.exists(orphan1)

            # 플래그 set → 1회 동작
            jbod.mark_gc_needed()
            removed = jbod.gc_orphan_files_if_needed()
            assert removed == 1
            assert not src.exists(orphan1)

            # 플래그 소비됨 → 다시 호출하면 동작 안 함
            src.write(orphan2, b"y")
            assert jbod.gc_orphan_files_if_needed() == 0
            assert src.exists(orphan2)
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_takeover_then_gc_full_cycle(self):
        """소유권 이전 후 옛 물리 파일이 GC로 정리되는 전체 흐름."""
        d = tempfile.mkdtemp()
        jbod, store, src = _make_jbod(d, "dev-A")
        try:
            now = time.time()
            old_phys = "5" * 32 + "_shared.txt"
            src.write(old_phys, b"old data")
            store.insert("/shared.txt", "loop-001", old_phys, 8,
                         now, now, device_id="dev-A")

            # device_id를 dev-B로 바꿔 원격 소유 상태를 만든 뒤 수정
            store.update("/shared.txt", file_size=8, modified_at=now,
                         device_id="dev-B", source_id="loop-001",
                         physical_path=old_phys)

            # dev-A가 원격(now dev-B) 소유 파일 수정 → takeover
            jbod.write_file("/shared.txt", b"new data")
            assert jbod._gc_needed is True

            rec = store.lookup("/shared.txt")
            assert rec.device_id == "dev-A"
            new_phys = rec.physical_path
            assert new_phys != old_phys

            # GC: 옛 물리 파일은 더 이상 참조 안 됨 → 삭제, 새 파일은 보존
            removed = jbod.gc_orphan_files_if_needed()
            assert removed == 1
            assert not src.exists(old_phys)
            assert src.exists(new_phys)
            assert jbod.read_file("/shared.txt") == b"new data"
        finally:
            store.close()
            shutil.rmtree(d, ignore_errors=True)
