"""LoopbackSource(파일 내 FAT 이미지) 테스트.

`<path>`가 고정 크기 FAT 이미지로 포맷되고 모든 파일이 이미지 내부에 저장되는지,
동반 디렉토리(`.d`)가 더 이상 생기지 않는지, 용량이 실제로 한정되는지 검증한다.
"""

from __future__ import annotations

import os

import pytest

from stardustlib.storage_source import LoopbackSource

SIZE = 10 * 1024 * 1024  # 10 MiB(최소)


def _src(tmp_path, name="vol.img", size=SIZE, read_only=False):
    s = LoopbackSource(name, str(tmp_path / name), size, read_only=read_only)
    s.initialize()
    return s


def test_format_creates_fat_image_no_companion_dir(tmp_path):
    s = _src(tmp_path)
    assert s.is_active
    img = str(tmp_path / "vol.img")
    assert os.path.isfile(img)
    # 동반 디렉토리(.d)는 더 이상 생기지 않는다.
    assert not os.path.exists(img + ".d")
    # 진짜 FAT 이미지인지: read_only로 다시 열려야 한다.
    assert s._is_fat_image() is True
    s.close()


def test_write_read_roundtrip_inside_image(tmp_path):
    s = _src(tmp_path)
    data = os.urandom(200 * 1024)
    s.write("abc_song.mp3", data)
    assert s.read("abc_song.mp3") == data
    assert s.exists("abc_song.mp3")
    # 하위 경로
    s.write("dir/def_clip.bin", b"hello" * 100)
    assert s.read("dir/def_clip.bin") == b"hello" * 100
    assert "abc_song.mp3" in s.list_physical_files()
    # 파일이 호스트에 평문으로 새지 않는다(동반 디렉토리 없음)
    assert not os.path.exists(str(tmp_path / "vol.img.d"))
    s.close()


def test_chunk_roundtrip(tmp_path):
    s = _src(tmp_path)
    data = os.urandom(2 * 1024 * 1024 + 77)
    total = len(data)
    step = 512 * 1024
    for off in range(0, total, step):
        s.write_chunk("c.bin", data[off:off + step], off, total)
    out = b""
    off = 0
    while True:
        part = s.read_chunk("c.bin", off, step)
        out += part
        off += len(part)
        if len(part) < step:
            break
    assert out == data
    s.close()


def test_capacity_enforced(tmp_path):
    s = _src(tmp_path)
    with pytest.raises(OSError, match="insufficient space"):
        s.write("big.bin", b"x" * (12 * 1024 * 1024))  # >10MiB 이미지
    # 부분 파일이 남지 않아야 한다
    assert not s.exists("big.bin")
    s.close()


def test_space_accounting(tmp_path):
    s = _src(tmp_path)
    assert s.get_total_space() == SIZE
    before = s.get_available_space()
    s.write("a.bin", b"y" * (1024 * 1024))
    after = s.get_available_space()
    assert after <= before - 1024 * 1024 + 4096  # 근사(클러스터 반올림 허용)
    s.delete("a.bin")
    assert s.get_available_space() >= after
    s.close()


def test_read_missing_raises_filenotfound(tmp_path):
    s = _src(tmp_path)
    with pytest.raises(FileNotFoundError):
        s.read("nope.bin")
    s.close()


def test_dir_ops(tmp_path):
    s = _src(tmp_path)
    s.mkdir("sub")
    assert s.exists("sub")
    s.write("sub/f.bin", b"z" * 10)
    assert "f.bin" in s.list_dir("sub")
    s.rmdir("sub")
    assert not s.exists("sub")
    s.close()


def test_read_only_on_missing_image_is_inactive(tmp_path):
    # 이미지가 아직 없으면 read_only 소스는 비활성(데몬이 생성하기 전)
    s = _src(tmp_path, name="ro.img", read_only=True)
    assert not s.is_active


def test_read_only_blocks_write(tmp_path):
    rw = _src(tmp_path, name="x.img")
    rw.write("a.bin", b"data")
    rw.close()
    ro = _src(tmp_path, name="x.img", read_only=True)
    assert ro.is_active
    assert ro.read("a.bin") == b"data"
    with pytest.raises(Exception):
        ro.write("b.bin", b"nope")
    ro.close()
