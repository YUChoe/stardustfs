# Design: P2P 릴레이 Fallback (long-polling)

## Overview

중앙 서버에 메모리 기반 릴레이 큐를 두어, 직접 P2P 연결이 불가능한 두 디바이스 간에
P2P 작업(read/write/list/exists/...)을 중계한다. 모든 트래픽이 outbound HTTP이므로
NAT/CGNAT를 통과한다. 서버는 payload를 불투명 blob으로 중계하며 영속화하지 않는다.

```
요청자(PC-A)                  중앙 서버                    대상(PC-B)
    |  POST /relay/request        |                            |
    |---------------------------->| 큐 적재, request_id 발급    |
    |  GET /relay/response/{id}   |        GET /relay/poll      |
    |----(long-poll 대기)-------->|<----(long-poll 대기)--------|
    |                             |---- (request_id,op,payload)>|
    |                             |                   (로컬 처리)|
    |                             |   POST /relay/response/{id} |
    |                             |<----------------------------|
    |<----(응답 전달)-------------|                            |
```

## 서버 컴포넌트

### RelayHub (in-memory, app.state)

대상 디바이스별 요청 큐와 요청별 응답 future를 관리한다. 단일 uvicorn 워커 가정
(MVP 데모 범위). 다중 워커 확장은 향후 과제(Redis 등 외부 큐 필요).

```python
class RelayHub:
    # device_id -> asyncio.Queue[RelayMessage]   (대상에게 전달할 요청)
    _inbox: dict[str, asyncio.Queue]
    # request_id -> asyncio.Future[dict]          (요청자가 기다리는 응답)
    _pending: dict[str, asyncio.Future]

    async def submit(device_id, message) -> None      # 요청 적재
    async def poll(device_id, timeout) -> RelayMessage | None
    async def deliver(request_id, response) -> None    # 응답 전달
    async def wait_response(request_id, timeout) -> dict
```

- `submit`: target inbox 큐에 요청을 넣고, `_pending[request_id]`에 Future 생성
- `poll`: 대상이 자기 inbox에서 요청을 꺼냄(`asyncio.wait_for`로 타임아웃)
- `deliver`: `_pending[request_id]` Future에 결과 set
- `wait_response`: 요청자가 Future를 await(타임아웃 시 504)

### 엔드포인트 (app/routers/relay.py)

모두 JWT 인증(get_current_user). prefix `/relay`.

| 메서드 | 경로 | 호출자 | 설명 |
|--------|------|--------|------|
| POST | `/relay/request` | 요청자 | (target_device_id, op, payload) 적재 → request_id 반환 |
| GET | `/relay/response/{request_id}` | 요청자 | 응답 long-poll (타임아웃 시 504) |
| GET | `/relay/poll` | 대상 | 자신 앞 요청 long-poll (없으면 204) |
| POST | `/relay/response/{request_id}` | 대상 | 처리 결과 업로드 |

#### 인가 규칙

- `/relay/request`: target_device_id의 소유자 user_id == 요청자 user_id 확인.
  불일치 시 403. 디바이스 없으면 404.
- `/relay/poll`: 폴링하는 디바이스가 본인 user_id 소유인지 확인(토큰 기반).
  서버는 그 user_id의 디바이스들에게 온 요청만 전달.
- `/relay/response/{request_id}`: 응답 올리는 user_id가 요청의 target user_id와
  일치하는지 확인.

#### 데이터 모델 (schemas)

```python
class RelayRequestBody(BaseModel):
    target_device_id: str
    op: str               # "read" | "exists" | "list" | "write" | ...
    payload: dict         # 불투명 — 서버는 해석하지 않음

class RelayRequestAccepted(BaseModel):
    request_id: str

class RelayPolled(BaseModel):
    request_id: str
    op: str
    payload: dict
    requester_device_id: str | None = None

class RelayResponseBody(BaseModel):
    status: int           # 핸들러가 낸 HTTP 상태 등가 (200/404/400/...)
    result: dict          # 불투명 결과(예: {"data": "<b64>"} 또는 {"error": ...})
```

#### 타임아웃

- poll long-poll: 25초 (프록시 60초 한계 이내). 빈 결과 시 클라이언트 즉시 재폴.
- response long-poll: 30초. 초과 시 504.

## 클라이언트 컴포넌트

### RelayClient (stardustlib/relay_client.py)

요청자 측에서 P2P op를 릴레이로 보낸다.

```python
class RelayClient:
    def __init__(auth_client, server_url, target_device_id, timeout)
    def request(op: str, payload: dict) -> dict
        # 1. POST /relay/request {target_device_id, op, payload} -> request_id
        # 2. GET /relay/response/{request_id} (long-poll)
        # 3. status != 200 이면 OSError, 200이면 result 반환
```

`_EventLoopThread`(기존 RemoteSource와 공유)로 동기 인터페이스 제공.

