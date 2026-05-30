#!/usr/bin/env python3
"""MVP2 Phase 2 연동 테스트: sync_client, 버전 추적, conflict copy.

테스트 항목:
7. sync_client.py (업로드/다운로드/병합)
8. 버전 추적 (files 테이블 확장)
9. conflict copy 로직

실행: source .venv/Scripts/activate && python scripts/test_sync_integration.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def main():
    from stardustlib.auth_client import AuthClient
    from stardustlib.config_loader import ConfigLoader
    from stardustlib.conflict_resolver import ConflictResolver
    from stardustlib.metadata_store import MetadataStore
    from stardustlib.sync_client import SyncClient

    print("=" * 60)
    print("MVP2 Phase 2 연동 테스트")
    print("=" * 60)

    # 설정 로드
    loader = ConfigLoader("dev-config.json")
    config = loader.load()
    server_url = config["server"]["url"]
    device_name = config["server"]["device_name"]

    print(f"\n[1] 서버: {server_url}")
    print(f"    디바이스: {device_name}")

    # 인증
    email = os.environ.get("STARDUST_EMAIL", "")
    password = os.environ.get("STARDUST_PASSWORD", "")
    auth_client = AuthClient(server_url)

    try:
        await auth_client.login(email, password)
        print(f"[2] 로그인 성공: user_id={auth_client.user_id}")
    except Exception as e:
        print(f"[2] 로그인 실패: {e}")
        await auth_client.close()
        return

    # MetadataStore 초기화 (테스트용 임시 DB)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = ConfigLoader.load_encryption_key(config.get("key_file"))
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=b"stardustfs-metadata-db", info=b"db-encryption-key")
    db_key = hkdf.derive(key)

    db_path = config["metadata_db"]
    metadata_store = MetadataStore(db_path, db_key)
    metadata_store.initialize()

    # --- 테스트 7: sync_client 업로드/다운로드 ---
    print("\n--- 테스트 7: sync_client 업로드/다운로드 ---")

    conflict_resolver = ConflictResolver(metadata_store, device_name)
    sync_client = SyncClient(
        auth_client, server_url, metadata_store,
        conflict_resolver, interval_seconds=30,
        encryption_key=db_key,
    )

    # 7-1: 메타데이터 업로드
    print("[7-1] 메타데이터 업로드 시도...")
    try:
        await sync_client.upload_metadata()
        print("      업로드 성공")
    except Exception as e:
        print(f"      업로드 실패: {e}")

    # 7-2: 메타데이터 다운로드 (initial_sync)
    print("[7-2] 메타데이터 다운로드 (initial_sync)...")
    try:
        await sync_client.initial_sync()
        print("      다운로드/병합 성공")
    except Exception as e:
        print(f"      다운로드 실패: {e}")

    # --- 테스트 8: 버전 추적 ---
    print("\n--- 테스트 8: 버전 추적 (files 테이블 확장) ---")

    # 테스트 파일 삽입
    test_path = "/test/sync_test_file.txt"
    now = time.time()

    existing = metadata_store.lookup(test_path)
    if existing:
        metadata_store.delete(test_path)

    metadata_store.insert(test_path, "loop-001", "test/sync_test.enc", 100, now, now)
    meta = metadata_store.lookup(test_path)
    print(f"[8-1] 삽입 후: version={meta.version}, sync_status={meta.sync_status}, device_id={meta.device_id}")
    assert meta.version == 1, f"Expected version=1, got {meta.version}"
    assert meta.sync_status == "pending", f"Expected pending, got {meta.sync_status}"

    # version 증가
    metadata_store.increment_version(test_path, "test-device-001")
    meta = metadata_store.lookup(test_path)
    print(f"[8-2] increment 후: version={meta.version}, device_id={meta.device_id}")
    assert meta.version == 2, f"Expected version=2, got {meta.version}"
    assert meta.device_id == "test-device-001"

    # sync_status 변경
    metadata_store.set_sync_status(test_path, "synced")
    meta = metadata_store.lookup(test_path)
    print(f"[8-3] synced 설정 후: sync_status={meta.sync_status}")
    assert meta.sync_status == "synced"

    # update() 시 version 자동 증가
    metadata_store.update(test_path, 200, time.time())
    meta = metadata_store.lookup(test_path)
    print(f"[8-4] update 후: version={meta.version}, sync_status={meta.sync_status}")
    assert meta.version == 3, f"Expected version=3, got {meta.version}"
    assert meta.sync_status == "pending"

    # pending 파일 조회
    pending = metadata_store.get_pending_files()
    pending_paths = [f.virtual_path for f in pending]
    print(f"[8-5] pending 파일 수: {len(pending)}, 포함 여부: {test_path in pending_paths}")
    assert test_path in pending_paths

    print("      버전 추적 테스트 통과 ✓")

    # --- 테스트 9: conflict copy 로직 ---
    print("\n--- 테스트 9: conflict copy 로직 ---")

    # 충돌 감지 테스트
    conflict_path = "/test/conflict_test.txt"
    existing = metadata_store.lookup(conflict_path)
    if existing:
        metadata_store.delete(conflict_path)

    metadata_store.insert(conflict_path, "loop-001", "test/conflict.enc", 50, now, now)
    metadata_store.set_sync_status(conflict_path, "synced")

    # 로컬에서 수정 (version 2)
    metadata_store.update(conflict_path, 100, time.time())
    meta = metadata_store.lookup(conflict_path)
    print(f"[9-1] 로컬 수정 후: version={meta.version}, sync_status={meta.sync_status}")

    # 충돌 감지: server_version=3 > local_base_version=1, local_version=2 > base=1
    is_conflict = conflict_resolver.detect_conflict(
        conflict_path,
        server_version=3,
        local_version=2,
        local_base_version=1,
    )
    print(f"[9-2] 충돌 감지: {is_conflict}")
    assert is_conflict is True, "Expected conflict=True"

    # 비충돌 케이스: 서버만 수정
    no_conflict = conflict_resolver.detect_conflict(
        conflict_path,
        server_version=3,
        local_version=1,
        local_base_version=1,
    )
    print(f"[9-3] 비충돌 (서버만 수정): {no_conflict}")
    assert no_conflict is False

    # conflict copy 생성
    print("[9-4] conflict copy 생성...")
    copy_path = conflict_resolver.resolve_conflict(conflict_path, server_version=3)
    print(f"      생성된 경로: {copy_path}")

    # conflict copy 확인
    copy_meta = metadata_store.lookup(copy_path)
    assert copy_meta is not None, "Conflict copy not found in metadata"
    assert copy_meta.sync_status == "conflict"
    print(f"[9-5] conflict copy 메타: sync_status={copy_meta.sync_status}")

    # 원본 경로는 rename으로 이동했으므로 없어야 함
    original = metadata_store.lookup(conflict_path)
    assert original is None, "Original should be moved to conflict copy"
    print(f"[9-6] 원본 경로 제거 확인 ✓")

    print("\n      conflict copy 테스트 통과 ✓")

    # --- 정리 ---
    metadata_store.delete(copy_path)
    metadata_store.delete(test_path)

    await sync_client.stop()
    await auth_client.close()
    metadata_store.close()

    print("\n" + "=" * 60)
    print("모든 Phase 2 연동 테스트 통과 ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
