import os
# import sqlite3
# import json

from .log import log
from .config import config
from .full_path import full_path as _full_path

def strip_path(path):
    if path.startswith("/"):
        return path[1:]
    log(f'path startswith /: {path}')
    return path


def readdir(path, fh):
    log(f'readdir path:{path} fh:{fh}')
    path = strip_path(path)

    dirents = ['.', '..']
    dir_nodes = []
    for node in config['nodes']:
        full_path = os.path.join(node['abs_path'], path)

        if os.path.isdir(full_path):
            dir_nodes.extend(os.listdir(full_path))

    dirents.extend(list(set(dir_nodes)))

    for r in dirents:
        # log(f'*** readdir out === {r}')
        yield r


def mkdir(path, mode):
    path = strip_path(path)

    log(f"mkdir {path} {mode}")
    # 만들 때 모든 노드 다 만들 것

    log(f'{config["nodes"]}')

    for node in config['nodes']:
        full_path = os.path.join(node['abs_path'], path)
        log(f"os.mkdir {full_path} {mode}")
        rtn = os.mkdir(full_path, mode)
    log(f"returning {rtn}")
    return rtn
