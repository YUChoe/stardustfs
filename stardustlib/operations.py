"""
FUSE 의존성 없이 파일시스템 작업을 수행하는 핵심 로직
Windows 테스트 환경에서도 사용 가능
"""
import os
import errno

from .config import config
from .full_path import full_path
from .getattr import get_attr
from .readdir import readdir, mkdir
from .log import log


class StardustOperations:
    """FUSE 독립적인 파일시스템 작업 클래스"""

    def __init__(self, cfg=None):
        if cfg:
            config.clear()
            config.update(cfg)
        self.root = config.get('abs_mount_point', '')

    def _full_path(self, partial):
        return full_path(partial)

    def access(self, path, mode):
        fp = self._full_path(path)
        if not os.access(fp, mode):
            raise OSError(errno.EACCES, 'Permission denied', path)

    def chmod(self, path, mode):
        return os.chmod(self._full_path(path), mode)

    def getattr(self, path, fh=None):
        return get_attr(path, fh)

    def readdir(self, path, fh):
        for r in readdir(path, fh):
            yield r

    def mkdir(self, path, mode):
        return mkdir(path, mode)

    def rmdir(self, path):
        return os.rmdir(self._full_path(path))

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    # 파일 I/O
    def open(self, path, flags):
        return os.open(self._full_path(path), flags)

    def create(self, path, mode, fi=None):
        fp = self._full_path(path)
        return os.open(fp, os.O_WRONLY | os.O_CREAT, mode)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def release(self, path, fh):
        return os.close(fh)

    def truncate(self, path, length, fh=None):
        fp = self._full_path(path)
        with open(fp, 'r+') as f:
            f.truncate(length)

    def flush(self, path, fh):
        return os.fsync(fh)
