"""WS chat 域 handler：唤醒开关 / 文本 / 打断。

ARCH-INC-P2-01（批次 D）领域化拆分：原 22 个 dispatcher 聚合在 `ws_handler.py`
单文件内且共享 10 参数签名（参数面随消息类型增长）。现按领域拆分，
签名统一收口为 `(ctx, data, msg, channel)`。**wire 行为逐字节不变**。
"""

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from Module.output import WebSocketChannel

if TYPE_CHECKING:
    from core.agent import Agent
from core.input.ws_handlers.context import WsRequestContext

# 复用父模块 logger 名（保持日志可追溯性：日志过滤/告警规则按
# `core.input.ws_handler` 配置，拆包不应改变运维可观测性）
logger = logging.getLogger("core.input.ws_handler")
from pydantic import ValidationError

from core.input._common import _send_ack
from core.input.audio_buffer import AudioBuffer
from core.input.voice_sessions import VoiceSessionManager
from core.input.ws_v2 import _route_v2_text
from core.network.rate_limiter import RateLimiter
from core.schema.event import InterruptData
from frontend_contract.ws_message import V2TextMessage


async def _dispatch_wake_toggle(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    enabled = msg.enabled if msg is not None else data.get("enabled", False)
    if ctx.audio_buf:
        ctx.audio_buf.set_wake_enabled(enabled)
        logger.debug("唤醒词开关: %s", "开启" if enabled else "关闭")

def _conn_id(channel: WebSocketChannel | None) -> str:
    """本连接的稳定身份（v2 握手分配 conn_<hex>；v1/无连接为 ""）。

    CR-13：透传到工具安全管道，使 HITL 审批只认发起连接。
    """
    conn_id = getattr(channel, "connection_id", None)
    return conn_id if isinstance(conn_id, str) else ""


async def _dispatch_text(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    # 9abc P1-3：v2 文本完整帧 V2TextMessage 校验——ValidationError → ACK
    # rejected CONTRACT_VALIDATION（保留 v2 ACK rejected 通道，spec §4.2），
    # 非 ErrorMessage；合法帧 → 校验后模型交给 _route_v2_text（attachments 等
    # 为已校验字段，消除对未校验 data 加法/索引的 TypeError 断连源）。
    if channel is not None and channel.protocol_version >= 2:
        try:
            text_model = V2TextMessage.model_validate(data)
        except ValidationError as e:
            # clientRequestId 缺失/非 str 也是 ValidationError——错误 ACK 只能回显
            # 合法 str（非 str 原值直传会导致 UserAckMessage 构造失败、ACK 被吞）。
            raw_crid = data.get("clientRequestId", "")
            if not isinstance(raw_crid, str):
                raw_crid = ""
            # 9abc R4：clientMessageId/conversationId 同收口——非 str → None，
            # 否则 UserAckMessage 的 Optional[str] 校验抛 → bare except 吞 ACK，
            # 客户端拿到已拒绝的校验错误却无响应。
            raw_cmid = data.get("clientMessageId")
            raw_cid = data.get("conversationId")
            await _send_ack(ctx.ws, "", raw_crid,
                            raw_cmid if isinstance(raw_cmid, str) else None,
                            raw_cid if isinstance(raw_cid, str) else None,
                            "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": f"消息校验失败: {e}",
                                   "retryable": False})
            return
        # spec §3.8：v2 空文本/空媒体 → rejected CONTRACT_VALIDATION（不再静默丢弃）
        if not (text_model.content.strip() or text_model.stickerId
                or text_model.attachments):
            await _send_ack(ctx.ws, "", text_model.clientRequestId,
                            text_model.clientMessageId, text_model.conversationId,
                            "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": "消息内容为空", "retryable": False})
            return
        await _route_v2_text(ctx.agent, ctx.ws, text_model,
                             sent_message_ids=ctx.sent_message_ids)
        # BE-081 敏感纪律：只记文本长度 + 客户端消息 ID，不记正文
        logger.debug("WS 收到文本 (len=%d, clientMessageId=%s)",
                     len(text_model.content.strip()),
                     text_model.clientMessageId or "")
        return
    # v1 老路径（data.get + 多模态 content-as-parts，v1 不受 v2 收紧影响）
    # 注：原先此处还有 `elif _is_v2(...)` 的空文本 ACK 分支，已删除——本分支只在
    # `channel is None or channel.protocol_version < 2` 时进入，而 _is_v2 复用同一次
    # _find_channel 结果且两处之间无 await（无让出点），故该 elif 恒 False、不可达；
    # v2 空文本的 rejected ACK 由上方 v2 分支（V2TextMessage 校验后）独占处理。
    text_content = data.get("content", "")
    if isinstance(text_content, str):
        if text_content.strip() or data.get("stickerId") or data.get("attachments"):
            await ctx.agent.process_message(text=text_content.strip())
            # BE-081 敏感纪律：只记文本长度 + 客户端消息 ID，不记正文
            logger.debug("WS 收到文本 (len=%d, clientMessageId=%s)",
                         len(text_content.strip()),
                         data.get("clientMessageId", ""))
    elif isinstance(text_content, list):
        # 多模态 content-as-parts 分支（v1 老路径）。
        # CR-09（2026-09-12）：此处曾把客户端 src 原样透传，与 REST `/api/chat` 同根因
        # （RC-13）。现由 `_validate_src` 形态白名单 + `resolve_image_source` 读取白名单
        # 双层收口——非白名单 src 在 `parts_from_dicts` 即抛 ValidationError，由
        # `ws_handler.py` 的分派边界（`except ValidationError`）回 CONTRACT_VALIDATION
        # error 帧且连接存活。故本分支**无需**再做一次收口，也不应向 v1 连接发 v2 ack。
        from core.schema.adapters.message_adapter import content_text, parts_from_dicts
        await ctx.agent.process_message(text=content_text(parts_from_dicts(text_content)), user_name=ctx.agent.user_name, content=text_content)
        logger.debug("WS 收到多模态消息")

async def _dispatch_interrupt(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    logger.debug("WS 收到打断指令")
    await do_interrupt(ctx.agent)



async def do_interrupt(agent: "Agent", skip_vad: bool = False) -> None:
    """发布 INTERRUPT 事件。

    Args:
        agent: Agent 实例。
        skip_vad: 为 True 时，THINKING/EXECUTING 阶段抑制打断（VAD 误报防护）。
                  按钮打断应传 skip_vad=False（默认值）。
    """
    if skip_vad and agent.status.should_suppress_vad_interrupt:
        return
    if not agent.status.is_service_running:
        # S2-R2-4：停机窗口内**输入层不再向 bus 投任何事件**——事件消费者已在关停
        # 第 1 步退出，残留的中断会在同实例重启（rebuild_on_restart=False）后被当成本轮
        # 的中断消费；没有在途轮次时它虽是 no-op，但"残留事件跨运行被消费"本身
        # 就是缺陷族（判据与 CR-08 / process_message 同源：status.is_service_running）。
        logger.debug("interrupt 被拒：服务未在运行（停机窗口）")
        return
    from core.monitoring.event_bus import EventPriority
    agent.bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)
