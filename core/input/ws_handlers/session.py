"""WS session 域 handler：模型切换 / TTS 音色 / 服务开关 / 会话激活。

ARCH-INC-P2-01（批次 D）领域化拆分；详见 `chat.py` 模块说明。
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
from core.input._common import _find_channel
from core.memory.session_manager import SessionBusyError
from core.input.tts_profile import _active_tts_profile
from core.schema.types import SessionId
from frontend_contract import (
    ErrorMessage,
    ServiceModeResultMessage,
    SetTtsVoiceResultMessage,
    SwitchModelResultMessage,
)


async def _dispatch_switch_model(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    service = msg.service if msg is not None else data.get("service", "")
    model = msg.model if msg is not None else data.get("model", "")
    if service == "llm":
        result = await ctx.agent.switch_llm(model)
    elif service == "stt":
        result = await ctx.agent.switch_stt(model)
    elif service == "tts":
        result = await ctx.agent.switch_tts(model)
    else:
        result = {"ok": False, "error": f"未知 service: {service}"}
    from Module.output import WebSocketChannel
    if ctx.ws is not None:
        for ch in ctx.agent.output.channels:
            if isinstance(ch, WebSocketChannel) and ch.ws is ctx.ws:
                try:
                    # 契约：error 仅失败带（成功缺省省略，非 null）
                    await ctx.ws.send_json(SwitchModelResultMessage(
                        service=service,
                        model=model,
                        ok=result.get("ok", False),
                        error=result.get("error"),
                    ).wire_dict())
                except Exception:
                    logger.debug("Failed to send switch_model_result", exc_info=True)
                break
    logger.info("WS 切换模型: service=%s model=%s ok=%s", service, model, result.get("ok"))

async def _dispatch_set_tts_voice(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    # 批次 5（Task 11，BE-051）：设置活动会话 TTS 声音覆盖。
    # 经活动会话激活 TTS 后端 list_voices() 校验 voice 存在 → 写活动会话
    # service_state.tts_voice + save → 回 set_tts_voice_result；未知 voice/
    # 后端不可用 → {ok: False, error}，不改 state（失败保留原选择）。
    voice = msg.voice if msg is not None else data.get("voice", "")
    result: dict = {"ok": False, "error": "TTS 未启用"}
    # review 修正：空串/缺省 voice = 清除覆盖，回退 character 默认音色——
    # 否则 tts_voice 只进不出（list_voices 只含系统音色，角色默认不在列），无恢复路径。
    if not voice:
        ctx.agent.session.service_state.tts_voice = None
        try:
            await ctx.agent.session.save()
        except Exception as e:
            logger.warning("TTS voice 已清除但持久化失败: %s", e)
        result = {"ok": True, "voice": None}
    backend = _active_tts_profile(ctx.agent)
    if backend is not None and voice:
        try:
            voices = await backend.list_voices()
            if not any(v.get("id") == voice for v in voices):
                result = {"ok": False, "error": f"未知音色: {voice}"}
            else:
                ctx.agent.session.service_state.tts_voice = voice
                try:
                    await ctx.agent.session.save()
                except Exception as e:
                    # 持久化失败不回滚已生效的写入——运行中 state 是权威，仅告警
                    logger.warning("TTS voice 已设置但持久化失败: %s", e)
                result = {"ok": True, "voice": voice}
        except Exception as e:
            logger.warning("set_tts_voice 失败: %s", e)
            # 信封不携带异常文本（review Minor）——只出通用消息
            result = {"ok": False, "error": "设置声音失败"}
    from Module.output import WebSocketChannel
    if ctx.ws is not None:
        for ch in ctx.agent.output.channels:
            if isinstance(ch, WebSocketChannel) and ch.ws is ctx.ws:
                try:
                    await ctx.ws.send_json(SetTtsVoiceResultMessage(
                        ok=result.get("ok", False),
                        voice=result.get("voice"),
                        error=result.get("error"),
                    ).wire_dict())
                except Exception:
                    logger.debug("Failed to send set_tts_voice_result", exc_info=True)
                break
    logger.info("WS 设置 TTS 声音: voice=%s ok=%s", voice, result.get("ok"))

async def _dispatch_set_service(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    service = msg.service if msg is not None else data.get("service", "")
    enabled = msg.enabled if msg is not None else data.get("enabled", False)
    result = await ctx.agent.set_service(service, enabled)
    # 广播给所有 WebSocket 客户端（而非仅发送者）——含失败时的 enabled 字段。
    # 不复制 switch_model_result 模式（那只回给请求方）。capabilities 重播在 Task 7 接入。
    from Module.output import WebSocketChannel
    try:
        # channels 是快照副本（OutputManager.channels），并发 connect/disconnect
        # 不会让本循环的迭代中途失效
        for ch in ctx.agent.output.channels:
            if isinstance(ch, WebSocketChannel):
                # 契约：error 键恒存在（成功为 null，ts_null 标记）
                await ch.send_json(ServiceModeResultMessage(
                    service=service,
                    enabled=result.get("enabled", enabled),
                    ok=result.get("ok", False),
                    error=result.get("error"),
                ).wire_dict())
    except Exception:
        # 并发 connect/disconnect 会改通道集合——捕获记录，
        # 不向请求方传播（per-channel 发送失败已由 send_json 内部记录）
        logger.warning("set_service 广播失败（连接并发变化？）", exc_info=True)
    logger.info("WS 设置服务开关: service=%s enabled=%s ok=%s", service, enabled, result.get("ok"))
    # 批次 6（spec §3.2 NEW-X/XA）：stt 关闭成功 → 通知本连接语音会话（RECORDING 除外）。
    # 9abc P1-2：仅对依赖全局 STT 的会话生效——profile dictation 途中关闭全局 STT
    # 不取消独立会话（depends_on_global_stt() 判定，与 _voice_stt_gate 共用）。
    if (service == "stt" and not enabled and result.get("ok")
            and ctx.voice_mgr is not None and ctx.voice_mgr.depends_on_global_stt()):
        try:
            await ctx.voice_mgr.notify_stt_disabled()
        except Exception:
            logger.debug("notify_stt_disabled 失败", exc_info=True)

async def _dispatch_activate_session(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    # 批次 3：切 active + 重发新活动会话回放；未知 conversationId → SESSION_NOT_FOUND
    # review 修复（兼容#5）：activate_session 仅 v2 连接处理——v1 恒 default 无
    # 切会话语义，处理会让 v1 连接意外切走 active 影响 v2 客户端。
    if ctx.ws is None:
        logger.debug("WS activate_session 无发送连接，忽略")
    else:
        channel = _find_channel(ctx.agent, ctx.ws)
        if channel is None or channel.protocol_version < 2:
            logger.debug("WS activate_session: v1 连接忽略（v1 恒 default）")
        else:
            await _handle_activate_session(ctx.agent, ctx.ws, data)



async def _handle_activate_session(agent: "Agent", ws: "web.WebSocketResponse", data: dict) -> None:
    """处理 activate_session：切 active + 重发目标会话回放；未知会话 → 404。

    spec §6.1：`await agent.switch_session(conversationId)` → 重发 SessionMessage
    （新活动会话回放，复用建连回放路径）。未知 conversationId → 不发回放，
    发 `ErrorMessage{code: SESSION_NOT_FOUND}`（批次 1 错误信封）。

    批次 3 review 修复（F1-a）：切换前 FSM 门禁——agent 忙（生成/TTS 中，
    is_accepting_input=False）→ 拒绝 AGENT_BUSY（retryable 语义由码名表达），
    不执行切换。Task 11（M3）：engine per-instance 后无全局单例 attach_session
    换绑向量，但忙时切换仍拒绝——防与一次 v2 生成流式/TTS 并发 → mood/L3 跨会话污染。
    """
    # 连接级 helper 在 ws_handler（本包父模块）——函数内懒导入，避免模块级环
    from core.input.ws_handler import (
        _current_request_id, _emit_state_snapshot, _send_session_replay,
    )

    sid = data.get("conversationId", "")
    if not sid:
        await ws.send_json(ErrorMessage(
            message="缺少 conversationId",
            code="CONTRACT_VALIDATION",
            requestId=_current_request_id(),
        ).wire_dict())
        return
    try:
        if not getattr(agent.status, "is_accepting_input", True):
            logger.info("activate_session: agent 忙（is_accepting_input=False），拒绝切换 %s", sid)
            await ws.send_json(ErrorMessage(
                message="Agent 忙（生成/TTS 中），无法切换会话",
                code="AGENT_BUSY",
                requestId=_current_request_id(),
            ).wire_dict())
            return
        if not await agent.session_manager.has_session(sid):
            logger.debug("activate_session: 未知会话 %s → SESSION_NOT_FOUND", sid)
            await ws.send_json(ErrorMessage(
                message=f"会话不存在: {sid}",
                code="SESSION_NOT_FOUND",
                requestId=_current_request_id(),
            ).wire_dict())
            return
        target = await agent.switch_session(SessionId(sid))
        await _send_session_replay(ws, agent, target)
        # 阶段 G(§7.1):v2 + character_state_stream=true 时发角色状态快照(强制,不看 last)
        if getattr(agent, "character_state_stream", False):
            await _emit_state_snapshot(agent, ws)
        logger.info("WS activate_session: 已切到会话 %s", sid)
    except SessionBusyError:
        # review 修复（批次 C）：switch() 双保险 busy check 抛 SessionBusyError——
        # 此前被通用 except Exception 吞成 INTERNAL。单独捕获 → AGENT_BUSY（可重试）。
        logger.info("activate_session: 双保险 busy check 命中（SessionBusyError），拒绝切换 %s", sid)
        await ws.send_json(ErrorMessage(
            message="Agent 忙（生成/TTS 中），无法切换会话",
            code="AGENT_BUSY",
            requestId=_current_request_id(),
        ).wire_dict())
    except Exception:
        logger.warning("activate_session 处理失败 sid=%s", sid, exc_info=True)
        await ws.send_json(ErrorMessage(
            message="激活会话失败",
            code="INTERNAL",  # review 修复（兼容#4）：INTERNAL_ERROR → 错误目录标准码 INTERNAL
            requestId=_current_request_id(),
        ).wire_dict())
