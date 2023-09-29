import os
# import sqlite3
import json

from .log import log
from .config import config

NODE_ALGORITHM = "RR"

# NODE0PATH = config['nodes'][0]['abs_path']
# DBfile = os.path.join(NODE0PATH, 'hashdata.db')
# SQLITECON = sqlite3.connect(DBfile)


def full_path(partial) -> str:
    if partial.startswith("/"):
        partial = partial[1:]
        # log(f'partial startswith /: {partial}')

    for node in config['nodes']:
        # path = os.path.join(node['abs_path'], 'live', partial)
        path = os.path.join(node['abs_path'], partial)
        # log(f"lookup each node {path}")
        # TODO: if isdir(path) == True RR
        if os.path.exists(path):
            # log(f'### full_path out === {path}')
            return path
        else:
            # how do i know path is dir or file?
            pass

    # log(f'coun\'t find {partial}')
    node_for_new = config['nodes'][get_node_byalgorithm()]
    log(f'node_for_new[{node_for_new}]')
    return os.path.join(node_for_new['abs_path'], partial)

    # 모든 노드 db에 같은 값이 들어 있어야 함
    # path = lookup_abspath(partial)

LATEST_NODE = 0
def get_node_byalgorithm() -> int:
    global LATEST_NODE
    sz = len(config['nodes'])
    if NODE_ALGORITHM == 'RR':
        log(f'RR nodes[{sz}]')
        LATEST_NODE += 1
        if LATEST_NODE >= sz:
            LATEST_NODE = 0
    log(f'picked node[{LATEST_NODE}]')
    return LATEST_NODE

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

