# Design: 메타데이터 버전 롱폴링 이벤트

## Overview

서버에 user_id별 메타데이터 version 변경 알림(in-memory)을 두고, 롱폴링
엔드포인트 `GET /sync/metadata/wait`로 클라이언트가 변경을 즉시 감지하게 한다.
클라이언트는 버전 대기 루프로 변경 즉시 다운로드·병합한다. 기존 주기 폴링은
안전망으로 유지한다. zero-knowledge 유지(version 정수만 다룸).

## 서버 컴포넌트

### VersionNotifier (app/services/version_notifier.py)

user_id별 asyncio 동기화 객체로 version 증가를 통지한다. 단일 uvicorn 워커 가정
(릴레이 RelayHub와 동일 전제).

```python
class VersionNotifier:
    _events: dict[str, asyncio.Event]   # user_id -> Event

    def _event(user_id) -> asyncio.Event   # 없으면 생성
    def notify(user_id) -> None            # set 후 즉시 clear(엣지 트리거)
    async def wait(user_id, timeout) -> bool  # Event.wait를 timeout으로 감쌈
```

- `notify`: 대기 중인 모든 wait를 깨운다. set() 직후 새 Event로 교체(또는
  set→clear)하여 다음 사이클을 위한 엣지 트리거로 동작.
- app.state.version_notifier에 단일 인스턴스 보관(relay_hub와 동일 패턴).

구현 주의: `asyncio.Event`는 set 후 clear해야 재사용 가능. 깨우기와 재무장 사이의
경합을 피하려면, wait 측은 "깨어난 뒤 서버 version을 다시 조회"하여 known_version과
비교한다(이벤트는 신호일 뿐, 실제 판정은 version 비교). 이로써 set/clear 경합이
있어도 정확성이 보장된다(놓친 알림은 다음 짧은 폴 또는 타임아웃으로 보정).

### 엔드포인트: GET /sync/metadata/wait

```python
@router.get("/metadata/wait")
async def wait_metadata_version(known_version: int = 0, ...):
    notifier = app.state.version_notifier
    deadline = now + WAIT_TIMEOUT       # 예: 25초
    while now < deadline:
        current = await service.get_current_version(user_id)  # 없으면 0
        if current > known_version:
            return {"version": current, "changed": True}
        remaining = deadline - now
        await notifier.wait(user_id, timeout=min(remaining, TICK))
    # 타임아웃: 변경 없음
    return {"version": current, "changed": False}
```

- 인증(get_current_user). known_version 쿼리 파라미터(기본 0).
- WAIT_TIMEOUT 25초(프록시 60초 이내). 내부 TICK(예: 5초)로 깨어나 version 재확인
  → 알림 누락에도 최대 TICK 지연으로 보정(견고성).
- get_current_version: metadata_backups의 MAX(version), 없으면 0.

### upload_metadata 통지

`SyncService.upload_metadata`가 version 증가에 성공하면(예외 없이 반환) 라우터
계층에서 `notifier.notify(user_id)`를 호출한다. CAS 충돌(409)이면 호출하지 않는다.

라우터에서 호출하는 이유: notifier는 app.state에 있고 service는 db만 알기 때문.
upload_metadata 라우터가 정상 version 반환 후 notify.

## 클라이언트 컴포넌트

### 버전 대기 루프 (SyncClient)

```python
async def _version_wait_loop(self):
    self._wait_enabled = True
    while self._running and self._wait_enabled:
        try:
            changed, server_version = await self._wait_for_version(
                self._last_synced_version
            )
        except WaitUnsupported:        # 404 → 비활성화
            self._wait_enabled = False
            return
        except (Timeout, ConnectError):
            await asyncio.sleep(RETRY)
            continue
        if changed:
            await self._download_and_merge()
            self._run_orphan_gc()
        # changed=False(타임아웃)면 즉시 재대기
```

- `_wait_for_version(known)`: `GET /sync/metadata/wait?known_version=known`,
  응답 {version, changed}. 404면 WaitUnsupported.
- start_periodic_sync 시 `_version_wait_loop`도 task로 시작.
- stop() 시 함께 취소.
- 다운로드·병합은 기존 `_download_and_merge` 재사용(멱등). 주기 루프와 동시
  실행돼도 version 비교 병합이라 안전.

### known_version 일관성

클라이언트는 `_last_synced_version`을 known_version으로 사용한다. download/merge
후 이 값이 갱신되므로, 다음 wait가 정확한 기준으로 대기한다. 주기 루프와 wait
루프가 같은 `_last_synced_version`을 공유하되, 갱신은 _download_and_merge 내에서
일어나므로 경합은 "둘 다 다운로드 시도" 정도이며 결과는 동일(멱등).

## 시퀀스: PC-A 업로드 → PC-B 즉시 반영

1. PC-B: GET /sync/metadata/wait?known_version=24 (대기)
2. PC-A: PUT /sync/metadata (version 24→25) 성공
3. 서버 라우터: notifier.notify(user_id) → PC-B wait 깨어남
4. PC-B wait: 서버 version=25 > 24 → {version:25, changed:true} 반환
5. PC-B: _download_and_merge → 병합, _last_synced_version=25
6. PC-B: 다시 wait?known_version=25 (대기)

지연: 폴링 30초 → 거의 즉시(네트워크 왕복 + notify 지연).

## 설계 결정

- 단일 워커 + 메모리 알림(RelayHub와 동일 전제). 다중 워커는 외부 pub/sub 필요(범위 밖).
- 이벤트는 신호일 뿐, 정확성은 version 비교로 보장(알림 누락에 견고). 내부 TICK으로
  알림 누락 시에도 최대 TICK 지연 보정.
- zero-knowledge 유지: 서버는 version 정수만 다룸. 내용은 불투명 blob.
- 기존 주기 폴링 유지(안전망). 롱폴 미지원(404) 서버에서 자동 비활성화(하위 호환).
- 전송량 최적화(파셜)는 별도 범위. 본 변경은 "변경 통지 지연" 해결만.

## 정확성 속성 (테스트 후보)

- Property 1: version > known이면 wait가 즉시 반환한다.
- Property 2: notify가 대기 중 롱폴러를 깨운다(타임아웃 전 반환).
- Property 3: 타임아웃 시 changed=false, version은 현재값.
- Property 4: 404 시 클라이언트가 wait 루프를 비활성화하고 주기 폴링만 사용.
