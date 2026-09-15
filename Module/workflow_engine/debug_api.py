"""WorkflowDebugAPI — WorkflowEngine 调试数据收集 + SSE 实时推送。

**按需**轮询 WorkflowEngine 内部状态（懒启动——首个 SSE 订阅者到达才收集，
最后一个离开即停止），diff 后通过 SSE 推送给前端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from Module._common.task_scope import AsyncTaskScope, cancel_and_drain

if TYPE_CHECKING:
    from Module.workflow_engine.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30.0      # 30s 无事件则发心跳（SSE 业务事件）
_SSE_KEEPALIVE_INTERVAL = 10.0  # 10s 无事件则写一行注释（断连探测，非业务事件）
_POLL_INTERVAL = 0.5            # 500ms 内部轮询
_SENTINEL: dict = {}            # shutdown 信号，替代 None（Queue[dict] 不接受 None）


def _force_put(q: "asyncio.Queue[dict]", item: dict) -> None:
    """**绝不阻塞**地把 `item` 放进队列：满了先丢弃排队事件（S3-05）。

    关停语义下排队的事件已无意义，而 sentinel 必须送达才能让 SSE handler 退出。
    无 `await` 间隔，故"腾空后仍满"只在有并发生产者的极端情形下可能——那时记一条
    warning 而不是把关停卡住。
    """
    try:
        q.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover —— 无 await 间隔，理论不可达
            break
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:       # pragma: no cover —— 同上
        logger.warning("WorkflowDebugAPI: sentinel 投递失败（队列仍满）")


class WorkflowDebugAPI:
    """收集 WorkflowEngine 运行时状态，diff 后 SSE 推送给前端。

    使用方式：
        api = WorkflowDebugAPI(task_system)
        await api.start()

        app.router.add_get('/debug/api/tasks', api.snapshot_handler)
        app.router.add_get('/debug/api/tasks/stream', api.sse_handler)

    生命周期（**轮询懒启动**，2026-09-12 起）：
        start()       → 就绪；**不**启动轮询（见下）
        subscribe()   → 首个订阅者到达时才启动 _poll_loop
        unsubscribe() → 最后一个订阅者离开时停止 _poll_loop 并复位快照基线
        stop()        → 停止轮询 + 通知订阅者断开；之后 subscribe() 不再拉起轮询

    为什么懒启动：``_poll_loop`` 每 500ms 遍历全部 task × node_states、抽 graph
    edges 并做整表 diff，**没有任何消费者时是纯开销**。此前 ``start()`` 在
    aiohttp on_startup 无条件拉起它，于是后端每次开机就开始 2Hz 空转——哪怕
    `/debug` 因为缺构建产物而 404、根本没人能打开这个面板。
    """

    def __init__(self, task_system: WorkflowEngine) -> None:
        """初始化 debug API。

        Args:
            task_system: 用于收集 debug 数据的 WorkflowEngine 实例。
        """
        self._ts = task_system
        self._subscribers: dict[str, asyncio.Queue[dict]] = {}
        self._last_snapshot: list[dict] | None = None
        self._last_task_ids: set[str] = set()
        self._last_event_time: float = time.monotonic()
        self._poll_task: asyncio.Task[Any] | None = None
        self._scope = AsyncTaskScope("workflow-debug-api")
        self._started = False

    # ── 生命周期 ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """标记调试 API 就绪（幂等）。

        **刻意不启动轮询**：没有订阅者时轮询只产生开销、不产生数据。轮询由
        :meth:`subscribe` 按需拉起。
        """
        self._started = True
        logger.info(
            "WorkflowDebugAPI: 就绪（轮询懒启动：有订阅者才收集，interval=%ss）",
            _POLL_INTERVAL)

    async def stop(self) -> None:
        """停止轮询循环并通知所有订阅者断开（幂等）。

        **本方法不得阻塞**（S3-05）：原实现是 `await q.put(_SENTINEL)`，队列（maxsize=256）
        被慢/半开 SSE 客户端打满时会**永久挂起**——而调用方正是关停路径
        （`core/input/debug_routes/__init__.py::_on_shutdown` → 这里），
        即"一个不读数据的调试面板能把关停卡死"。广播路径本来就把满队列当作可丢弃
        （`put_nowait` + `QueueFull` 跳过），停止路径同理：**腾空后再放 sentinel**
        ——关停时排队的事件已无意义，而 sentinel 必须送达，否则 handler 不会退出。
        """
        self._started = False
        await self._stop_polling()
        # 通知所有订阅者关闭连接
        for q in self._subscribers.values():
            _force_put(q, _SENTINEL)
        self._subscribers.clear()
        logger.info("WorkflowDebugAPI: 已停止")

    def _ensure_polling(self) -> None:
        """确保轮询循环在跑（幂等）。未 ``start()`` 时不做任何事。"""
        if not self._started:
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = self._scope.spawn(
            self._poll_loop(), name="workflow-debug-poll")
        logger.info(
            "WorkflowDebugAPI: 轮询已启动 (interval=%ss, subscribers=%d)",
            _POLL_INTERVAL, len(self._subscribers))

    async def _stop_polling(self) -> None:
        """停止轮询循环并复位快照基线（幂等）。"""
        task, self._poll_task = self._poll_task, None
        await cancel_and_drain(task, label="workflow-debug-poll")
        if task is not None:
            # 显式记一行——懒启动的另一半是懒停止，不落日志就无法验证它真的停了
            logger.info(
                "WorkflowDebugAPI: 轮询已停止（无订阅者，interval=%ss）", _POLL_INTERVAL)
        # 复位基线：下一个订阅者应拿到全新全量快照，而不是与「停止前那一刻」做
        # diff——否则无人观察期间消失的 task 会被补发一串 task_done。
        self._last_snapshot = None
        self._last_task_ids = set()
        self._last_event_time = time.monotonic()

    # ── 数据收集 ───────────────────────────────────────────────────────

    def _collect_snapshot(self) -> list[dict]:
        """从 WorkflowEngine._get_debug_snapshot() 获取原始数据，裁剪后返回。

        裁剪策略：
        - node_states 只保留 {node_id, status, attempt, summary, error}
        - 不序列化 evaluation（Planner replan 内部用，前端不需要）

        Returns:
            list[dict]: 裁剪后的任务快照列表，可直接序列化为 JSON。
        """
        raw = self._ts._get_debug_snapshot()
        tasks = []
        for t in raw["tasks"]:
            ts = t["task_state"]
            exec_state = t["exec_state"]

            nodes = []
            if exec_state:
                for nid, ns in exec_state.node_states.items():
                    nodes.append({
                        "node_id": nid,
                        "status": ns.status.value if hasattr(ns.status, 'value') else str(ns.status),
                        "attempt": ns.attempt,
                        "summary": ns.summary if ns.summary else "",
                        "error": ns.error if ns.error else "",
                    })

            # edges: 从 graph 提取
            edges = []
            graph = t.get("graph")
            if graph is not None:
                try:
                    raw_edges = list(graph._graph.edges)
                    edges = [[str(e[0]), str(e[1])] for e in raw_edges]
                except Exception:
                    pass

            # phase: 从 exec_state 提取
            phase = ""
            if exec_state and hasattr(exec_state, 'current_phase'):
                phase = (exec_state.current_phase.value
                         if hasattr(exec_state.current_phase, 'value')
                         else str(exec_state.current_phase))

            tasks.append({
                "task_id": str(ts.task_id),
                "status": ts.status.value if hasattr(ts.status, 'value') else str(ts.status),
                "plan_version": ts.plan.plan_version if ts.plan else 0,
                "created_at": ts.created_at,
                "error": ts.error if ts.error else "",
                "error_code": ts.error_code.value if ts.error_code else "",
                "nodes": nodes,
                "edges": edges,
                "mailbox_depth": t["mailbox_depth"],
                "phase": phase,
            })
        return tasks

    # ── Diff ───────────────────────────────────────────────────────────

    def _diff(self, old: list[dict] | None, new: list[dict]) -> list[dict]:
        """比较两次快照，产出 SSE 事件列表。

        Args:
            old: 上一次快照，首次轮询时为 None。
            new: 当前快照。

        Returns:
            list[dict]: SSE 事件列表，每项包含 event 和 data。
        """
        if old is None:
            # 基线未就位——理论不可达：subscribe() 先落基线再拉起轮询。留作防御，
            # 顺带避免首轮把「全部现存任务」误报成 task_new。
            return []

        events: list[dict] = []
        old_by_id = {t["task_id"]: t for t in old}
        new_by_id = {t["task_id"]: t for t in new}

        # 新增任务
        for tid in new_by_id:
            if tid not in old_by_id:
                events.append({"event": "task_new", "data": new_by_id[tid]})

        # 状态/进度变更
        for tid in new_by_id:
            if tid in old_by_id:
                nt = new_by_id[tid]
                ot = old_by_id[tid]
                if nt["status"] != ot["status"]:
                    events.append({"event": "task_update", "data": {
                        "task_id": tid,
                        "status": nt["status"],
                        "nodes": nt["nodes"],
                    }})
                    # 终态
                    if nt["status"] in ("completed", "failed", "cancelled"):
                        events.append({"event": "task_done", "data": {
                            "task_id": tid,
                            "status": nt["status"],
                            "error": nt["error"],
                        }})
                # 节点状态变更
                elif nt["nodes"] != ot["nodes"]:
                    old_nodes = {n["node_id"]: n for n in ot["nodes"]}
                    for nn in nt["nodes"]:
                        on = old_nodes.get(nn["node_id"])
                        if on and nn["status"] != on["status"]:
                            events.append({"event": "node_update", "data": {
                                "task_id": tid,
                                "node_id": nn["node_id"],
                                "status": nn["status"],
                                "attempt": nn["attempt"],
                            }})

        # 消失的任务——补发 task_done
        current_ids = set(new_by_id.keys())
        removed_ids = self._last_task_ids - current_ids
        for tid in removed_ids:
            ot = old_by_id.get(tid)
            if ot and ot["status"] not in ("completed", "failed", "cancelled"):
                events.append({"event": "task_done", "data": {
                    "task_id": tid,
                    "status": ot["status"],
                    "error": ot["error"],
                }})

        self._last_task_ids = current_ids
        return events

    # ── 轮询循环 ───────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """后台轮询循环：500ms 间隔收集快照，diff 后 broadcast 给订阅者。

        只在有订阅者期间存活（见类 docstring「生命周期」）——订阅者归零时由
        :meth:`unsubscribe` 取消本协程。
        """
        while True:
            try:
                snapshot = self._collect_snapshot()
                events = self._diff(self._last_snapshot, snapshot)
                self._last_snapshot = snapshot

                now = time.monotonic()
                if events:
                    self._last_event_time = now
                    for event in events:
                        await self._broadcast(event)
                elif now - self._last_event_time >= _HEARTBEAT_INTERVAL:
                    await self._broadcast({"event": "heartbeat", "data": {}})
                    self._last_event_time = now

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WorkflowDebugAPI._poll_loop: 异常")

            await asyncio.sleep(_POLL_INTERVAL)

    # ── 订阅管理 ───────────────────────────────────────────────────────

    async def subscribe(self) -> tuple[str, asyncio.Queue[dict]]:
        """新 SSE 客户端订阅。首条消息自动发送全量快照，并按需拉起轮询。

        Returns:
            tuple: (subscriber_id, queue)，队列用于接收 SSE 事件。
        """
        import uuid
        sid = uuid.uuid4().hex[:8]
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._subscribers[sid] = q

        if self._last_snapshot is not None:
            await q.put({"event": "snapshot", "data": self._last_snapshot})
        else:
            # 首次 snapshot——立即收集
            self._last_snapshot = self._collect_snapshot()
            self._last_task_ids = {t["task_id"] for t in self._last_snapshot}
            await q.put({"event": "snapshot", "data": self._last_snapshot})

        # 基线就位**之后**才拉起轮询——否则 _poll_loop 可能先跑一轮拿到
        # _last_snapshot is None 的早退分支。
        if not self._started:
            logger.warning(
                "WorkflowDebugAPI: subscriber %s 在 start() 之前订阅"
                "——只会收到首帧快照，不会有实时更新", sid)
        self._ensure_polling()

        logger.info("WorkflowDebugAPI: subscriber %s 已连接 (total=%d)", sid, len(self._subscribers))
        return sid, q

    async def unsubscribe(self, sid: str) -> None:
        """移除一个订阅者；最后一个订阅者离开时停止轮询。

        Args:
            sid: 由 subscribe() 返回的订阅者 ID。
        """
        self._subscribers.pop(sid, None)
        logger.info("WorkflowDebugAPI: subscriber %s 已断开 (total=%d)", sid, len(self._subscribers))
        if not self._subscribers:
            await self._stop_polling()

    async def _broadcast(self, event: dict) -> None:
        """推送给所有 subscriber。满队列的跳过（前端太慢）。

        Args:
            event: SSE 事件 dict，包含 event 和 data。
        """
        for sid, q in list(self._subscribers.items()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug(
                    "WorkflowDebugAPI: subscriber %s 队列满，跳过事件 %s",
                    sid, event.get("event"),
                )

    # ── aiohttp handlers ──────────────────────────────────────────────

    async def snapshot_handler(self, request: web.Request) -> web.Response:
        """GET /debug/api/tasks —— 当前全量快照 JSON。

        Args:
            request: aiohttp Request。

        Returns:
            web.Response: 当前所有任务状态的 JSON 快照。
        """
        snapshot = self._collect_snapshot()
        return web.json_response(snapshot)

    async def sse_handler(self, request: web.Request) -> web.StreamResponse:
        """GET /debug/api/tasks/stream —— SSE 事件流。

        Args:
            request: aiohttp Request。

        Returns:
            web.StreamResponse: SSE 流连接，持续推送任务状态变更。

        每 ``_SSE_KEEPALIVE_INTERVAL`` 秒无业务事件时写一行 ``: keepalive`` 注释。
        它**不是**心跳（业务心跳是 ``event: heartbeat``，30s 一次），唯一作用是
        **强制一次写**：本协程停在 ``q.get()`` 上时无法感知对端已离开，只有写失败
        才会走 ``finally`` → :meth:`unsubscribe` → 停轮询。没有它，一次正常关页
        要等到 30s 后的业务心跳才被发现，闲置轮询就多跑 30s。
        """
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # S2-4：**不要**加 `Access-Control-Allow-Origin: *`。SSE 走
                # fetch + 自定义头（debugFetch 带 Authorization），浏览器不允许
                # 通配来源携带凭据；这一行既不起作用，又让"这个端点对任意站点开放"
                # 成为读者（与将来的维护者）的错误前提。debug 页面与后端同源。
            },
        )
        await resp.prepare(request)

        sid, q = await self.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        q.get(), timeout=_SSE_KEEPALIVE_INTERVAL)
                except asyncio.TimeoutError:
                    # 注释行——SSE 规范要求客户端忽略以 `:` 开头的行
                    await resp.write(b": keepalive\n\n")
                    continue
                if event is _SENTINEL:  # sentinel——stop() 发送
                    break
                data = json.dumps(event["data"], ensure_ascii=False, default=str)
                await resp.write(
                    f"event: {event['event']}\ndata: {data}\n\n".encode("utf-8")
                )
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await self.unsubscribe(sid)
        return resp
