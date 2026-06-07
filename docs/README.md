# StardustFS 문서

StardustFS는 여러 디바이스의 스토리지를 하나의 가상 파일서버로 묶는 암호화 분산
파일시스템입니다. FTP 유사 CLI로 업로드/다운로드하며, 파일은 클라이언트에서
AES-256-GCM으로 암호화되고 메타데이터는 중앙 서버를 통해 동기화됩니다(zero-knowledge).

프로젝트 개요·CLI 사용법·빠른 시작은 루트 [README.md](../README.md)를 참조하세요.

## 문서 목록

- [아키텍처](./ARCHITECTURE.md) — 현행 시스템 구조(daemon+CLI, 암호화 스토리지 통합, 메타데이터
  동기화, P2P/릴레이, 토큰 인증)
- [로드맵](./ROADMAP.md) — 제품 방향(가상 파일서버 피벗, 후속 단계)
- [핸드오버 가이드](./HANDOVER.md) — 현재 상태·작업 규칙·인증(토큰) 모델
- [설정 가이드](./CONFIGURATION.md) — 설정 파일(`dev-config.json`) 작성 방법
- [API 레퍼런스](./API_REFERENCE.md) — 모듈별 함수 설명
- [코드 리뷰](./CODE_REVIEW.md) — 코드 품질 분석

기능별 상세 스펙은 `.kiro/specs/<기능>/`의 requirements/design/tasks 문서를 참조하세요
(예: `cli-virtual-fileserver`, `token-auth-transition`, `replication-parity`).
