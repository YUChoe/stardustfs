import os
# import sqlite3
import json

from .log import log
from .config import config


# NODE0PATH = config['nodes'][0]['abs_path']
# DBfile = os.path.join(NODE0PATH, 'hashdata.db')
# SQLITECON = sqlite3.connect(DBfile)


def full_path(partial) -> str:
    if partial.startswith("/"):
        partial = partial[1:]
    log(f'### partial {partial}')
    for node in config['nodes']:
        # path = os.path.join(node['abs_path'], 'live', partial)
        path = os.path.join(node['abs_path'], partial)
        log(f"### path {path}")
        if os.path.exists(path):
            log(f'### full_path out === {path}')
            return path

    # TODO: 없는 파일/디렉토리는 nodes 에서 적당한 node 골라서
    node_for_new = config['nodes'][0]
    return os.path.join(node_for_new['abs_path'], 'live', partial)

    # 모든 노드 db에 같은 값이 들어 있어야 함
    # path = lookup_abspath(partial)


# def lookup_abspath(partial: str) -> str:
#     log(f"### lookup_db {partial}")
#     if  partial:
#         cur = SQLITECON.cursor()
#         res = cur.execute(f"SELECT * FROM stardustfiles WHERE vpath = '{partial}'").fetchone()
#         if res:
#             _vpath, _abspath, _hash = res
#             log(f"{partial} is in db")
#             return _abspath
#     # TODO: 없는 파일은 어떻게?
#     return f"{NODE0PATH}/live"



if __name__ == '__main__':
    print(config)

