---
inclusion: manual
---

# 복제 소유 모델 정정 및 진행 가시성 — Requirements

파일 소유는 사용자(user) 단위인데 복제 경로 곳곳이 device를 소유자처럼 다루고 있다.
그 결과 (1) 물리 데이터를 갖고 있지 않은 device가 백업을 맡아 원본 전체를 릴레이로
당겨오고, (2) 배제 기준이 "실행 중인 기기"라 원본을 보관한 기기가 홀더로 뽑힐 수
있다. 또한 백업 진행이 로그·GUI 어디에도 드러나지 않아 멈춘 것과 구분되지 않는다.

관측된 사례(2026-07-26): 787 MB 파일 `/Blades…mp4`의 물리 데이터는 데스크톱
(556f1c07…)에만 있고 lg14는 동기화된 메타데이터만 갖고 있었다. lg14에서 백업을
요청하자 lg14가 `/p2p/read_chunk` 범위 분할 읽기로 787 MB 전체를 4 MiB씩 188회,
1.5초/회로 4분간 릴레이로 당겨왔다(이 구간 로그 0줄). 이어 홀더로 자기 자신을 골라
188 청크를 자기 ParityStore에 저장했다 — `exclude=[self_device_id]`가 비어 있었다.

근본 원인은 데이터를 갖지 않은 device가 백업을 맡는 구조다. 물리 블록이 전부 다른
기기에 있으면 그 기기에서 전량을 당겨와야 하고, 배제 기준이 실행 기기라 원본을 가진
기기가 홀더로 뽑힐 수도 있다.

## Requirement 1: 소유는 사용자, device는 보관 위치

사용자 스토리: 사용자로서, 내 파일은 어느 기기에 물리적으로 놓여 있든 내 것이고,
백업 여부가 기기 소속으로 갈리지 않기를 바란다.

### Acceptance Criteria
1. THE 백업 대상 선정 SHALL 파일의 `files.device_id`(레코드 관리 기기)를 소유
   판정에 사용하지 않는다.
2. WHEN device가 자동 백업 주기를 수행하면 THE daemon SHALL 그 device의 로컬
   스토리지에 실제로 청크가 있는 파일을 대상으로 삼는다.
3. WHEN 한 파일의 청크가 여러 device에 흩어져 있으면 THE 각 device SHALL 자기
   로컬 청크만 홀더에 올리고, 다른 device의 청크를 릴레이로 당겨오지 않는다.
4. THE 파일 단위 복제 상태(`replication_status`) SHALL 서버 레지스트리 기준으로
   판정한다 — 모든 청크가 `min_replicas` 이상이면 replicated, 아니면 pending.
5. IF 파일에 로컬 청크가 하나도 없으면 THE device SHALL 그 파일을 백업 대상에서
   제외하고 원격 읽기를 시도하지 않는다.

## Requirement 2: 원본 보관 device는 홀더 후보에서 제외

사용자 스토리: 사용자로서, 원본이 있는 바로 그 기기에 사본이 생겨 백업이 무의미해지는
일이 없기를 바란다.

### Acceptance Criteria
1. WHEN 청크를 홀더에 배치하면 THE ReplicationManager SHALL 그 청크의 원본을
   보관 중인 device를 placement `exclude`에 포함한다.
2. THE exclude SHALL 자기 device(`storage_pool.device_id`)와 보관 한도 초과로
   배제 중인 device를 계속 포함한다.
3. IF 제외 후 후보 홀더가 없으면 THE 결과 SHALL pending이며, 사유(후보 없음)를
   WARNING으로 1회 남긴다 — 조용한 성공 처리 금지.
4. WHEN heal(재복제)이 후보를 고를 때도 THE 동일한 제외 규칙 SHALL 적용된다.

## Requirement 3: 자기 device 식별 실패를 조용히 넘기지 않는다

사용자 스토리: 운영자로서, 배치 판단의 전제인 자기 device 식별이 실패했다면 그것을
알고 싶다 — 조용히 잘못된 배치가 일어나지 않게.

관측(2026-07-26): lg14의 백업 결과 188개 청크의 홀더가 lg14 자신(064f3bc5…)이었다.
`exclude=[self_device_id]`가 비어 있었다는 뜻이고, 단발 세션의 `_identify_self`가
(name, os) 매칭에 실패해 None을 돌려준 경우가 유력하다. 이번에는 원본이 다른
기기에 있어 결과적으로 유효한 복제가 됐지만, 원본이 로컬이었다면 같은 기기에
사본이 생겨 내구성 이득이 0이 된다.

