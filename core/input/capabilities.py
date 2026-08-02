"""capabilities 构建与广播 — 服务运行时状态下发前端。

嵌套 per-service 形状：available=false 时 model 为 null（不读 None.model，
避免 AttributeError 与"显示选中不可用模型"）。
"""

import logging
from typing import TYPE_CHECKING

from Module.output import WebSocketChannel

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


def build_capabilities(agent: "Agent") -> dict:
    """构建嵌套 per-service capabilities。

    Args:
        agent: Agent 实例（只读公开属性 + config）。

    Returns:
        ``{"type": "capabilities", "live2d_static_image": ..., "services": {...}}``，
        每个 service 为 ``{available, enabled, model, default}``。
    """

    def svc(available: bool, enabled: bool, model: str | None, default: str) -> dict:
        return {
            "available": available,
            "enabled": enabled,
            "model": model if available else None,
            "default": default,
        }

    cfg = agent.config
    return {
        "type": "capabilities",
        "live2d_static_image": cfg.live2d.static_image,
        "services": {
            "llm": svc(True, True, agent.llm.model, cfg.llm.model),
            "stt": svc(agent.stt is not None, agent.stt_enabled,
                       agent.stt.model if agent.stt else None, cfg.stt.model),
            "tts": svc(agent.tts is not None, agent.tts_enabled,
                       agent.tts.model if agent.tts else None, cfg.tts.model),
        },
    }


async def broadcast_capabilities(agent: "Agent") -> None:
    """服务状态变化（switch_*/set_service 成功）后向所有 WebSocketChannel 重播。

    不复制 switch_model_result 仅回请求方的模式——遍历 ``agent.output._channels``，
    过滤 ``WebSocketChannel``（agent 无条件注册 ConsoleChannel，无 ``_ws``），
    对每个 WebSocketChannel 调 ``_send_json``（WebSocketChannel 无公开 send）。

    Args:
        agent: Agent 实例。
    """
    try:
        payload = build_capabilities(agent)
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel):
                await ch._send_json(payload)  # noqa: SLF001
    except Exception:
        # 与 per-channel 发送失败（_send_json 内部已记录）区分：这里捕获的是
        # build_capabilities 回归或 list-change——需 warning 级别才可见
        logger.warning("capabilities 重播失败", exc_info=True)
