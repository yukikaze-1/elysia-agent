"""make_public_state — CharacterRuntimeState(内部扁平态)→ CharacterPublicStateView(产品裁剪视图)。

阶段 G(架构稿 §12.4)。产品态只暴露 mood/currentActivity/lastInteractionAt/proactiveEnabled/
updatedAt;内部态(self_l2/state_l3/self_perception_history/reflector_buffer 等)绝不外泄。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from frontend_contract.rest import CharacterPublicStateView, MoodState

if TYPE_CHECKING:
    from core.character.runtime import CharacterRuntime


def make_public_state(runtime: "CharacterRuntime") -> CharacterPublicStateView:
    """从 runtime 构造产品态视图。

    数据源:
      - mood.label/intensity: runtime.state.mood / mood_intensity(engine.state 共享引用)
      - mood.updatedAt: runtime.state.updated_at(frozen,已知近似——无独立 mood 时间戳)
      - lastInteractionAt / updatedAt: runtime.instance.updatedAt(每 flush 更新,agent.py:432)
      - proactiveEnabled: runtime.revision.runtimeDefaults["proactive"]["enabled"](immutable 配置)
    内部态绝不外泄——只读上述白名单字段。
    """
    state = runtime.state
    runtime_defaults: dict[str, Any] = dict(runtime.revision.runtimeDefaults or {})
    proactive = runtime_defaults.get("proactive") or {}
    return CharacterPublicStateView(
        mood=MoodState(label=state.mood, intensity=state.mood_intensity,
                       updatedAt=state.updated_at),
        currentActivity=None,
        lastInteractionAt=runtime.instance.updatedAt,
        proactiveEnabled=bool(proactive.get("enabled", False)),
        updatedAt=runtime.instance.updatedAt,
    )