### Acceptance Criteria
1. WHEN 단발 세션(CLI/GUI)이 자기 device_id를 확정하지 못하면 THE 세션 SHALL
   WARNING을 남긴다(어떤 name/os로 조회했는지 포함).
2. IF 자기 device_id가 없는 상태에서 복제를 수행하면 THE ReplicationManager SHALL
   Requirement 2의 원본 보관 device 제외에만 의존하고, 그 사실을 로그로 알린다.
3. THE `make_replication_manager` SHALL 서버 정책의 `min_replicas`를 전달한다
   (현재 기본값 1로 고정돼 정책이 반영되지 않는다).

## Requirement 4: 수동 백업 요청은 청크 보관 device가 수행

사용자 스토리: 사용자로서, 어느 기기의 GUI에서 백업을 눌러도 데이터가 불필요하게
네트워크를 왕복하지 않기를 바란다.

### Acceptance Criteria
1. WHEN 사용자가 GUI/CLI에서 파일 백업을 요청하면 THE 클라이언트 SHALL 그 파일의
   청크를 보관한 device 집합을 확인한다.
2. IF 청크 전부가 다른 device에 있으면 THE 클라이언트 SHALL 릴레이로 데이터를
   당겨오지 않고 그 device에 백업을 위임(announce)한다.
3. IF 위임 대상 device가 오프라인이면 THE 클라이언트 SHALL 위임 실패를 사용자에게
   알리고(상태바 메시지) 로컬 전송을 강행하지 않는다.
4. WHEN 청크 일부만 로컬에 있으면 THE 클라이언트 SHALL 로컬 청크는 직접 올리고
   나머지는 해당 device에 위임한다.

## Requirement 5: 진행 상태를 로그로 확인

사용자 스토리: 운영자로서, 대용량 백업이 진행 중인지 멈춘 것인지 로그로 구분하고 싶다.

### Acceptance Criteria
1. WHEN 청크 수가 20개 이상인 파일을 복제하면 THE ReplicationManager SHALL 진행
   로그를 10회로 나눠 남긴다(현재 구현됨).
2. WHEN 원격 device에서 청크를 읽어야 하면 THE 읽기 단계 SHALL 동일 간격으로
   진행 로그를 남긴다 — 읽기 구간이 로그 공백이 되지 않게.
3. WHEN 한 청크의 홀더 전송이 `DIRECT_HOLDER_TIMEOUT`(3초)과 릴레이 타임아웃
   (35초)을 모두 소진하면 THE 로그 SHALL 어느 홀더에서 얼마나 지연됐는지 남긴다.
4. THE 진행 로그 SHALL 청크 20개 미만 파일에서는 남기지 않는다(로그 오염 방지).

## Requirement 6: 진행 상태를 GUI에서 확인

사용자 스토리: 사용자로서, 백업을 요청한 뒤 GUI에서 진행률을 보고 싶다.

### Acceptance Criteria
1. THE daemon SHALL 현재 복제 진행 상태(대상 경로, 처리 청크 수, 전체 청크 수,
   확보 청크 수, 단계)를 메모리에 유지하고 제어 채널로 노출한다.
2. WHEN GUI가 3초 주기 폴링을 수행하면 THE GUI SHALL 진행 중인 백업이 있을 때
   상태바에 "백업 중: {파일명} {처리}/{전체} 청크"를 표시한다.
3. WHEN 진행 중인 백업이 없으면 THE 상태바 SHALL 기존 표시로 되돌아간다.
4. IF daemon이 실행 중이 아니거나 제어 채널 조회가 실패하면 THE GUI SHALL 진행
   표시를 생략하고 기존 동작을 유지한다(폴링 실패는 무시).
5. THE 제어 채널 응답 SHALL 파일 경로 외 사용자 데이터를 포함하지 않는다.

## 비기능 요구사항

- 제어 채널 폴링은 기존 `/ctl/*` 라우트와 같은 토큰 인증(`X-Ctl-Token`)을 쓴다.
- 진행 상태 갱신은 청크 단위로 하되 GUI 폴링 주기(3초)보다 잦은 파일 I/O를
  유발하지 않는다(메모리 보관, 파일 기록 없음).
- 기존 동작 회귀 금지: 청크가 모두 로컬인 일반 파일의 백업 경로는 그대로다.
