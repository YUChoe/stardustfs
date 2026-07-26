"""파셜/증분 메타데이터 동기화 헬퍼.

record_id 파생(경로의 HMAC), FileMetadata 직렬화, 256바이트 패딩을 제공한다.
암호화 자체는 SyncClient의 blob 암호화 경로(_encrypt_blob/_decrypt_blob)를 재사용한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct

from stardustlib.models import ChunkRef, FileMetadata

# record_id용 subkey 파생 정보 문자열
_RECORD_ID_INFO = b"stardustfs-record-id"
# 레코드 평문 패딩 블록 크기(바이트). 암호문 크기를 이 배수로 양자화한다.
_PAD_BLOCK = 256
# encryption_key가 없는 개발 모드에서 쓰는 고정 subkey 재료(zero-knowledge 비적용 환경)
_DEV_KEY = b"\x00" * 32

# 서버로 동기화되는 FileMetadata 필드(로컬 전용 evicted 제외)
_SYNC_FIELDS = (
    "virtual_path",
    "source_id",
    "physical_path",
    "file_size",
    "created_at",
    "modified_at",
    "version",
    "device_id",
    "sync_status",
    "deleted",
    "replication_status",
)


def derive_record_subkey(encryption_key: bytes | None) -> bytes:
    """encryption_key에서 record_id용 subkey(32B)를 HKDF-SHA256으로 파생한다.

    encryption_key가 None(개발 모드)이면 고정 재료로 파생한다.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ikm = encryption_key if encryption_key is not None else _DEV_KEY
    hkdf = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_RECORD_ID_INFO
    )
    return hkdf.derive(ikm)


def record_id_for(subkey: bytes, virtual_path: str) -> str:
    """경로의 HMAC-SHA256 hex를 반환한다(결정적, 불투명)."""
    return hmac.new(
        subkey, virtual_path.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def pad_plaintext(plaintext: bytes) -> bytes:
    """`[4B 길이(BE)] + [평문] + [0x00 패딩]`을 256B 배수로 만든다."""
    body = struct.pack(">I", len(plaintext)) + plaintext
    remainder = len(body) % _PAD_BLOCK
    if remainder:
        body += b"\x00" * (_PAD_BLOCK - remainder)
    return body


def unpad_plaintext(padded: bytes) -> bytes:
    """pad_plaintext의 역연산: 길이 프리픽스로 원본을 정확히 복원한다."""
    if len(padded) < 4:
        raise ValueError("패딩된 레코드가 최소 크기(4B) 미만입니다")
    (length,) = struct.unpack(">I", padded[:4])
    if 4 + length > len(padded):
        raise ValueError("길이 프리픽스가 실제 크기를 초과합니다")
    return padded[4:4 + length]


def serialize_metadata(
    fm: FileMetadata, chunks: list | None = None
) -> bytes:
    """FileMetadata를 동기화 필드만 담은 JSON bytes로 직렬화한다(패딩 전).

    청크 표현 파일이면 청크 매니페스트를 `chunks` 배열로 함께 담는다. 동기화 단위는
    여전히 파일이므로 record_id·CAS·롱폴 프로토콜은 바뀌지 않는다. 레거시 통짜 blob
    파일은 `chunks`를 생략한다.

    Args:
        fm: 파일 메타데이터.
        chunks: ChunkRef 목록(비었거나 None이면 생략).
    """
    data = {field: getattr(fm, field) for field in _SYNC_FIELDS}
    if chunks:
        data["chunks"] = [
            {
                "index": c.index,
                "chunk_ref": c.chunk_ref,
                "source_id": c.source_id,
                "device_id": c.device_id,
                "size": c.size,
                "hash": c.hash,
            }
            for c in sorted(chunks, key=lambda c: c.index)
        ]
    return json.dumps(
        data, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def deserialize_chunks(data: bytes) -> list:
    """레코드 JSON에서 청크 매니페스트를 복원한다.

    `chunks`가 없으면(레거시 레코드 또는 통짜 blob 파일) 빈 목록을 반환한다.
    """
    obj = json.loads(data.decode("utf-8"))
    entries = obj.get("chunks") or []
    return [
        ChunkRef(
            index=e["index"],
            chunk_ref=e["chunk_ref"],
            source_id=e["source_id"],
            device_id=e.get("device_id"),
            size=e.get("size", 0),
            hash=e.get("hash"),
        )
        for e in sorted(entries, key=lambda e: e["index"])
    ]


def deserialize_metadata(data: bytes) -> FileMetadata:
    """JSON bytes를 FileMetadata로 역직렬화한다(evicted는 로컬 기본값)."""
    obj = json.loads(data.decode("utf-8"))
    return FileMetadata(
        virtual_path=obj["virtual_path"],
        source_id=obj["source_id"],
        physical_path=obj["physical_path"],
        file_size=obj["file_size"],
        created_at=obj["created_at"],
        modified_at=obj["modified_at"],
        version=obj["version"],
        device_id=obj.get("device_id"),
        sync_status=obj.get("sync_status", "synced"),
        deleted=bool(obj.get("deleted", False)),
        replication_status=obj.get("replication_status", "none"),
    )
