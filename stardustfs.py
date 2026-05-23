#!/usr/bin/env python

# from __future__ import with_statement

import os
import errno

from fuse import FUSE, FuseOSError, Operations

import stardustlib
from stardustlib.log import log


class Passthrough(Operations):
    def __init__(self):
        self.root = stardustlib.config['abs_mount_point']

    def _full_path(self, partial):
        return stardustlib.full_path(partial)

    def access(self, path, mode):
        # log('access in ---')
        full_path = self._full_path(path)
        if not os.access(full_path, mode):
            raise FuseOSError(errno.EACCES)
        # log('access out ===')

    def chmod(self, path, mode):
        # log('chmod in ---')
        full_path = self._full_path(path)
        return os.chmod(full_path, mode)

    def chown(self, path, uid, gid):
        # log('chown')
        full_path = self._full_path(path)
        return os.chown(full_path, uid, gid)

    def getattr(self, path, fh=None):
        return stardustlib.get_attr(path, fh)

    def readdir(self, path, fh):
        for r in stardustlib.readdir(path, fh):
            yield r

    def readlink(self, path):
        log(f'readlink {path}')
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            # Path name is absolute, sanitize it.
            return os.path.relpath(pathname, self.root)
        else:
            return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        # TODO: 남은 파일있는 디렉토리 리턴 > 에러 발생
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        log(f"mkdir {path} {mode}")
        # TODO: 만들 때 모든 노드 다 만들 것
        # 아니면 일단 한 디렉토리에만 쓰다가 용량 부족할 때 다른 노드에 만들 것
        return stardustlib.mkdir(path, mode)
        # return os.mkdir(self._full_path(path), mode)

    def statfs(self, path):
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def symlink(self, name, target):
        return stardustlib.symlink(name, target)

    def _relative_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        return os.path.join('.', partial)


    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    def link(self, target, name):
        return os.link(self._full_path(target), self._full_path(name))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    # File methods
    # ============

    def open(self, path, flags):
        full_path = self._full_path(path)
        return os.open(full_path, flags)

    def create(self, path, mode, fi=None):
        full_path = self._full_path(path)
        return os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def truncate(self, path, length, fh=None):
        full_path = self._full_path(path)
        with open(full_path, 'r+') as f:
            f.truncate(length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)


def main(mountpoint):
    FUSE(Passthrough(), mountpoint, nothreads=True, foreground=True)

def logo():
    o  = '                                           88                          888888888 ad8888ba  \n'
    o += '           ,d                              88                     ,d   88       d8"    "8b \n'
    o += '           88                              88                     88   88       Y8,        \n'
    o += ',adPPYba,MM88MMM,adPPYYba, 8b,dPYba ,adPYb,8888      88,adPPYba,MM88MMM88aaaa   `Y8aaaa,   \n'
    o += 'I8[    ""  88   ""     `Y8 88P"  "Ya8"   `Y8888      88I8[    ""  88   88""""     `""""8b, \n'
    o += ' `"Y8ba,   88   ,adPPPPP88 88      8b      8888      88 `"Y8ba,   88   88              `8b \n'
    o += 'aa    ]8I  88,  88,    ,88 88      "8a,  ,d88"8a,  ,a88aa    ]8I  88,  88       Y8a    a8P \n'
    o += '`"YbbdP""  "Y888`"8bbdP"Y8 88       `"8bdP"Y8 `"YbdP"Y8`"YbbdP""  "Y88888        "Y8888P"  \n'
    print(o)

def init_node(node) -> bool:
    path = node['abs_path']
    log(f'init_node {node} path{path}')
    if not os.path.isdir(path):
        log(f'Error to init {path}')
        return False

    # db = os.path.join(path, 'hashdata.db')
    # livefiles = os.path.join(path, 'live')
    # if os.path.isfile(db): return
    # if not os.path.isdir(path): os.mkdir(path)
    # if not os.path.isdir(livefiles): os.mkdir(os.path.join(path, 'live'))
    # # TODO: sqlite
    # # TODO: OLD files
    log(f'node init complete {path}')
    return True

if __name__ == '__main__':
    logo()
    log(f'config {stardustlib.config}')
    _config = stardustlib.config
    for node in _config['nodes']:
        if not init_node(node):
            exit(1)

    # main(sys.argv[2], sys.argv[1])
    main(_config['abs_mount_point'])
