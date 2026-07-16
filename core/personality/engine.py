"""PersonalityEngine — 角色性格引擎。

四层性格模型:
  L1 Core    — 不可变底色（文件加载）
  L2 Self    — 长期自我（session，缓慢演化，L3→L2 压缩）
  L3 State   — 短期状态（session，Reflector + 事件驱动）
  L4 Mood    — 当前情绪（EmotionEngine，不动）

CharacterReflector 作为触发源之一，产出的事件提交给 consume_trigger。
L3→L2 压缩在其中触发：state_history 新增 >= 5 条 OR 距上次 > 7 天。
"""
import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from Module.system_prompt import PromptManager

if TYPE_CHECKING:
    from core.llm_adapter import LLMAdapter
    from core.memory.session import SessionMemory
    from core.schema.character import CharacterState
    from core.storage.character_state_repo import CharacterStateRepository

logger = logging.getLogger(__name__)

# AwarenessTrigger 配置
AWARENESS_COOLDOWN_SECONDS = 3600  # 同一主题冷却 1 小时，避免频繁输出"我变了"
AWARENESS_MAX_HISTORY = 10         # state_history 保留条数

# L3→L2 压缩配置
COMPRESSION_COUNT_THRESHOLD = 5    # state_history 新增 N 条触发压缩
COMPRESSION_TIME_THRESHOLD = 604800  # 距上次压缩 > 7 天也触发
COMPRESSION_MIN_ENTRIES = 3        # 首次压缩最少需要 state_history 条目数


