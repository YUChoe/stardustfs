"""
FUSE 마운트 없이 StardustFS 로직을 테스트하기 위한 Mock 레이어
Windows 환경에서도 실행 가능
"""
import os
import tempfile
import shutil


class MockFUSE:
    """Passthrough 클래스를 직접 호출하여 FUSE 없이 테스트"""

    def __init__(self, operations):
        self.ops = operations

    def ls(self, path: str) -> list:
        """디렉토리 내용 조회"""
        return list(self.ops.readdir(path, None))

    def stat(self, path: str) -> dict:
        """파일/디렉토리 속성 조회"""
        return self.ops.getattr(path)

    def mkdir(self, path: str, mode: int = 0o755) -> int:
        """디렉토리 생성"""
        return self.ops.mkdir(path, mode)

    def rmdir(self, path: str) -> int:
        """디렉토리 삭제"""
        return self.ops.rmdir(path)

    def cat(self, path: str, size: int = 4096) -> bytes:
        """파일 내용 읽기"""
        fh = self.ops.open(path, os.O_RDONLY)
        try:
            data = self.ops.read(path, size, 0, fh)
            return data
        finally:
            self.ops.release(path, fh)

    def write_file(self, path: str, content: bytes, mode: int = 0o644) -> int:
        """파일 생성 및 쓰기"""
        fh = self.ops.create(path, mode)
        try:
            written = self.ops.write(path, content, 0, fh)
            return written
        finally:
            self.ops.release(path, fh)

    def rm(self, path: str) -> int:
        """파일 삭제"""
        return self.ops.unlink(path)

    def mv(self, old: str, new: str) -> int:
        """파일/디렉토리 이름 변경"""
        return self.ops.rename(old, new)

    def exists(self, path: str) -> bool:
        """파일/디렉토리 존재 여부"""
        try:
            self.ops.getattr(path)
            return True
        except (OSError, FileNotFoundError):
            return False


class TestEnvironment:
    """테스트용 임시 노드 환경 생성"""

    def __init__(self, node_count: int = 3):
        self.node_count = node_count
        self.temp_dir = None
        self.nodes = []
        self.mount_point = None

    def setup(self) -> dict:
        """임시 디렉토리로 테스트 환경 구성"""
        self.temp_dir = tempfile.mkdtemp(prefix='stardustfs_test_')
        self.mount_point = os.path.join(self.temp_dir, 'mnt')
        os.makedirs(self.mount_point)

        self.nodes = []
        for i in range(self.node_count):
            node_path = os.path.join(self.temp_dir, f'd{i+1:03d}')
            os.makedirs(node_path)
            self.nodes.append({
                'type': 'local_partition',
                'abs_path': node_path
            })

        return {
            'version': 1,
            'abs_mount_point': self.mount_point,
            'nodes': self.nodes
        }

    def teardown(self):
        """테스트 환경 정리"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None
            self.nodes = []
            self.mount_point = None

    def __enter__(self):
        return self.setup()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.teardown()
        return False
