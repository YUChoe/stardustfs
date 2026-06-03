# StardustFS

여러 디바이스의 스토리지를 하나의 가상 파일서버로 묶는 암호화 분산 파일시스템.
같은 계정의 여러 PC/Linux에 분산된 저장소를 단일 네임스페이스로 업로드/다운로드한다.

- 접근 계층: FTP 유사 CLI(`stardustfs <명령>`). 차후 GUI 파일탐색기.
- 보안: 파일은 클라이언트에서 AES-256-GCM으로 암호화. 서버는 암호문과 version만
  다룬다(zero-knowledge).
- 멀티디바이스: 메타데이터는 중앙 서버를 통해 동기화, 파일은 소유 디바이스에서
  P2P(직접/릴레이)로 전송.
- 인증: 토큰 기반. `login`으로 토큰을 자격증명 저장소에 보관하고 자동 갱신.

중앙 서버는 별도 저장소 [`../stardustfs-server`](../stardustfs-server)에 있다.

## 프로세스 구조

- `stardustfs.py daemon` — 상주 데몬. 이 디바이스를 온라인 피어로 유지(device 등록,
  P2P/릴레이, heartbeat, 메타데이터 동기화). `daemon status` / `daemon stop`.
- `stardustfs.py <명령>` — 단발 CLI. daemon과 메타데이터 DB를 공유한다.

## CLI 명령

```
stardustfs.py login   --config <cfg> [--email .. --password .. --key-password ..]
stardustfs.py logout  --config <cfg>
stardustfs.py ls      [경로]          # 가상 경로 목록
stardustfs.py df                       # 총/가용 용량
stardustfs.py status                   # 동기화 상태(보류 변경 수)
stardustfs.py devices                  # 내 device 목록 + online 여부
stardustfs.py put  <로컬> [원격]       # 업로드
stardustfs.py get  <원격> [로컬]       # 다운로드
stardustfs.py rm   <경로> [-r]
stardustfs.py mkdir <경로>
stardustfs.py mv   <원본> <대상>
stardustfs.py cp   <원본> <대상>
```

가상 경로는 선행 슬래시 없이 상대 경로(`ls foo`)로 써도 된다(Git Bash의 경로 변환
회피). `--config`는 서브커맨드 앞/뒤 모두 가능하다.

## 개발

```bash
# 가상환경 (Windows Git Bash)
source .venv/Scripts/activate

# daemon 개발 실행 (10MB 루프백 스토리지 3개)
./run-dev.sh

# 테스트
PYTHONPATH=. python -m pytest -q
```

- Python, 가상환경 `.venv`. 프로덕션 Python 3.9.
- 핵심 라이브러리: `stardustlib/`. 메타데이터: `stardustlib/metadata_store.py`
  (SQLCipher 가능 시 암호화, 아니면 sqlite3 폴백).
- 파일 LF + UTF-8, PEP8, 타입 힌트.

## 문서

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 현행 아키텍처
- [docs/ROADMAP.md](docs/ROADMAP.md) — 제품 방향/로드맵
- [docs/HANDOVER.md](docs/HANDOVER.md) — 핸드오버 가이드(상태·규칙)
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — 설정
- `.kiro/specs/` — 기능별 스펙(requirements/design/tasks)
