# StardustFS

여러 디바이스의 스토리지를 하나의 가상 파일서버로 묶는 암호화 분산 파일시스템.
같은 계정의 여러 PC/Linux에 분산된 저장소를 단일 네임스페이스로 업로드/다운로드한다.

- 접근 계층: FTP 유사 CLI(`stardustfs <명령>`)와 데스크톱 GUI 파일탐색기.
- 보안: 파일은 클라이언트에서 4 MiB 암호문 청크로 나뉘어 청크별로 AES-256-GCM 암호화된다.
  서버는 암호문과 version만 다룬다(zero-knowledge).
- 멀티디바이스: 메타데이터는 중앙 서버를 통해 동기화(변경 레코드만 증분 전송), 파일은
  청크를 보관한 디바이스에서 P2P(직접/릴레이)로 전송. 필요한 범위만 부분 읽기 가능.
- 인증: 토큰 기반. `login`으로 토큰을 자격증명 저장소에 보관하고 자동 갱신.

중앙 서버는 별도 저장소 [`../stardustfs-server`](../stardustfs-server)에 있다.

## 프로세스 구조

- `stardustfs.py daemon` — 상주 데몬. 이 디바이스를 온라인 피어로 유지(device 등록,
  P2P/홀펀칭/릴레이, heartbeat, 메타데이터 동기화, 복제 스케줄러). `daemon status` /
  `daemon stop`.
- `stardustfs.py <명령>` — 단발 CLI. daemon과 메타데이터 DB를 공유하고, 전송(put/get)은
  daemon 제어 채널에 위임한다(없으면 직접 수행).
- `stardustfs.py gui` — 데스크톱 GUI. daemon 생존을 감시해 죽어 있으면 자동 재시작한다.

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
stardustfs.py backup  <경로>           # 암호화 복제(목표 카피 수 확보)
stardustfs.py restore <경로>           # 복제본에서 복구
stardustfs.py heal    <경로>           # 부족한 복제본 보충
```

조회 계열(`ls`/`df`/`status`/`devices`)은 `--json`으로 기계 판독 출력을 낼 수 있다.

가상 경로는 선행 슬래시 없이 상대 경로(`ls foo`)로 써도 된다(Git Bash의 경로 변환
회피). `--config`는 서브커맨드 앞/뒤 모두 가능하다.

## GUI (데스크톱)

```
python stardustfs.py gui [--config <cfg>]
```

Tkinter 기반 파일 탐색기(목록/업로드/다운로드/폴더/삭제/이동/복사 + 스토리지·디바이스
관리 + 백업/복구 + daemon 상태·제어 + 로그인/로그아웃). 데스크톱 세션이 필요하다.
`--config` 미지정 시 GUI에서 설정 파일을 고르거나 새로 만든다(`파일 → 새 설정...`).
테마는 `sv-ttk`, 트레이는 `pystray`/`Pillow`가 있으면 쓰고 없으면 폴백한다.
인자 없이 실행(exe 더블클릭)하면 GUI가 열린다.

## 설치

- Windows 배포본: GitHub Releases의 `stardustfs-windows-x64.zip`(PyInstaller onedir).
  압축을 풀고 `stardustfs.exe` 실행. Python 설치가 필요 없다.
- 소스 실행: 아래 개발 섹션 참조.

새 PC를 같은 계정에 추가하는 절차는
[docs/NEW_DEVICE_SETUP.md](docs/NEW_DEVICE_SETUP.md)에 있다.

## 개발

```bash
# 가상환경 (Windows Git Bash)
source .venv/Scripts/activate
pip install -r requirements.txt

# daemon 개발 실행 (dev-config.json)
./run-dev.sh

# GUI 개발 실행
./run.sh

# 테스트 (약 2.5분)
PYTHONPATH=. python -m pytest -q
```

- Python 3.10 이상, 가상환경 `.venv`. 개발 환경 3.10, Windows 배포 빌드(CI) 3.13.
- 핵심 라이브러리: `stardustlib/`. 메타데이터: `stardustlib/metadata_store.py`
  (SQLCipher 가능 시 암호화, 아니면 sqlite3 폴백).
- 파일 LF + UTF-8, PEP8, 타입 힌트.

## 문서

- [docs/](docs/) — 문서 목차
- [docs/NEW_DEVICE_SETUP.md](docs/NEW_DEVICE_SETUP.md) — 신규 디바이스 설치·설정·실행·동기화 절차
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — 설정 스키마·기본값
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 현행 아키텍처
- [docs/DISTRIBUTION_POLICY.md](docs/DISTRIBUTION_POLICY.md) — 파일 저장 위치 정책
- [docs/TRANSPORT.md](docs/TRANSPORT.md) — 전송 캐스케이드
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) — 모듈별 공개 API
- [docs/ROADMAP.md](docs/ROADMAP.md) — 제품 방향/로드맵
- [docs/HANDOVER.md](docs/HANDOVER.md) — 핸드오버 가이드(상태·규칙)
- `.kiro/specs/` — 기능별 스펙(requirements/design/tasks)
