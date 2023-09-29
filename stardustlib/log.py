import datetime
import traceback
import os


def log(s: str) -> None:
    print(datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], f'{stackname()}', s)


def stackname():
    stack = traceback.extract_stack()
    filename, codeline, funcName, text = stack[-3]
    return f'{os.path.basename(filename)}:{funcName}({codeline})'
