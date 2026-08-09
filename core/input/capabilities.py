"""capabilities 构建与广播 — 服务运行时状态下发前端。

嵌套 per-service 形状：available=false 时 model 为 null（不读 None.model，
避免 AttributeError 与"显示选中不可用模型"）。契约单源：构造
`CapabilitiesMessage` 模型 + `wire_dict()` 序列化（ts_null 补显式 null）。
"""

import logging
from typing import TYPE_CHECKING

from Module.output import WebSocketChannel
from frontend_contract import (
    CapabilitiesMessage,
    ServerInfoFeatures,
    ServerInfoMessage,
    ServiceCapability,
)
from frontend_contract.version import PROTOCOL_VERSION

if TYPE_CHECKING:
    from core.agent import Agent
    from core.agent.server import AgentServer

logger = logging.getLogger(__name__)


def build_capabilities(agent: "Agent | AgentServer") -> CapabilitiesMessage:
    """构建嵌套 per-service capabilities 契约模型。

    Args:
        agent: Agent 或 AgentServer mixin（只读公开属性 + config；
            运行时经 Agent 组合，属性实际由 Agent 提供）。

    Returns:
        ``CapabilitiesMessage``（type=capabilities + live2d_static_image +
        services），每个 service 为 ``ServiceCapability{available, enabled, model, default}``。
    """

    def svc(available: bool, enabled: bool, model: str | None, default: str) -> ServiceCapability:
        # model: available=False 时置 None → wire_dict 经 TsNull 补显式 null
        return ServiceCapability(
            available=available,
            enabled=enabled,
            model=model if available else None,
            default=default,
        )

    cfg = agent.config
    return CapabilitiesMessage(
        live2d_static_image=cfg.live2d.static_image,
        services={
            "llm": svc(True, True, agent.llm.model, cfg.llm.model),
            "stt": svc(agent.stt is not None, agent.stt_enabled,
                       agent.stt.model if agent.stt else None, cfg.stt.model),
            "tts": svc(agent.tts is not None, agent.tts_enabled,
                       agent.tts.model if agent.tts else None, cfg.tts.model),
        },
    )


def build_server_info(agent: "Agent", connection_id: str | None = None) -> ServerInfoMessage:
    """构建 v2 server_info（protocol_version/features/services，取代 capabilities）。

    `features` 严禁超前声明——每个字段必须如实反映后端当前真实能力：
    结构化聊天/多会话/Desktop/转写分别在批次 2/3/4/6 实现后置 true，批次 1 全 false。
    `services` 复用与 capabilities 相同的 per-service 形状，Electron 设置面板共用渲染。
    `connection_id`（批次 4，BE-042 附件持有者校验闭环）：v2 + electron 建连时
    ws_handler 传入握手生成的 conn_id → `server_info.connectionId`；非 electron/普通
    v2 传 None → wire 缺省省略（None → exclude_none，v1 类型字节不变）。

    Args:
        agent: Agent 实例（只读公开属性 + config）。
        connection_id: 桌面客户端连接 ID（仅 v2 + electron 建连时非 None）。

    Returns:
        ``ServerInfoMessage``（type=server_info + protocol_version + features + services）。
    """
    from core.version import build_server_version, get_build_sha

    def svc(available: bool, enabled: bool, model: str | None, default: str) -> ServiceCapability:
        return ServiceCapability(
            available=available,
            enabled=enabled,
            model=model if available else None,
            default=default,
        )

    cfg = agent.config
    return ServerInfoMessage(
        # P7 单源：协议版本显式取 PROTOCOL_VERSION（frontend_contract.version）——
        # 运行时不再手写 "2.0" 字面量，改协议版本只改 version.py + make gen-types
        protocol_version=PROTOCOL_VERSION,
        server_version=build_server_version(),
        build_sha=get_build_sha(),  # 批次 7：build SHA 独立上 wire（前端展示/反馈定位）
        features=ServerInfoFeatures(
            structured_generation=True,  # 批次 2 已实现
            multi_conversation=True,     # 批次 3 已实现
            desktop_runtime=True,        # 批次 4 已实现（D-s，spec §8）
            dictation=True,        # 批次 6 已实现（BE-062）
            voice_recording=True,  # 批次 6（BE-060）
            voice_realtime=True,   # 批次 6（BE-061）
            media_upload=True,     # 批次 7 已实现（POST /api/uploads）
        ),
        services={
            "llm": svc(True, True, agent.llm.model, cfg.llm.model),
            "stt": svc(agent.stt is not None, agent.stt_enabled,
                       agent.stt.model if agent.stt else None, cfg.stt.model),
            "tts": svc(agent.tts is not None, agent.tts_enabled,
                       agent.tts.model if agent.tts else None, cfg.tts.model),
        },
        connectionId=connection_id,
    )


async def broadcast_capabilities(agent: "Agent | AgentServer") -> None:
    """服务状态变化（switch_*/set_service 成功）后向 v1 WebSocketChannel 重播。

    不复制 switch_model_result 仅回请求方的模式——遍历 ``agent.output._channels``，
    过滤 ``WebSocketChannel``（agent 无条件注册 ConsoleChannel，无 ``_ws``），
    对每个 WebSocketChannel 调 ``_send_json``（WebSocketChannel 无公开 send）。
    **批次 8c（F15）版本门控**：仅重播 protocol_version < 2 的通道——v1 浏览器
    通道（无头）恒 1 → 保留重播；v2 Electron 建连已收 server_info（services 同形状
    取代 capabilities），跳过避免双轨下发。

    Args:
        agent: Agent 或 AgentServer mixin（switch_*/set_service 内以 self 调用）。
    """
    try:
        payload = build_capabilities(agent)
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel) and getattr(ch, "protocol_version", 1) < 2:
                await ch._send_json(payload.wire_dict())  # noqa: SLF001
    except Exception:
        # 与 per-channel 发送失败（_send_json 内部已记录）区分：这里捕获的是
        # build_capabilities 回归或 list-change——需 warning 级别才可见
        logger.warning("capabilities 重播失败", exc_info=True)
