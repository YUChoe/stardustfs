# SQLite 사용 규칙

## 기본 규칙
- `sqlite3` 가 설치되어 있지 않으니 python을 이용할 것
- python -c 를 이용한 실행 하지 말 것
- 스크립트를 scripts 디렉토리에 생성 한 후 실행 할 것
- 참조를 위해 언제나 최신의 스키마를 data/DATABASE_SCHEMA.md 에 업데이트 할 것
- 데이터 베이스 테이블 스키마는 절대로 절대로 추측하지 말고 data/DATABASE_SCHEMA.md 확인 후 사용 할 것.

## 사용 예제
```python
import sqlite3
conn = sqlite3.connect('data/mud_engine.db')
cursor = conn.cursor()
cursor.execute('SELECT username, is_admin FROM players WHERE username=?', ('pp',))
result = cursor.fetchone()
print(f'Player pp: {result}')
conn.close()
```
