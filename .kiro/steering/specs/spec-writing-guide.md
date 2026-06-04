---
inclusion: auto
---

# 스펙 문서 작성 가이드

## 언어 규칙
- 스펙 문서(requirements.md, design.md, tasks.md)의 본문은 한국어로 작성한다
- 단, 스펙 형식이 요구하는 섹션 헤더는 영어를 유지한다:
  - `## Correctness Properties`
  - `### Property N: {한글 제목}`
  - `## Error Handling`
  - `## Testing Strategy`
  - `## Components and Interfaces`
  - `## Data Models`
- Property 설명문에서 "For any"는 "*임의의*"로 번역한다
- 코드 블록 내 주석은 한국어로 작성한다

## 형식 제약 (진단 통과 필수)
- design.md의 Correctness Properties 섹션 헤더는 반드시 `## Correctness Properties`로 작성 (한글 변환 금지)
- 각 속성 헤더는 반드시 `### Property N:` 형식 유지 (숫자 + 콜론 필수)
- requirements.md의 Acceptance Criteria는 EARS 패턴(WHEN/IF/WHILE/THE...SHALL) 사용

## 내용 규칙
- 타임아웃, 재시도 횟수, 범위 등 구체적 수치를 명시한다
- 에러 발생 시 동작(예외 타입, HTTP 상태 코드)을 명확히 기술한다
- 오프라인/실패 시나리오를 반드시 포함한다
- 마이그레이션이 필요한 경우 백업 + 롤백 전략을 명시한다
