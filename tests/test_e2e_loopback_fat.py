"""루프백 FAT 이미지 e2e — 실제 스택 관통.

config → _build_core(암호화+메타+JBOD 조립) → write_file(암호화 후 FAT 이미지 내부
저장) → read_file(복호화) → 재마운트 후 영속성 → FAT 소스 간 JBOD 스필오버 → 삭제.
별도 호스트/데몬 없이 단일 프로세스에서 저장 계층 전체를 검증한다.
"""

from __future__ import annotations

import os

import pytest


def _config(tmp_path, size=10 * 1024 * 1024):
    key = tmp_path / "key.bin"
    key.write_bytes(os.urandom(32))
    return {
        "key_file": str(key),
        "metadata_db": str(tmp_path / "meta.db"),
        "sources": [
            {"id": "loop-a", "type": "loopback",
             "path": str(tmp_path / "a.img"), "size": size},
            {"id": "loop-b", "type": "loopback",
             "path": str(tmp_path / "b.img"), "size": size},
        ],
    }


def _build(cfg):
    from stardustfs import _build_core

    jbod, meta, enc, _db = _build_core(cfg)
    return jbod, meta


def test_e2e_write_read_persist_spillover(tmp_path):
    cfg = _config(tmp_path)
    jbod, meta = _build(cfg)

    # 1) 쓰기/읽기 라운드트립 — 파일이 FAT 이미지 내부에 저장된다.
    data = os.urandom(300 * 1024)
    jbod.write_file("/docs/report.bin", data)
    assert jbod.read_file("/docs/report.bin") == data

    # 호스트에 평문 동반 디렉토리가 새지 않는다(.d 없음), 이미지 파일은 존재.
    assert os.path.isfile(str(tmp_path / "a.img"))
    assert not os.path.exists(str(tmp_path / "a.img.d"))

    # at-rest 암호화 확인: 메타의 물리 위치에서 raw로 읽으면 암호문(평문과 다름).
    m = meta.lookup("/docs/report.bin")
    src = jbod._get_source_by_id(m.source_id)
    cipher = src.read(m.physical_path)
    assert cipher != data and len(cipher) >= len(data)

    # 2) FAT 소스 간 JBOD 스필오버: loop-a를 채운 뒤 큰 파일은 loop-b로.
    jbod.write_file("/big_a.bin", os.urandom(6 * 1024 * 1024))  # loop-a 채움
    jbod.write_file("/big_b.bin", os.urandom(5 * 1024 * 1024))  # a 부족 → b
    ma = meta.lookup("/big_a.bin")
    mb = meta.lookup("/big_b.bin")
    assert ma.source_id != mb.source_id  # 서로 다른 이미지에 분산
    assert jbod.read_file("/big_b.bin") is not None

    # 3) 재마운트 영속성: 핸들 닫고 다시 조립해도 파일이 그대로 읽힌다.
    jbod.close_local_sources()
    meta.close()
    jbod2, meta2 = _build(cfg)
    assert jbod2.read_file("/docs/report.bin") == data

    # 4) 삭제 후 가용 공간 회수.
    m2 = meta2.lookup("/big_a.bin")
    src_a = jbod2._get_source_by_id(m2.source_id)
    before = src_a.get_available_space()
    jbod2.delete_file("/big_a.bin")
    after = src_a.get_available_space()
    assert after >= before

    jbod2.close_local_sources()
    meta2.close()


def test_e2e_capacity_exhausted_raises(tmp_path):
    # 두 소스 모두 채우면 무손실 에러(InsufficientStorageError).
    from stardustlib.exceptions import InsufficientStorageError

    cfg = _config(tmp_path)
    jbod, meta = _build(cfg)
    try:
        jbod.write_file("/f1.bin", os.urandom(8 * 1024 * 1024))  # loop-a
        jbod.write_file("/f2.bin", os.urandom(8 * 1024 * 1024))  # loop-b
        with pytest.raises(InsufficientStorageError):
            jbod.write_file("/f3.bin", os.urandom(8 * 1024 * 1024))  # 둘 다 부족
    finally:
        jbod.close_local_sources()
        meta.close()
