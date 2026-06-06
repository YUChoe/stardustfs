"""rudp 위 P2P op 송수신 (홀펀칭 직접 전송 Phase 4).

펀치로 연 UDP 경로 + rudp(신뢰성 메시지)를 이용해 기존 P2P op(read/write/list/space,
replica_store/fetch/delete)를 직접 전송한다. 한 노드가 서버(요청 처리)와 클라이언트
(요청 송신) 역할을 한 엔드포인트에서 동시에 수행한다.

메시지 포맷: 1바이트 태그(REQ/RESP) + JSON 본문.
- 요청(REQ): {"op": str, "payload": dict}
- 응답(RESP): {"status": int, "result": dict}
요청-응답은 rudp msg_id로 매칭한다(서버가 요청 id를 그대로 에코해 응답). 바이너리
(청크 등)는 기존 dispatch 규약대로 payload 안에서 base64 문자열로 운반한다.
"""

from __future__ import annotations

import asyncio
import json
import logging

from stardustlib.rudp import RudpEndpoint

logger = logging.getLogger(__name__)

_TAG_REQ = b"\x01"
_TAG_RESP = b"\x02"
_OP_TIMEOUT = 30.0  # 초 — 요청 전송 + 응답 대기 전체


class P2pUdpNode:
    """rudp 엔드포인트 위에서 P2P op를 주고받는 노드(서버+클라이언트 겸용).

    dispatch는 async (op, payload) -> (status, result). 보통 P2PServer.dispatch_async.
    """

    def __init__(self, endpoint: RudpEndpoint, dispatch) -> None:
        self._ep = endpoint
        self._dispatch = dispatch
        self._pending: dict[int, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._req_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._recv_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for t in list(self._req_tasks):
            t.cancel()
        self._req_tasks.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------
    # 클라이언트: 요청 송신
    # ------------------------------------------------------------------

    async def send_op(
        self, addr: tuple[str, int], op: str, payload: dict,
        *, timeout: float = _OP_TIMEOUT,
    ) -> tuple[int, dict]:
        """addr로 op 요청을 보내고 (status, result)를 받는다.

        rudp가 요청을 신뢰 전달(전량 ACK)한 뒤 응답 메시지를 기다린다. 응답 미수신 시
        TimeoutError(호출자가 릴레이 등으로 fallback).
        """
        msg_id = self._ep.new_msg_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        body = _TAG_REQ + json.dumps(
            {"op": op, "payload": payload}, ensure_ascii=False
        ).encode("utf-8")
        try:
            await asyncio.wait_for(
                self._ep.send(addr, body, msg_id=msg_id), timeout=timeout
            )
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)
        return resp["status"], resp.get("result", {})

    # ------------------------------------------------------------------
    # 수신 루프: 응답 라우팅 + 요청 처리
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        while True:
            try:
                addr, msg_id, data = await self._ep.recv()
            except asyncio.CancelledError:
                raise
            if not data:
                continue
            tag, body = data[:1], data[1:]
            if tag == _TAG_RESP:
                self._resolve_response(msg_id, body)
            elif tag == _TAG_REQ:
                # 요청 처리는 별도 태스크로(수신 루프 블로킹 방지).
                t = asyncio.create_task(self._handle_request(addr, msg_id, body))
                self._req_tasks.add(t)
                t.add_done_callback(self._req_tasks.discard)

    def _resolve_response(self, msg_id: int, body: bytes) -> None:
        fut = self._pending.get(msg_id)
        if fut is None or fut.done():
            return
        try:
            fut.set_result(json.loads(body.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as e:
            fut.set_exception(OSError(f"응답 디코드 실패: {e}"))

    async def _handle_request(
        self, addr: tuple[str, int], msg_id: int, body: bytes
    ) -> None:
        try:
            req = json.loads(body.decode("utf-8"))
            status, result = await self._dispatch(
                req.get("op", ""), req.get("payload", {})
            )
        except Exception as e:  # noqa: BLE001 — 처리 실패도 응답으로 전달
            logger.warning("P2P UDP 요청 처리 실패: %s", e)
            status, result = 500, {"error": "Internal error"}
        resp = _TAG_RESP + json.dumps(
            {"status": status, "result": result}, ensure_ascii=False
        ).encode("utf-8")
        try:
            # 요청 id를 그대로 에코해 클라이언트가 응답을 매칭하게 한다.
            await self._ep.send(addr, resp, msg_id=msg_id)
        except Exception as e:  # noqa: BLE001 — 응답 전달 실패(상대 사라짐 등)
            logger.info("P2P UDP 응답 전달 실패 id=%s: %s", msg_id, e)
