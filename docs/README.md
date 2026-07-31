# StardustFS 문서

StardustFS는 여러 디바이스의 스토리지를 하나의 가상 파일서버로 묶는 암호화 분산
파일시스템입니다. FTP 유사 CLI와 데스크톱 GUI로 업로드/다운로드하며, 파일은
클라이언트에서 4 MiB 청크로 나뉘어 청크별로 AES-256-GCM 암호화되고 메타데이터는 중앙
서버를 통해 동기화됩니다(zero-knowledge).

프로젝트 개요·CLI 사용법·빠른 시작은 루트 [README.md](../README.md)를 참조하세요.

## 사용자 문서

- [신규 디바이스 설치·설정·실행·동기화](./NEW_DEVICE_SETUP.md) — 새 PC를 같은 계정의
  디바이스로 추가하는 절차(키 백업 복원 포함), 부록에 첫 디바이스 최초 설정
- [설정 가이드](./CONFIGURATION.md) — v2 설정 파일 스키마·기본값·검증 규칙
- [파일 저장 위치 정책](./DISTRIBUTION_POLICY.md) — 조각(청크)이 어디에 저장되는지,
  스필오버·백업 카피·축출·읽기 경로

## 설계 문서

- [아키텍처](./ARCHITECTURE.md) — 현행 시스템 구조(daemon + CLI/GUI, 암호화 스토리지
  통합, 메타데이터 동기화, P2P/릴레이, 복제, 토큰 인증)
- [전송 계층](./TRANSPORT.md) — 직접 TCP → 홀펀칭 UDP → 서버 릴레이 캐스케이드,
  데몬 전송 위임, 청크 무결성 검증
- [로드맵](./ROADMAP.md) — 제품 방향과 진행 현황
- [API 레퍼런스](./API_REFERENCE.md) — `stardustlib/` 모듈별 공개 진입점
- [핸드오버 가이드](./HANDOVER.md) — 현재 상태·작업 규칙·인증(토큰) 모델

## 개발 도구

- [Kiro 명령 승인 설정](./KIRO_COMMAND_TRUST.md)
- [Tkinter GUI 검증(tkinter-mcp-server)](./TKINTER_MCP.md)

기능별 상세 스펙은 `.kiro/specs/<기능>/`의 requirements/design/tasks 문서를 참조하세요
(예: `chunk-native-storage`, `chunk-copy-policy`, `token-auth-transition`,
`holepunch-transport`, `gui-ux-cleanup`). 주제별 목록은 HANDOVER 11절에 있습니다.
