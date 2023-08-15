import datetime
import traceback

def log(s: str) -> None:
    print(datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], f'{who_am_i()}', s)

def who_am_i():
    stack = traceback.extract_stack()
    # filename, codeline, funcName, text = stack[-2]
    filename, codeline, funcName, text = stack[-3]
    return f'{filename}\t{funcName}({codeline})'