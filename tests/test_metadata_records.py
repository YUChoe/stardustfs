"""metadata_records 헬퍼 단위 테스트 (record_id 파생, 패딩, 직렬화)."""

import pytest

from stardustlib.metadata_records import (
    _PAD_BLOCK,
    derive_record_subkey,
    deserialize_metadata,
    pad_plaintext,
    record_id_for,
    serialize_metadata,
    unpad_plaintext,
)
from stardustlib.models import FileMetadata


def _sample_fm(path="/docs/report.txt", deleted=False):
    """테스트용 FileMetadata."""
    return FileMetadata(
        virtual_path=path,
        source_id="loop-001",
        physical_path="ab/cd/deadbeef.enc",
        file_size=12345,
        created_at=1700000000.5,
        modified_at=1700000100.25,
        version=3,
        device_id="dev-abc",
        sync_status="pending",
        deleted=deleted,
        replication_status="replicated",
    )


class TestRecordId:
    def test_deterministic(self):
        """같은 키·경로는 항상 같은 record_id."""
        key = b"\x11" * 32
        sub = derive_record_subkey(key)
        a = record_id_for(sub, "/a/b/c.txt")
        b = record_id_for(sub, "/a/b/c.txt")
        assert a == b
        assert len(a) == 64  # sha256 hex

    def test_different_paths_differ(self):
        sub = derive_record_subkey(b"\x11" * 32)
        assert record_id_for(sub, "/a.txt") != record_id_for(sub, "/b.txt")

    def test_different_keys_differ(self):
        """키가 다르면 같은 경로라도 record_id가 다르다."""
        a = record_id_for(derive_record_subkey(b"\x11" * 32), "/a.txt")
        b = record_id_for(derive_record_subkey(b"\x22" * 32), "/a.txt")
        assert a != b

    def test_none_key_deterministic(self):
        """개발 모드(키 없음)에서도 결정적으로 파생된다."""
        a = record_id_for(derive_record_subkey(None), "/a.txt")
        b = record_id_for(derive_record_subkey(None), "/a.txt")
        assert a == b


class TestPadding:
    def test_round_trip(self):
        for data in [b"", b"x", b"hello world", b"\x00\x01\x02", b"a" * 300]:
            padded = pad_plaintext(data)
            assert len(padded) % _PAD_BLOCK == 0
            assert unpad_plaintext(padded) == data

    def test_short_input_is_one_block(self):
        assert len(pad_plaintext(b"short")) == _PAD_BLOCK

    def test_exact_boundary(self):
        """4B 헤더 포함 정확히 블록 경계면 그 크기를 유지한다."""
        data = b"y" * (_PAD_BLOCK - 4)
        padded = pad_plaintext(data)
        assert len(padded) == _PAD_BLOCK
        assert unpad_plaintext(padded) == data

    def test_unpad_rejects_truncated(self):
        with pytest.raises(ValueError):
            unpad_plaintext(b"\x00")

    def test_unpad_rejects_bad_length(self):
        """길이 프리픽스가 실제 크기를 초과하면 오류."""
        import struct
        bad = struct.pack(">I", 9999) + b"short"
        with pytest.raises(ValueError):
            unpad_plaintext(bad)


class TestSerialize:
    def test_round_trip_preserves_fields(self):
        fm = _sample_fm()
        restored = deserialize_metadata(serialize_metadata(fm))
        assert restored.virtual_path == fm.virtual_path
        assert restored.source_id == fm.source_id
        assert restored.physical_path == fm.physical_path
        assert restored.file_size == fm.file_size
        assert restored.created_at == fm.created_at
        assert restored.modified_at == fm.modified_at
        assert restored.version == fm.version
        assert restored.device_id == fm.device_id
        assert restored.sync_status == fm.sync_status
        assert restored.deleted == fm.deleted
        assert restored.replication_status == fm.replication_status

    def test_tombstone_round_trip(self):
        fm = _sample_fm(deleted=True)
        assert deserialize_metadata(serialize_metadata(fm)).deleted is True

    def test_none_device_id(self):
        fm = _sample_fm()
        fm.device_id = None
        assert deserialize_metadata(serialize_metadata(fm)).device_id is None

    def test_full_pipeline_round_trip(self):
        """직렬화→패딩→언패딩→역직렬화 전체 파이프라인."""
        fm = _sample_fm()
        restored = deserialize_metadata(
            unpad_plaintext(pad_plaintext(serialize_metadata(fm)))
        )
        assert restored.virtual_path == fm.virtual_path
        assert restored.version == fm.version
