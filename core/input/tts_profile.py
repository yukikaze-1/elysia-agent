"""TTS profile 共享 helper — 活动会话激活 TTS 后端 + 默认音色解析（Task 9/10 共用）。

放独立小模块避免 api_routes.py 膨胀；Task 10（试听）与 Task 11（WS set_tts_voice）
也经此取"当前激活 TTS 后端"，防多处判定分叉。
"""

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent import Agent
    from Module.tts.protocol import TTSBackend


def _active_tts_profile(agent: "Agent") -> Optional["TTSBackend"]:
    """返回活动会话激活 TTS 后端；TTS 未启用或无后端 → None。

    激活判定对齐 switch_tts / set_service / TTS 启动的有效开关逻辑：
    ``agent.tts_enabled``（三态回退 None → config.tts.enabled）为 True
    且 ``agent.tts`` 后端已挂载（启动 / 热插拔成功后才有）。

    Args:
        agent: Agent 组合根（None 亦安全——getattr 兜底）。

    Returns:
        激活的 TTS 后端，或 None（未启用 / 无后端）。
    """
    if not getattr(agent, "tts_enabled", False):
        return None
    backend = getattr(agent, "tts", None)
    if backend is None:
        return None
    return backend


def _backend_default_voice(backend: "TTSBackend", character: str = "") -> str:
    """后端默认音色 ID（service_state.tts_voice 未设置时 voices 响应的 current）。

    与 synthesize 无 voice_id 覆盖时的实际默认一致：
    - MiniMax → _resolve_voice(character)（voice_map 未命中 → 构造 fallback voice_id）；
    - gpt-sovits / openai → character 即音色键（synthesize 直接把 character 当 voice）。

    Args:
        backend: 激活的 TTS 后端。
        character: 角色名（agent.character.name）。

    Returns:
        后端默认音色 ID；无法判定时空串（路由层转 None）。
    """
    provider = getattr(backend, "provider_name", "")
    if provider == "minimax":
        resolve = getattr(backend, "_resolve_voice", None)  # noqa: SLF001 — 默认音色解析
        if resolve is not None and character:
            return resolve(character)
        return getattr(backend, "_voice_id", "")  # noqa: SLF001 — MiniMax 回退 voice_id
    # gpt-sovits / openai：character 即音色键
    return character
