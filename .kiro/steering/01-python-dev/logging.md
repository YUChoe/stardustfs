# 로깅 규칙

## 기본 원칙

- **Python 표준 logging 모듈 사용**
- **구조화된 로깅**: 일관된 형식으로 로그 메시지 작성
- **적절한 로그 레벨 사용**: DEBUG, INFO, WARNING, ERROR, CRITICAL
- **표준 출력 포맷**: `{시분초.ms} {LEVEL} [{filename.py:line}] {logstring}`

## 로그 출력 포맷 규칙

**필수 포맷**: `{시분초.ms} {LEVEL} [{filename.py:line}] {logstring}`

**예시 출력**:

```
14:23:45.123 INFO [game_engine.py:45] Player john connected to server
14:23:45.456 DEBUG [database.py:123] Executing query: SELECT * FROM players
14:23:45.789 ERROR [session_manager.py:67] Failed to authenticate user: Invalid token
14:23:46.012 WARNING [world_manager.py:234] Room capacity exceeded
```

**포맷터 설정**:

```python
# 커스텀 포맷터 클래스
class MudEngineFormatter(logging.Formatter):
    def format(self, record):
        # 시분초.밀리초 형식
        timestamp = self.formatTime(record, '%H:%M:%S')
        ms = int(record.created * 1000) % 1000
        time_with_ms = f"{timestamp}.{ms:03d}"

        # 파일명과 라인 번호
        filename = record.filename
        lineno = record.lineno
        location = f"[{filename}:{lineno}]"

        # 최종 포맷
        return f"{time_with_ms} {record.levelname} {location} {record.getMessage()}"

# 포맷터 적용 예제
formatter = MudEngineFormatter()
```

## 로거 설정

```python
import logging

# 모듈별 로거 생성
logger = logging.getLogger(__name__)

# 클래스 내부에서 사용 시
class MyClass:
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
```

## 로그 레벨 가이드라인

### DEBUG

- 개발 중 디버깅 정보
- 상세한 실행 흐름

```python
logger.debug(f"Processing user input: {user_input}")
logger.debug(f"Database query: {query}")
```

### INFO

- 정상적인 프로그램 실행 정보
- 중요한 비즈니스 이벤트

```python
logger.info(f"Player {player_name} connected")
logger.info(f"Starting {operation_name}")
logger.info(f"{operation_name} completed successfully")
```

### WARNING

- 예상치 못한 상황이지만 프로그램은 계속 실행
- 잠재적 문제 상황

```python
logger.warning("Required method not found")
logger.warning(f"Player {player_id} attempted invalid action")
```

### ERROR

- 오류 발생으로 기능 실행 실패
- 예외 상황

```python
logger.error(f"{operation_name} failed: {e}", exc_info=True)
logger.error(f"Database connection failed: {error}")
```

### CRITICAL

- 시스템 전체에 영향을 주는 심각한 오류
- 프로그램 종료가 필요한 상황

```python
logger.critical("Database initialization failed - shutting down")
```

## 로깅 패턴

### 1. 작업 시작/완료 로깅

```python
logger.info(f"Starting {operation_name}")
try:
    result = await operation()
    logger.info(f"{operation_name} completed successfully")
    return result
except Exception as e:
    logger.error(f"{operation_name} failed: {e}", exc_info=True)
    raise
```

### 2. 예외 처리 로깅

```python
try:
    # 작업 수행
    pass
except SpecificException as e:
    logger.error(f"Specific error occurred: {e}")
    # 복구 로직
except Exception as e:
    logger.error(f"Unexpected error: {e}", exc_info=True)
    raise
```

### 3. 조건부 로깅

```python
if not hasattr(obj, 'required_method'):
    logger.warning("Required method not found")
    return None

if validation_failed:
    logger.error(f"Validation failed for {data}")
    return False
```

### 4. 성능 모니터링

```python
import time

start_time = time.time()
# 작업 수행
elapsed_time = time.time() - start_time
logger.info(f"Operation completed in {elapsed_time:.2f} seconds")
```

## 로그 메시지 형식

### 일관된 메시지 구조

```python
# 좋은 예
logger.info(f"Player {player_name} joined room {room_id}")
logger.error(f"Failed to save player {player_id}: {error_message}")

# 나쁜 예
logger.info("player joined")
logger.error("save failed")
```

### 컨텍스트 정보 포함

```python
# 사용자 액션 로깅
logger.info(f"User {user_id} executed command '{command}' in room {room_id}")

# 시스템 상태 로깅
logger.info(f"Server started - listening on {host}:{port}")
```

## 보안 고려사항