class PersonalityEngine:
    """角色的性格状态管理器。"""

    def __init__(
        self,
        *,
        llm: "LLMAdapter",                # LLMAdapter (用于 compression 调用)
        session: "SessionMemory",         # SessionMemory
        character_name: str,
        user_name: str,
        base_personality: str,            # L1 Core 文本
        prompt_dir: str = "prompt",       # PromptManager 模板目录
    ):
        self._llm = llm
        self._session = session
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality

        # 冷却计时: 记录上次 C9 触发的时间戳
        self._last_awareness_time: float = 0.0

        # L3→L2 压缩
        self._compression_lock = asyncio.Lock()
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("PersonalityEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None

    # ---- 状态恢复 ----

    def restore_state(self) -> None:
        """从 session.params 恢复 L2/L3 状态 + 冷却计时。必须在 session.load() 之后调用。"""
        self._last_awareness_time = self._session.get_param("last_awareness_time", 0.0)

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

        self._last_awareness_time = time.time()
        self._session.set_param("last_awareness_time", self._last_awareness_time)
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

        # L3→L2 压缩检查 — 在状态更新后触发
        await self._maybe_compress()

    # ---- awareness 判定 ----

    def _awareness_allowed(self) -> bool:
        """判断当前是否允许 C9 渲染（冷却检查）。

        Phase 1 简单版本：距离上次触发超过 AWARENESS_COOLDOWN_SECONDS 秒。
        后续 Phase 2 加入 L2/L3 语义偏差检测。
        """
        if self._last_awareness_time == 0.0:
            return True
        return (time.time() - self._last_awareness_time) >= AWARENESS_COOLDOWN_SECONDS

    # ---- L3→L2 压缩 ----

    async def _maybe_compress(self) -> None:
        """检查压缩条件，满足时触发压缩。"""
        if self._pm is None:
            return
        if not self._should_compress():
            return
        if self._compression_lock.locked():
            logger.debug("PersonalityEngine: 压缩已在运行中，跳过")
            return

        async with self._compression_lock:
            # 双重检查 — 上一个压缩可能刚好完成了
            if not self._should_compress():
                return
            await self._compress()

    def _should_compress(self) -> bool:
        """检查是否应触发 L3→L2 压缩。

        触发条件（任一满足）:
        1. state_history 新增 >= COMPRESSION_COUNT_THRESHOLD 条
        2. 距上次压缩 > COMPRESSION_TIME_THRESHOLD 秒

        同时要求 state_history 至少有 COMPRESSION_MIN_ENTRIES 条。
        """
        history = self._session.get_param("state_history", [])
        total = len(history)
        if total < COMPRESSION_MIN_ENTRIES:
            return False

        last_count: int = self._session.get_param("compression_last_count", 0)
        last_at: float = self._session.get_param("compression_last_at", 0.0)

        # 计数触发：新增 >= 阈值
        new_count = total - last_count
        if new_count >= COMPRESSION_COUNT_THRESHOLD:
            logger.debug("PersonalityEngine: 计数触发压缩 (%d 新条目)", new_count)
            return True

        # 时间触发：距上次 > 阈值（仅限已有压缩记录时）
        if last_at > 0 and (time.time() - last_at) >= COMPRESSION_TIME_THRESHOLD:
            logger.debug("PersonalityEngine: 时间触发压缩 (%.1f 天)",
                         (time.time() - last_at) / 86400)
            return True

        return False

    def _get_new_entries(self) -> list[dict]:
        """取上次压缩后新增的 state_history 条目。"""
        history = self._session.get_param("state_history", [])
        last_count: int = self._session.get_param("compression_last_count", 0)
        return history[last_count:]

    async def _compress(self) -> None:
        """执行 L3→L2 压缩：渲染模板 → 调 LLM → 更新 self_l2。"""
        if self._pm is None:
            return

        current_self = self._session.get_param("self_l2", "")
        l3_current = self._session.get_param("state_l3", "")
        new_entries = self._get_new_entries()
        history = self._session.get_param("state_history", [])

        logger.info("PersonalityEngine: 开始 L3→L2 压缩 (%d 新条目, 当前 L2 %d chars)",
                    len(new_entries), len(current_self))

        try:
            system = self._pm.render_macro(
                "PersonalityCompression.j2", "PersonalityCompressionSystemPrompt",
                character_name=self._character_name,
                l1_core=self._l1_core,
            )
            user_prompt = self._pm.render_macro(
                "PersonalityCompression.j2", "PersonalityCompressionUserPrompt",
                character_name=self._character_name,
                current_self=current_self,
                l3_current=l3_current,
                recent_changes=new_entries,
            )
        except Exception:
            logger.warning("PersonalityEngine: 压缩模板渲染失败", exc_info=True)
            return

        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("PersonalityEngine: 压缩 LLM 调用失败 — 下次重试", exc_info=True)
            # 不更新追踪状态，下次 trigger 重试
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        if not text:
            logger.info("PersonalityEngine: 压缩 LLM 返回空 — 更新追踪但不覆盖 L2")
            # 空结果：更新追踪避免无限重试，但不覆盖已有 L2
            self._session.set_param("compression_last_at", time.time())
            self._session.set_param("compression_last_count", len(history))
            return

        # 成功：写入 L2 + 更新追踪
        self._session.set_param("self_l2", text)
        self._session.set_param("compression_last_at", time.time())
        self._session.set_param("compression_last_count", len(history))

        logger.info("PersonalityEngine: L3→L2 压缩完成 (%d chars, %.0f ms)",
                    len(text), elapsed_ms)

    async def flush_compression(self) -> None:
        """Shutdown 时强制压缩（如有未处理条目）。带超时保护。"""
        if self._pm is None:
            return
        history = self._session.get_param("state_history", [])
        last_count: int = self._session.get_param("compression_last_count", 0)
        new_count = len(history) - last_count
        if new_count < COMPRESSION_MIN_ENTRIES:
            logger.debug("PersonalityEngine flush: 仅 %d 新条目，跳过压缩", new_count)
            return
        if self._compression_lock.locked():
            logger.debug("PersonalityEngine flush: 压缩已在运行中")
            return

        async with self._compression_lock:
            try:
                await asyncio.wait_for(self._compress(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("PersonalityEngine: 压缩 flush 超时")
            except Exception:
                logger.debug("PersonalityEngine: 压缩 flush 失败", exc_info=True)

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

    # ---- 持久化 hooks ----

    async def save_character_state(self, repo: "CharacterStateRepository") -> None:
        """保存当前性格状态。shutdown 时调用。"""
        from core.schema.character import CharacterState
        from core.schema.types import SessionId
        state = CharacterState(
            session_id=SessionId(self._session.session_id),
            self_l2=self._session.get_param("self_l2", ""),
            state_l3=self._session.get_param("state_l3", ""),
            state_history=self._session.get_param("state_history", []),
            last_awareness_time=self._session.get_param("last_awareness_time", None),
            compression_last_at=self._session.get_param("compression_last_at", None),
            compression_last_count=self._session.get_param("compression_last_count", 0),
        )
        await repo.save_character(state)

    def restore_character_state(self, state: "CharacterState") -> None:
        """从持久化恢复性格状态。start() 时在 restore_state 之后调用。"""
        self._session.set_param("self_l2", state.self_l2)
        self._session.set_param("state_l3", state.state_l3)
        self._session.set_param("state_history", state.state_history)
        if state.last_awareness_time:
            self._session.set_param("last_awareness_time", state.last_awareness_time)
            self._last_awareness_time = state.last_awareness_time
        if state.compression_last_at:
            self._session.set_param("compression_last_at", state.compression_last_at)
        if state.compression_last_count is not None:
            self._session.set_param("compression_last_count", state.compression_last_count)
