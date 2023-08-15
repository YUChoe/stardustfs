import os
# import sqlite3
# import json

from .log import log
from .config import config
from .full_path import full_path as _full_path


def readdir(path, fh):
    log(f'readdir path:{path} fh:{fh}')

    if path.startswith("/"):
        path = path[1:]
        log(f'path startswith /: {path}')

    dirents = ['.', '..']

    for node in config['nodes']:
        full_path = os.path.join(node['abs_path'], path)
        if os.path.isdir(full_path):
            dirents.extend(os.listdir(full_path))

    for r in dirents:
        # log(f'*** readdir out === {r}')
        yield r