- **민감한 정보 로깅 금지**: 비밀번호, 토큰, 개인정보
- **사용자 입력 검증**: 로그 인젝션 방지

```python
# 안전한 로깅
logger.info(f"Login attempt for user: {username}")

# 위험한 로깅 (금지)
logger.info(f"Login attempt: {username}:{password}")
```

## 성능 고려사항

- **지연 평가 사용**: f-string 대신 % 포매팅 또는 lazy evaluation
- **조건부 로깅**: 불필요한 문자열 생성 방지

```python
# 성능 최적화
if logger.isEnabledFor(logging.DEBUG):
    logger.debug(f"Complex debug info: {expensive_operation()}")

# 또는
logger.debug("Complex debug info: %s", expensive_operation)
```

## 로그 파일 관리 규칙

- **파일명 형식**: `mud_engine-{YYYYMMDD}-{no}.log`
- **파일 크기 제한**: 200MB
- **로테이션 조건**: 파일 크기 초과 또는 날짜 변경
- **압축**: 로테이트된 과거 로그는 gzip 압축

## 로그 설정 예제

```python
import logging
import logging.handlers
import sys
from datetime import datetime
import os

def setup_logging(level=logging.INFO, log_dir='logs'):
    """로깅 설정 초기화"""
    # 로그 디렉토리 생성
    os.makedirs(log_dir, exist_ok=True)

    # 현재 날짜 기반 로그 파일명
    today = datetime.now().strftime('%Y%m%d')
    log_filename = os.path.join(log_dir, f'mud_engine-{today}-01.log')

    # 커스텀 포맷터 (필수 포맷 적용)
    class MudEngineFormatter(logging.Formatter):
        def format(self, record):
            # 시분초.밀리초 형식
            timestamp = self.formatTime(record, '%H:%M:%S')
            ms = int(record.created * 1000) % 1000
            time_with_ms = f"{timestamp}.{ms:03d}"

            # 파일명과 라인 번호
            filename = record.filename
            lineno = record.lineno
            location = f"[{filename}:{lineno}]"

            # 최종 포맷: {시분초.ms} {LEVEL} [{filename.py:line}] {logstring}
            return f"{time_with_ms} {record.levelname} {location} {record.getMessage()}"

    formatter = MudEngineFormatter()

    # 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # 파일 핸들러 (TimedRotatingFileHandler + 크기 제한)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_filename,
        when='midnight',
        interval=1,
        backupCount=30,  # 30일간 보관
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    # 크기 기반 로테이션을 위한 커스텀 핸들러
    size_handler = logging.handlers.RotatingFileHandler(
        filename=log_filename,
        maxBytes=200 * 1024 * 1024,  # 200MB
        backupCount=10,
        encoding='utf-8'
    )
    size_handler.setFormatter(formatter)

    # 루트 로거 설정
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    return root_logger

class CustomRotatingHandler(logging.handlers.BaseRotatingHandler):
    """날짜와 크기 기반 로그 로테이션 핸들러"""

    def __init__(self, filename, maxBytes=200*1024*1024, backupCount=30, encoding='utf-8'):
        self.maxBytes = maxBytes
        self.backupCount = backupCount
        self.current_date = datetime.now().strftime('%Y%m%d')
        self.file_number = 1

        # 파일명 생성
        self.base_filename = filename
        self.current_filename = self._get_current_filename()

        super().__init__(self.current_filename, 'a', encoding=encoding)

    def _get_current_filename(self):
        """현재 로그 파일명 생성"""
        today = datetime.now().strftime('%Y%m%d')
        if today != self.current_date:
            self.current_date = today
            self.file_number = 1

        return f"logs/mud_engine-{self.current_date}-{self.file_number:02d}.log"

    def shouldRollover(self, record):
        """로테이션 필요 여부 확인"""
        # 날짜 변경 확인
        today = datetime.now().strftime('%Y%m%d')
        if today != self.current_date:
            return True

        # 파일 크기 확인
        if self.stream is None:
            self.stream = self._open()

        if self.maxBytes > 0:
            msg = "%s\n" % self.format(record)
            self.stream.seek(0, 2)  # EOF로 이동
            if self.stream.tell() + len(msg) >= self.maxBytes:
                return True

        return False

    def doRollover(self):
        """로그 파일 로테이션 수행"""
        if self.stream:
            self.stream.close()
            self.stream = None

        # 현재 파일 압축
        import gzip
        import shutil

        current_file = self.current_filename
        if os.path.exists(current_file):
            compressed_file = f"{current_file}.gz"
            with open(current_file, 'rb') as f_in:
                with gzip.open(compressed_file, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            os.remove(current_file)

        # 새 파일명 생성
        today = datetime.now().strftime('%Y%m%d')
        if today != self.current_date:
            self.current_date = today
            self.file_number = 1
        else:
            self.file_number += 1

        self.current_filename = self._get_current_filename()
        self.baseFilename = self.current_filename

        # 새 스트림 열기
        if not self.delay:
            self.stream = self._open()

        # 오래된 로그 파일 정리
        self._cleanup_old_logs()

    def _cleanup_old_logs(self):
        """오래된 로그 파일 정리"""
        log_dir = os.path.dirname(self.current_filename)
        if not os.path.exists(log_dir):
            return

        # 로그 파일 목록 가져오기
        log_files = []
        for filename in os.listdir(log_dir):
            if filename.startswith('mud_engine-') and filename.endswith('.log.gz'):
                filepath = os.path.join(log_dir, filename)
                log_files.append((os.path.getctime(filepath), filepath))

        # 생성 시간 기준 정렬
        log_files.sort()

        # 백업 개수 초과 시 오래된 파일 삭제
        while len(log_files) > self.backupCount:
            _, old_file = log_files.pop(0)
            try:
                os.remove(old_file)
            except OSError:
                pass

# 사용 예제
def setup_advanced_logging(level=logging.INFO):
    """고급 로깅 설정"""
    # 커스텀 포맷터 (필수 포맷 적용)
    class MudEngineFormatter(logging.Formatter):
        def format(self, record):
            # 시분초.밀리초 형식
            timestamp = self.formatTime(record, '%H:%M:%S')
            ms = int(record.created * 1000) % 1000
            time_with_ms = f"{timestamp}.{ms:03d}"

            # 파일명과 라인 번호
            filename = record.filename
            lineno = record.lineno
            location = f"[{filename}:{lineno}]"

            # 최종 포맷: {시분초.ms} {LEVEL} [{filename.py:line}] {logstring}
            return f"{time_with_ms} {record.levelname} {location} {record.getMessage()}"

    formatter = MudEngineFormatter()

    # 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # 커스텀 로테이팅 핸들러
    file_handler = CustomRotatingHandler(
        filename='logs/mud_engine.log',
        maxBytes=200 * 1024 * 1024,  # 200MB
        backupCount=30
    )
    file_handler.setFormatter(formatter)

    # 루트 로거 설정
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    return root_logger
```

