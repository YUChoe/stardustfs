# 지식 그래프(knowledge-graph MCP) 사용 가이드

커밋 내용을 `.kiro/knowledge-graph.json`에 도메인 엔티티별로 누적 기록한다. 아래는
실제 시행착오에서 얻은 안정적 사용법이다. 그대로 따르면 반복 오류를 피한다.

## 0. 도구는 지연 로드(deferred)다 — 먼저 스키마를 불러온다
knowledge-graph 도구는 기본 노출되지 않는다. 호출 전 `ToolSearch`로 스키마를 로드한다.
```
ToolSearch(query="select:mcp__knowledge-graph__add_observations,mcp__knowledge-graph__create_entities", max_results=5)
```
로드 전에 직접 호출하면 InputValidationError가 난다.

## 1. add_observations — observations 배열을 반드시 명시
가장 흔한 오류: `Invalid arguments: observations array is required`.
원인은 파라미터(`observations`)가 비어 전송되는 것이다(간헐 발생). 빈 호출로 재시도하지
말고, 아래 형태로 배열을 명시해 호출한다.
```jsonc
{
  "observations": [
    {
      "entityName": "NATTraversal",
      "contents": [
        "YYYY-MM-DD <commit_hash>: <무엇을 왜 바꿨는지 한 줄 이상>"
      ]
    }
  ]
}
```
- `entityName`은 기존 엔티티 이름과 정확히 일치해야 한다(없으면 2번으로 생성).
- `contents`는 문자열 배열. 각 항목은 독립된 관찰(observation)로 누적된다.
- 성공 시 응답은 `{}` 또는 추가된 항목 echo. 둘 다 정상이다.

## 2. create_entities — 맞는 엔티티가 없을 때 먼저 생성
도메인에 맞는 엔티티가 없으면 만든 뒤 관찰을 추가한다.
```jsonc
{
  "entities": [
    {"name": "GUI", "entityType": "component",
     "observations": ["최초 설명 + YYYY-MM-DD <hash>: <변경>"]}
  ]
}
```
create_entities의 `observations`로 최초 관찰을 함께 넣을 수 있다(별도 add 불필요).

## 3. 조회는 좁게 — 전체 덤프는 토큰 한계를 넘는다
`search_nodes`/`read_graph`는 결과가 커서(7만 자 이상) 토큰 한계를 넘어 파일로
저장되는 경우가 많다. 그러면 그래프를 못 읽는다. 다음을 따른다.
- 엔티티 이름만 알면 되는 경우: JSON을 직접 파싱한다(grep은 구조상 실패하기 쉽다).
```bash
python -c "import json; d=json.load(open('.kiro/knowledge-graph.json',encoding='utf-8')); \
print([e.get('name') for e in (d.get('entities') or [])])"
```
- 특정 엔티티의 관찰만 보려면 `open_nodes`(이름 지정)를 쓰고, `search_nodes`는
  좁은 질의로만 쓴다. 광범위 질의는 피한다.

## 4. 커밋 후 기록 절차(매 커밋)
1. 변경을 해당 도메인 엔티티에 add_observations로 기록한다.
2. 관찰 첫머리에 `YYYY-MM-DD <commit_hash>:`를 반드시 넣는다(타임스탬프 필수).
3. 프로젝트 루트 엔티티(StardustFS)에는 구조적 변경만 기록한다.
4. 맞는 엔티티가 없으면 create_entities로 먼저 만든다.

## 5. 도메인 → 엔티티 매핑(현재)
| 도메인 | 엔티티 |
| --- | --- |
| NAT 트래버설/홀펀칭/rudp/전송 캐스케이드 | `NATTraversal` |
| 스토리지 티어링/스필오버/축출/청크 전송 | `StorageTiering` |
| 복제(청크/패리티/홀더) | `Replication` |
| 디바이스 소스 레지스트리 | `DeviceSourceRegistry` |
| 데몬 라이프사이클/전송 위임 | `DaemonLifecycle` |
| GUI(Tkinter, 업로드 다이얼로그 등) | `GUI` |
| 구조적/프로젝트 전반 | `StardustFS` |
| 아키텍처/코드리뷰/Windows 테스트/venv | `StardustFS-Architecture` 외 |

새 도메인이면 이 표에 맞추지 말고 적절한 엔티티를 새로 만든 뒤 표를 갱신한다.

## 6. 체크리스트
- [ ] 호출 전 ToolSearch로 스키마 로드
- [ ] add_observations에 `observations` 배열 명시(빈 호출 금지)
- [ ] 관찰에 `YYYY-MM-DD <hash>:` 포함
- [ ] 엔티티 없으면 create_entities 먼저
- [ ] 조회는 좁게(전체 덤프 회피), 이름은 JSON 직접 파싱
