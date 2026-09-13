"""server health/ready 探针（2026-08-14 批次 D 从 server.py 拆出）。

health_handler / ready_handler + _runtime_info / _liveness_payload / _probe / _readiness_payload。
函数体逐字不动。依赖 core.agent / core.config / core.version / errors。
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from aiohttp import web

from core.input.errors import _protocol_error_response
from core.version import build_server_version, get_build_sha

if TYPE_CHECKING:
    from core.agent import Agent
    from core.config import AppConfig
    from core.storage.idempotency_repo import IdempotencyRepository

logger = logging.getLogger(__name__)


_HEALTH_TIMEOUT = 2.0
_PROCESS_START = time.monotonic()


def _runtime_info(agent: "Agent") -> dict:
    """运行信息负载（/health 与 /ready 共用）：fsm/llm/memory/tools/版本/uptime。

    全部进程内存读取，无任何外部 I/O——liveness 借此实现零网络探针，readiness 借此
    附加可观测字段。fsm 内省失败降级 "unknown"（探针不应 500）。

    Args:
        agent: Agent 实例（公开接口 session / long_term / tool_adapter）。
    """
    try:
        fsm_state = agent.status.current_name()
    except Exception:
        fsm_state = "unknown"
    try:
        # CR-08：`fsm` 与 `run_state` 是**两个状态源**，停机窗口内二者不同步
        # （stop() 首行置 STOPPED，FSM 仍显示 accepting）。两个都报，分歧即可见。
        # 非 str 值（非真实 Agent / 测试替身）降级 unknown——/health 的"恒 200、
        # 零网络探针"是硬不变量，不能因为多报一个字段而 500（原 fsm 的 try 同理）。
        run_state = agent.state.name
        if not isinstance(run_state, str):
            run_state = "unknown"
    except Exception:
        run_state = "unknown"
    return {
        "fsm": fsm_state,
        "run_state": run_state,
        "llm": getattr(agent.llm, "model", "unknown"),
        "memory": {
            # 走公开接口 session.messages，不摸私有 _messages（改名时 getattr 会静默降级）
            "session_count": len(agent.session.messages),
            "long_term_count": agent.long_term.count,
        },
        # 守卫：startup 早期工具子系统可能未就绪，getattr 兜底 "<loading>"
        # （批次 A 门面：原为 getattr(getattr(adapter, "_manager"), ...) 双跳私有穿透，
        #   2026-09-12 收口为公开属性 tool_names）
        "tools": getattr(agent.tool_adapter, "tool_names", ["<loading>"]),
        "version": build_server_version(),
        "build_sha": get_build_sha(),
        "uptime": round(time.monotonic() - _PROCESS_START, 1),
        # 探活不应扰动 request_id 计数器（批次 8a Task 15）：request_id 独立 uuid 生成，
        # 不触碰 agent.next_request_id（该计数器由真实业务请求/生成链路驱动）。
        "request_id": f"req_{uuid.uuid4().hex[:24]}",
    }


def _liveness_payload(agent: "Agent") -> dict:
    """Liveness 负载：进程/FSM/运行信息，**零网络探针**（BE-P2-01 D9）。

    /health 只做进程存活检查 + 运行信息（fsm/uptime/build），不触碰任何外部依赖
    （session/message/attachment/Milvus/idempotency 一律不 wait_for）——任何依赖
    挂起都在固定预算内返回。status 恒 "ok"（进程存活即 ok；依赖状态由 /ready 负责）。

    Args:
        agent: Agent 实例。
    """
    return {"status": "ok", **_runtime_info(agent)}


async def _probe(factory: Callable[[], Any]) -> bool:
    """单依赖真实探测：wait_for 限时，超时/抛异常/mock 不可 await → 一律视为 down。

    Args:
        factory: 返回 awaitable 的探测工厂（如 repo.health_check）。
    """
    try:
        return bool(await asyncio.wait_for(factory(), timeout=_HEALTH_TIMEOUT))
    except Exception:
        # 超时 / 依赖抛异常 / mock 不可 await → 一律视为 down
        return False


async def _readiness_payload(
    agent: "Agent", *,
    product_mode: bool,
    idempotency_repo: "IdempotencyRepository | None",
) -> dict:
    """Readiness 负载：核心 chat 门禁 + 依赖明细（BE-P2-01 D9）。

    - ``chatReady``（HTTP 门禁）= ``mongo_ok``（session+message+attachment 真实 ping，
      ``asyncio.gather`` 并行探测）且 ``identity_ok``（用户身份权威存储可达，
      P0-3 fail-closed）且 ``lifecycle_ok``（``Agent._state == RUNNING``，CR-08：
      停机窗口必须立刻摘除）且（非 product_mode 或 ``idempotency_ok`` 真实 ping
      且补偿状态满足）；product 模式幂等门禁 = ping AND not ``needs_recovery``
      （BE-P1-07 D4：降级期记录未 reconcile 时 chatReady 保持 false）；
    - ``fullReady`` = ``chatReady`` 且 ``milvus_ok`` 且 ``embedding_ok``——后者是
      "长期记忆真的能持久化"的完整条件（C/CR-10：嵌入不可用时 `insert()` 只写进程内
      `_entries`，重启即丢）。Milvus 与 embedding 都**不门禁 HTTP**（文本可降级使用），
      只经 ``fullReady``/``dependencies`` 可观测；
    - ``status``：全依赖 ok → "ok" 否则 "degraded"（可观测，非 HTTP 门禁）。

    Args:
        agent: Agent 实例。
        product_mode: 产品模式（幂等 fail-closed）；dev 模式允许 idempotency 降级不门禁。
        idempotency_repo: 幂等 claim 仓库；None（纯内存 dev）→ 无持久依赖可探视为 ok。
    """
    # mongo 三 repo 并行探测（asyncio.gather + 每项 wait_for）——慢依赖不串行累加
    session_ok, message_ok, attachment_ok = await asyncio.gather(
        _probe(agent.session.health_check),
        _probe(agent.message_repo.health_check),
        _probe(agent.attachment_repo.health_check),
    )
    mongo_ok = session_ok and message_ok and attachment_ok

    milvus_ok = await _probe(agent.long_term.health_check)

    # C/CR-10：嵌入后端可用性。**非阻塞属性**（不触发冷加载——实测 11s 起）。
    # 原先 `/ready` 只报 mongo/milvus：Milvus 通了而 embedding 挂了时，探针与面板
    # 全都正常，长期记忆实际"只在进程内、重启即丢"（CR-10 的静默丢数据）。
    embedding_ok = bool(getattr(agent.long_term, "embedding_ready", False))

    # P0-3：用户身份权威存储（Mongo user_identity_profiles）纳入门禁——身份不可用时
    # 不得用占位身份继续生成/建会话（fail-closed）。无 repo 的本地模式 health_check
    # 恒 True，不门禁。
    identity_ok = await _probe(agent.user_identity_store.health_check)

    # idempotency：repo 为 None（纯内存 dev）→ 无持久依赖可探视为 ok；product 模式
    # 真实 ping 且补偿完成（ping AND not needs_recovery——D4：降级期记录未 reconcile
    # 时 chatReady 保持 false，用 needs_recovery 而非 durable 避免零降级瞬时窗口误伤）。
    # get_store() 读进程全局单例——与注入 repo 同实例的耦合由 create_app 单点维持
    # （configure_store + app["idempotency_repo"] 同源）。
    idempotency_ok = True
    if idempotency_repo is not None:
        idempotency_ok = await _probe(idempotency_repo.health_check)
        if product_mode:
            from core.storage.idempotency import get_store
            idempotency_ok = idempotency_ok and not get_store().needs_recovery

    chat_ready = mongo_ok and identity_ok and (not product_mode or idempotency_ok)

    # CR-08：非 RUNNING 一律不就绪。原先 /ready 在整个停机窗口持续 200，误导负载
    # 均衡继续导流——而这段时间 `process_message` 会把消息投进无人消费的 bus 队列
    # 并回成功。readiness 读 `_state`（与事件消费者同源），停机即摘除。
    from core.agent import RunState
    lifecycle_ok = agent.state == RunState.RUNNING
    chat_ready = chat_ready and lifecycle_ok

    # `fullReady` = "长期记忆真的能持久化"：chatReady + Milvus 可达 + **嵌入就绪**。
    # 三者缺一，insert() 都只会写进进程内 `_entries`（重启即丢），故都属"完整就绪"。
    full_ready = chat_ready and milvus_ok and embedding_ok
    dependencies = {
        "mongo": mongo_ok,
        "milvus": milvus_ok,
        "embedding": embedding_ok,
        "idempotency": idempotency_ok,
        "identity": identity_ok,
        "lifecycle": lifecycle_ok,
    }
    status = "ok" if all(dependencies.values()) else "degraded"

    return {
        "status": status,
        "chatReady": chat_ready,
        "fullReady": full_ready,
        **_runtime_info(agent),
        "dependencies": dependencies,
    }


async def health_handler(request: web.Request) -> web.Response:
    """Liveness 探针——进程存活检查，恒 200，**零网络探针**（BE-P2-01 D9）。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: 200 JSON 探针负载（进程/FSM/运行信息；不探外部依赖）。
    """
    return web.json_response(_liveness_payload(request.app["agent"]))


async def ready_handler(request: web.Request) -> web.Response:
    """Readiness 探针——核心 chat 门禁：``chatReady`` 才 200（BE-P2-01 D9）。

    chatReady = mongo 并行 ping 且 lifecycle（``_state == RUNNING``，CR-08）且
    （非 product 或 idempotency ping）；Milvus 不门禁 HTTP（文本可降级），经
    payload 的 fullReady/dependencies 可观测。

    Args:
        request: aiohttp Request（product_mode / idempotency_repo 在 create_app 注入）。

    Returns:
        web.Response: chatReady → 200；否则 503（payload 带 chatReady/fullReady/
        dependencies 解释）。
    """
    agent = request.app["agent"]
    payload = await _readiness_payload(
        agent,
        product_mode=request.app.get("product_mode", False),
        idempotency_repo=request.app.get("idempotency_repo"),
    )
    status_code = 200 if payload["chatReady"] else 503
    return web.json_response(payload, status=status_code)
