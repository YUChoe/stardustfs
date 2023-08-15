from .log import log
from .full_path import full_path as _full_path
import os

def get_attr(path, fh=None):
    # log(f'get_attr xx {path}')
    full_path = _full_path(path)
    # log(f'get_attr full_path {full_path}')
    st = os.lstat(full_path)
    return dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))