## 로그 파일 네이밍 규칙

```python
# 파일명 형식: mud_engine-{YYYYMMDD}-{no}.log
# 예시:
# mud_engine-20241225-01.log  # 첫 번째 파일
# mud_engine-20241225-02.log  # 크기 초과로 로테이트
# mud_engine-20241226-01.log  # 날짜 변경으로 새 파일

def get_log_filename():
    """현재 로그 파일명 생성"""
    today = datetime.now().strftime('%Y%m%d')
    return f"mud_engine-{today}-01.log"
```

## 로그 압축 및 정리

```python
import gzip
import glob
import os
from datetime import datetime, timedelta

def compress_old_logs():
    """오래된 로그 파일 압축"""
    log_pattern = "logs/mud_engine-*.log"
    for log_file in glob.glob(log_pattern):
        # 현재 사용 중인 파일은 제외
        if not is_current_log_file(log_file):
            compress_log_file(log_file)

def compress_log_file(log_file):
    """개별 로그 파일 압축"""
    compressed_file = f"{log_file}.gz"
    with open(log_file, 'rb') as f_in:
        with gzip.open(compressed_file, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    os.remove(log_file)

def cleanup_old_compressed_logs(days_to_keep=30):
    """오래된 압축 로그 파일 정리"""
    cutoff_date = datetime.now() - timedelta(days=days_to_keep)
    pattern = "logs/mud_engine-*.log.gz"

    for compressed_log in glob.glob(pattern):
        file_date = extract_date_from_filename(compressed_log)
        if file_date and file_date < cutoff_date:
            os.remove(compressed_log)
```

## 주의사항

- **로그 레벨 일관성**: 같은 종류의 이벤트는 같은 레벨 사용
- **과도한 로깅 방지**: 성능에 영향을 주지 않도록 적절한 수준 유지
- **로그 파일 관리**:
  - 파일명 형식 준수: `mud_engine-{YYYYMMDD}-{no}.log`
  - 200MB 크기 제한
  - 날짜 변경 시 새 파일 생성
  - 로테이트된 파일은 gzip 압축
- **디스크 공간 관리**: 오래된 압축 로그 정기적 정리
- **출력 포맷 준수**: 반드시 `{시분초.ms} {LEVEL} [{filename.py:line}] {logstring}` 형식 사용
- **구조화된 로깅**: JSON 형태로 로그 출력 고려 (필요시)
