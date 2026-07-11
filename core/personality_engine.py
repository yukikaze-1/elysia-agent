"""PersonalityEngine — 角色性格引擎。

四层性格模型:
  L1 Core    — 不可变底色（文件加载）
  L2 Self    — 长期自我（session，缓慢演化）
  L3 State   — 短期状态（session，Reflector + 事件驱动）
  L4 Mood    — 当前情绪（EmotionEngine，不动）

CharacterReflector 作为触发源之一，产出的事件提交给 consume_trigger。
"""
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# AwarenessTrigger 配置
AWARENESS_COOLDOWN_ROUNDS = 20  # 同一主题 N 轮内不重复触发
AWARENESS_MAX_HISTORY = 10      # state_history 保留条数


class PersonalityEngine:
    """角色的性格状态管理器。"""

    def __init__(
        self,
        *,
        llm,                              # LLMAdapter (用于 compression 调用)
        session,                          # SessionMemory
        character_name: str,
        user_name: str,
        base_personality: str,            # L1 Core 文本
    ):
        self._llm = llm
        self._session = session
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality

        # 冷却计时: 记录上次 C9 触发的轮次，防止频繁输出"我变了"
        self._last_awareness_round: int = -1

    # ---- 状态恢复 ----

    def restore_state(self) -> None:
        """从 session.params 恢复 L2/L3 状态。必须在 session.load() 之后调用。"""
        # L2/L3 已由 session.load() 加载到 params，无需额外操作
        pass

    # ---- 数据提供（给 section 渲染用）----

    def render_c1(self) -> dict[str, str]:
        """产出 C1 Personality section 的数据。

        Returns:
            {"core_personality": str, "self_personality": str}
            self_personality 为空串时表示无演化
        """
        l2 = self._session.get_param("self_l2", "")
        return {
            "core_personality": self._l1_core,
            "self_personality": l2 if l2 else "",
        }

    def render_c9(self) -> dict[str, str]:
        """产出 C9 SelfPerception section 的数据。

        Returns:
            {"awareness_text": str}
            awareness_text 为空串时 C9 跳过渲染
        """
        l3 = self._session.get_param("state_l3", "")
        if not l3:
            return {"awareness_text": ""}

        # AwarenessTrigger: 偏差检测 + 冷却
        if not self._awareness_allowed():
            return {"awareness_text": ""}

        self._last_awareness_round = self._current_round_estimate()
        return {"awareness_text": l3}

    # ---- 触发事件 ----

    async def consume_trigger(self, source: str, data: dict) -> None:
        """接收外部触发事件，更新 L3 State。

        Args:
            source: 触发源标识 ("reflector" | "event" | "idle" | "feedback")
            data:   触发数据，至少包含 "content" 键
        """
        content = data.get("content", "")
        if not content:
            return

        if source == "reflector":
            # Reflector 产出 → 直接设为 L3 State
            self._set_state_l3(content)

        elif source == "event":
            # 重大事件 → 追加到 L3 State
            self._append_state_l3(content)

        else:
            # idle / feedback → 直接设为 L3 State
            self._set_state_l3(content)

    # ---- awareness 判定 ----

    def _awareness_allowed(self) -> bool:
        """判断当前是否允许 C9 渲染（冷却检查）。

        Phase 1 简单版本：距离上次触发超过 AWARENESS_COOLDOWN_ROUNDS 轮。
        后续 Phase 2 加入 L2/L3 语义偏差检测。
        """
        current = self._current_round_estimate()
        if self._last_awareness_round < 0:
            return True
        return (current - self._last_awareness_round) >= AWARENESS_COOLDOWN_ROUNDS

    def _current_round_estimate(self) -> int:
        """估算当前轮次（基于 state_history 条目数）。"""
        hist = self._session.get_param("state_history", [])
        return len(hist)

    # ---- 内部状态管理 ----

    def _set_state_l3(self, text: str) -> None:
        """设置 L3 State，追加历史。"""
        self._session.set_param("state_l3", text)
        self._append_history("state_history", text)

    def _append_state_l3(self, text: str) -> None:
        """追加内容到 L3 State。"""
        current = self._session.get_param("state_l3", "")
        new = f"{current}\n{text}" if current else text
        self._session.set_param("state_l3", new)
        self._append_history("state_history", text)

    def _append_history(self, key: str, content: str) -> None:
        """追加条目到历史列表，控制长度。"""
        history = self._session.get_param(key, [])
        history.append({"timestamp": time.time(), "content": content})
        if len(history) > AWARENESS_MAX_HISTORY:
            history = history[-AWARENESS_MAX_HISTORY:]
        self._session.set_param(key, history)