### RemoteSource 직접연결 → 릴레이 Fallback

`_do_p2p_request`(또는 그 상위 `_p2p_request`)에서 직접 연결을 시도하고,
`httpx.ConnectError`/`httpx.TimeoutException` 발생 시 릴레이로 전환한다.

```python
async def _do_p2p_request(endpoint, payload, retry=True):
    try:
        # 기존: 직접 연결 POST http://{peer_address}{endpoint}
        ...
    except (ConnectError, NetworkError, TimeoutException):
        # 릴레이 fallback: op = endpoint에서 추출("/p2p/read" -> "read")
        return await self._relay_request(op, payload)
```

- op 매핑: `/p2p/read`→`read`, `/p2p/exists`→`exists`, `/p2p/list`→`list`,
  `/p2p/write`→`write`, `/p2p/delete`→`delete`, `/p2p/mkdir`→`mkdir`,
  `/p2p/rmdir`→`rmdir`, `/p2p/space`→`space`
- 릴레이 응답 result는 직접 연결 응답 body와 동일 형식(예: `{"data": ...}`)이므로
  상위 read_from_source 등은 변경 없이 동작.
- 릴레이도 실패하면 OSError.

### RelayWorker (대상 측, stardustlib/relay_worker.py)

대상 디바이스의 백그라운드 워커. `/relay/poll`을 반복 호출하여 요청을 받고, 기존
P2P 핸들러 로직을 재사용해 처리 후 `/relay/response`로 결과를 올린다.

```python
class RelayWorker:
    def __init__(p2p_server, auth_client, server_url, device_id)
    async def start()   # asyncio.Task로 _loop 실행
    async def stop()
    async def _loop():
        while running:
            polled = GET /relay/poll (long-poll 25s)
            if polled is None: continue
            status, result = await p2p_server.dispatch(op, payload)
            POST /relay/response/{request_id} {status, result}
```

`P2PServer`에 핸들러 디스패치 메서드를 추가한다. 기존 핸들러는 aiohttp Request에
묶여 있으므로, 순수 로직을 분리한다.

```python
class P2PServer:
    async def dispatch(op: str, payload: dict) -> tuple[int, dict]:
        # op별로 _op_read/_op_write/... 호출, (status, result) 반환
        # 기존 handle_* 는 _op_* 를 호출하도록 리팩토링(중복 제거)
```

리팩토링 방향: `handle_read`의 검증·읽기 로직을 `_op_read(payload) -> (status,
result)`로 추출하고, `handle_read`는 인증 후 `_op_read`를 호출해 web.Response로
감싼다. RelayWorker는 인증을 거치지 않고(이미 서버가 user 검증함) `_op_*`를 직접
호출한다. 단, 릴레이 경로의 인가는 서버가 user_id 일치로 보장한다.

## 시퀀스: PC-A가 PC-B 파일 read (직접연결 실패 → 릴레이)

1. PC-A: `read_file(/3333333.txt)` → device_id=PC-B → `_read_remote`
2. RemoteSource.read_from_source → `_p2p_request("/p2p/read", payload)`
3. 직접 연결 시도 → TimeoutException
4. fallback: RelayClient.request("read", payload)
   - POST /relay/request {target=PC-B, op=read, payload} → request_id
   - GET /relay/response/{request_id} (대기)
5. PC-B RelayWorker: GET /relay/poll → (request_id, read, payload)
6. PC-B: p2p_server.dispatch("read", payload) → (200, {"data": "<b64 암호문>"})
7. PC-B: POST /relay/response/{request_id} {status:200, result:{data}}
8. 서버: 요청자 Future에 전달 → PC-A의 GET 응답
9. PC-A: result["data"] base64 디코딩 → 로컬 master_key로 복호화 → 반환

## 설계 결정

- 메모리 큐 + 단일 워커: 데모/검증 범위. 영속화 안 함(요청은 휘발성). 다중 워커
  확장은 외부 큐 필요 — 범위 밖.
- 직접연결 우선: LAN·포트포워딩 환경에서 릴레이 오버헤드 회피. 릴레이는 fallback.
- 서버 무지(zero-knowledge): payload·result는 불투명. 파일 데이터는 이미 암호문.
- 인가: 같은 user_id 디바이스 간만. 교차 사용자 릴레이 불가(MVP5 평문 공유 폐기
  방침과 일치).
- op 추가 없이 기존 P2P 작업 집합 재사용. 릴레이는 전송 계층만 교체.

## 정확성 속성 (PBT 후보)

- Property 1 (왕복 동일성): 직접연결로 읽은 바이트 == 릴레이로 읽은 바이트.
- Property 2 (인가): user_id 다른 디바이스 쌍은 항상 403, 같으면 통과.
- Property 3 (불투명성): 임의 payload가 변형 없이 대상에 전달되고, result가 변형
  없이 요청자에게 전달된다.
