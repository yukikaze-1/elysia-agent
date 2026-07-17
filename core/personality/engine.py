"""CharacterEngine — 角色性格 + 情绪引擎。

合并原 PersonalityEngine + EmotionEngine，统一使用 CharacterState 管理所有状态。

四层性格模型:
  L1 Core    — 不可变底色（文件加载）
  L2 Self    — 长期自我（CharacterState，缓慢演化，L3→L2 压缩）
  L3 State   — 短期状态（CharacterState，Reflector + 事件驱动）
  L4 Mood    — 当前情绪（CharacterState.mood，keyword + STT 驱动）

CharacterReflector 作为子组件，产出的事件提交给 consume_trigger。
L3→L2 压缩在其中触发：state_history 新增 >= 5 条 OR 距上次 > 7 天。
"""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from Module.system_prompt import PromptManager
from Module.output import OutputManager
from core.schema.mood import Mood

if TYPE_CHECKING:
    from core.llm_adapter import LLMAdapter
    from core.schema.character import CharacterState, StateHistoryEntry

logger = logging.getLogger(__name__)

# AwarenessTrigger 配置
AWARENESS_COOLDOWN_SECONDS = 3600  # 同一主题冷却 1 小时，避免频繁输出"我变了"
AWARENESS_MAX_HISTORY = 10         # state_history 保留条数

# L3→L2 压缩配置
COMPRESSION_COUNT_THRESHOLD = 5    # state_history 新增 N 条触发压缩
COMPRESSION_TIME_THRESHOLD = 604800  # 距上次压缩 > 7 天也触发
COMPRESSION_MIN_ENTRIES = 3        # 首次压缩最少需要 state_history 条目数

# ---- 情绪关键词（从 emotion_engine.py 迁移）----

MOOD_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    "happy": (
        ["哈哈", "开心", "太好了", "喜欢", "棒", "nice", "嘿嘿", "耶", "赞", "爱你"],
        ["你喜欢", "我也开心", "太好了", "哈哈", "嘿嘿", "开心"],
    ),
    "excited": (
        ["!", "！", "天哪", "wow", "太棒", "厉害", "牛逼", "惊", "竟然"],
        ["!", "！", "兴奋", "激动", "期待"],
    ),
    "sad": (
        ["难过", "伤心", "哭", "失败", "失去", "遗憾", "倒霉", "唉", "烦", "累"],
        ["别难过", "抱抱", "辛苦了", "会好起来的"],
    ),
    "curious": (
        ["为什么", "怎么", "什么原因", "讲讲", "说说", "好奇", "告诉我"],
        ["好问题", "有趣", "想知道"],
    ),
    "neutral": (
        [],
        [],
    ),
    "warm": (
        ["谢谢", "感谢", "感恩", "感激", "温暖", "贴心", "真好", "感动"],
        ["不客气", "我的荣幸", "应该的"],
    ),
    "concerned": (
        ["担心", "焦虑", "不安", "紧张", "害怕", "怎么办", "压力"],
        ["别担心", "会好的", "我理解你的担心"],
    ),
    "calming": (
        ["气死", "生气", "愤怒", "怒火", "离谱", "太过分", "受不了", "岂有此理"],
        ["冷静", "消消气", "别急", "慢慢说"],
    ),
    "reassuring": (
        ["怕", "恐惧", "恐怖", "吓人", "不敢", "担心害怕"],
        ["别怕", "有我在", "我会保护你"],
    ),
    "understanding": (
        ["恶心", "想吐", "厌恶", "讨厌", "反感", "受不了这个"],
        ["我懂", "确实不舒服", "需要适应"],
    ),
}

# STT 情绪 → AI mood 映射（非 1:1 — AI 不直接复制用户情绪）
_STT_EMOTION_TO_MOOD: dict[str, Mood | None] = {
    "HAPPY":     Mood.WARM,
    "SAD":       Mood.CONCERNED,
    "ANGRY":     Mood.CALMING,
    "FEARFUL":   Mood.REASSURING,
    "DISGUSTED": Mood.UNDERSTANDING,
    "SURPRISED": Mood.CURIOUS,
    "NEUTRAL":   None,  # 不触发变化
}


@dataclass
class C1Data:
    """C1 Personality section 渲染数据。"""
    core_personality: str
    self_personality: str


@dataclass
class C9Data:
    """C9 SelfPerception section 渲染数据。"""
    awareness_text: str


class CharacterEngine:
    """角色的性格 + 情绪状态管理器。

    合并了原 PersonalityEngine（L1-L4 性格模型 + 压缩）和
    EmotionEngine（keyword/STT 情绪检测 + expression 推送）。
    """

    def __init__(
        self,
        *,
        llm: "LLMAdapter",
        state: "CharacterState",
        character_name: str,
        user_name: str,
        base_personality: str,
        character_config,          # CharacterConfig
        output: OutputManager,
        prompt_dir: str = "prompt",
    ):
        self._llm = llm
        self.state = state
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality
        self._character = character_config
        self._output = output
        self._idle_expressions = character_config.emotion.idle_expressions

        # 冷却计时: 记录上次 C9 触发的时间戳
        self._last_awareness_time: float = state.last_awareness_time or 0.0

        # L3→L2 压缩
        self._compression_lock = asyncio.Lock()
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None

        # 子组件: CharacterReflector
        from .reflector import CharacterReflector
        self._reflector = CharacterReflector(
            engine=self,
            l1_core=base_personality,
            llm=llm,
            character_name=character_name,
            user_name=user_name,
            prompt_dir=prompt_dir,
        )

    # ---- 数据提供（给 section 渲染用）----

    def render_c1(self) -> C1Data:
        """产出 C1 Personality section 的数据。"""
        l2 = self.state.self_l2
        return C1Data(
            core_personality=self._l1_core,
            self_personality=l2 if l2 else "",
        )

    def render_c9(self) -> C9Data:
        """产出 C9 SelfPerception section 的数据。awareness_text 为空串时 C9 跳过渲染。"""
        l3 = self.state.state_l3
        if not l3:
            return C9Data(awareness_text="")

        # AwarenessTrigger: 偏差检测 + 冷却
        if not self._awareness_allowed():
            return C9Data(awareness_text="")

        self._last_awareness_time = time.time()
        self.state.last_awareness_time = self._last_awareness_time
        return C9Data(awareness_text=l3)

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
            logger.debug("CharacterEngine: 压缩已在运行中，跳过")
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
        history = self.state.state_history
        total = len(history)
        if total < COMPRESSION_MIN_ENTRIES:
            return False

        last_count: int = self.state.compression_last_count
        last_at = self.state.compression_last_at or 0.0

        # 计数触发：新增 >= 阈值
        new_count = total - last_count
        if new_count >= COMPRESSION_COUNT_THRESHOLD:
            logger.debug("CharacterEngine: 计数触发压缩 (%d 新条目)", new_count)
            return True

        # 时间触发：距上次 > 阈值（仅限已有压缩记录时）
        if last_at > 0 and (time.time() - last_at) >= COMPRESSION_TIME_THRESHOLD:
            logger.debug("CharacterEngine: 时间触发压缩 (%.1f 天)",
                         (time.time() - last_at) / 86400)
            return True

        return False

    def _get_new_entries(self) -> "list[StateHistoryEntry]":
        """取上次压缩后新增的 state_history 条目。"""
        history = self.state.state_history
        last_count: int = self.state.compression_last_count
        return history[last_count:]

    async def _compress(self) -> None:
        """执行 L3→L2 压缩：渲染模板 → 调 LLM → 更新 self_l2。"""
        if self._pm is None:
            return

        current_self = self.state.self_l2
        l3_current = self.state.state_l3
        new_entries = self._get_new_entries()
        history = self.state.state_history

        logger.info("CharacterEngine: 开始 L3→L2 压缩 (%d 新条目, 当前 L2 %d chars)",
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
            logger.warning("CharacterEngine: 压缩模板渲染失败", exc_info=True)
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
            logger.warning("CharacterEngine: 压缩 LLM 调用失败 — 下次重试", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        if not text:
            logger.info("CharacterEngine: 压缩 LLM 返回空 — 更新追踪但不覆盖 L2")
            self.state.compression_last_at = time.time()
            self.state.compression_last_count = len(history)
            return

        # 成功：写入 L2 + 更新追踪
        self.state.self_l2 = text
        self.state.compression_last_at = time.time()
        self.state.compression_last_count = len(history)

        logger.info("CharacterEngine: L3→L2 压缩完成 (%d chars, %.0f ms)",
                    len(text), elapsed_ms)

    async def flush_compression(self) -> None:
        """Shutdown 时强制压缩（如有未处理条目）。带超时保护。"""
        if self._pm is None:
            return
        history = self.state.state_history
        last_count: int = self.state.compression_last_count
        new_count = len(history) - last_count
        if new_count < COMPRESSION_MIN_ENTRIES:
            logger.debug("CharacterEngine flush: 仅 %d 新条目，跳过压缩", new_count)
            return
        if self._compression_lock.locked():
            logger.debug("CharacterEngine flush: 压缩已在运行中")
            return

        async with self._compression_lock:
            try:
                await asyncio.wait_for(self._compress(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CharacterEngine: 压缩 flush 超时")
            except Exception:
                logger.debug("CharacterEngine: 压缩 flush 失败", exc_info=True)

    # ---- 内部状态管理 ----

    def _set_state_l3(self, text: str) -> None:
        """设置 L3 State，追加历史。"""
        self.state.state_l3 = text
        self._append_history(text)

    def _append_state_l3(self, text: str) -> None:
        """追加内容到 L3 State。"""
        current = self.state.state_l3
        new = f"{current}\n{text}" if current else text
        self.state.state_l3 = new
        self._append_history(text)

    def _append_history(self, content: str) -> None:
        """追加条目到 state_history，控制长度。"""
        history = self.state.state_history
        history.append({"timestamp": time.time(), "content": content})
        if len(history) > AWARENESS_MAX_HISTORY:
            del history[:-AWARENESS_MAX_HISTORY]

    # ---- 持久化 ----

    def sync_reflector_to_state(self) -> None:
        """同步 reflector 内部 buffer 到 CharacterState，供 session.save() 前调用。"""
        if self._reflector:
            self.state.reflector_buffer = [
                (u, a) for u, a in self._reflector._buffer
            ]
            self.state.reflector_context = list(self._reflector._context)

    # ---- Reflector 代理 ----

    def buffer_message(self, user_msg: str, assistant_msg: str) -> None:
        """缓冲一轮对话到 CharacterReflector。"""
        if self._reflector:
            asyncio.create_task(self._reflector.buffer(user_msg, assistant_msg))

    async def flush_reflector(self) -> None:
        """Agent shutdown 时调用 — 触发未完成的反思。"""
        if self._reflector:
            await self._reflector.flush()

    # ================================================================
    # 情绪引擎方法（从 EmotionEngine 迁移）
    # ================================================================

    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> Mood:
        """基于 STT 声学情绪标签更新 AI mood（语音输入主路径）。

        Returns: 更新后的 Mood。
        """
        mood = _STT_EMOTION_TO_MOOD.get(emotion_tag)
        if mood is None:
            return self.state.mood

        current = self.state.mood
        intensity = self.state.mood_intensity

        if mood != current:
            self.state.mood = mood
            self.state.mood_intensity = min(1.0, intensity + 0.1)
            self._push_expression(mood, intensity)
            logger.info("心情 (STT %s): %s → %s", emotion_tag, current.value, mood.value)
        else:
            self.state.mood_intensity = min(1.0, intensity + 0.05)

        # 音频事件 → expression 推送
        if events:
            for evt in events:
                if evt == "Laughter":
                    self._schedule(self._output.expression("laugh", intensity=0.7))
                elif evt == "Cry":
                    self._schedule(self._output.expression("cry", intensity=0.8))
                elif evt == "Applause":
                    self._schedule(self._output.expression("surprised", intensity=0.5))

        return mood

    def analyze_user(self, text: str) -> None:
        """Analyze user message and update mood + push expressions (keyword fallback)."""
        detected = self._detect_mood(text)
        if detected == Mood.NEUTRAL:
            return

        current = self.state.mood
        intensity = self.state.mood_intensity

        if detected != current:
            self.state.mood = detected
            self.state.mood_intensity = min(1.0, intensity + 0.1)
            self._push_expression(detected, intensity)
            logger.info("心情 (关键词): %s → %s", current.value, detected.value)
        else:
            self.state.mood_intensity = min(1.0, intensity + 0.05)

    def analyze_assistant(self, _text: str) -> None:
        """After assistant responds, decay mood toward neutral."""
        intensity = self.state.mood_intensity
        new_intensity = max(0.1, intensity - 0.15)
        self.state.mood_intensity = new_intensity
        if new_intensity < 0.2:
            self.state.mood = Mood.NEUTRAL
            self._push_idle()

    async def push_idle_expression(self) -> None:
        """Push a random idle expression (called periodically or on IDLE entry)."""
        self._push_idle()

    # ---- 情绪 internal ----

    def _detect_mood(self, text: str) -> Mood:
        """Simple keyword-based mood detection. Returns the strongest match."""
        scores: dict[str, int] = {}
        for mood_key, (user_kw, _) in MOOD_KEYWORDS.items():
            if mood_key == "neutral":
                continue
            score = sum(1 for kw in user_kw if kw in text)
            if score > 0:
                scores[mood_key] = score
        if not scores:
            return Mood.NEUTRAL
        best_key = max(scores, key=lambda k: scores[k])
        return Mood(best_key)

    def _push_expression(self, mood: Mood, intensity: float) -> None:
        """Push expression to output channels."""
        expr_map = {
            Mood.HAPPY: ("happy", intensity),
            Mood.EXCITED: ("surprised", intensity),
            Mood.SAD: ("sad", intensity),
            Mood.CURIOUS: ("curious", intensity),
        }
        expr, val = expr_map.get(mood, ("neutral", 0.5))
        self._schedule(self._output.expression(expr, intensity=val))

    def _push_idle(self) -> None:
        """Push one of the idle expressions."""
        if self._idle_expressions:
            expr = random.choice(self._idle_expressions)
            self._schedule(self._output.expression(expr, intensity=0.3))

    @staticmethod
    def _schedule(coro) -> None:
        """Schedule a coroutine on the event loop, if one is running.

        Wrapped so exceptions in the fire-and-forget task are logged.
        """
        try:
            loop = asyncio.get_running_loop()

            async def _safe_wrapper():
                try:
                    await coro
                except asyncio.CancelledError:
                    pass
                except Exception:
                    _logger = logging.getLogger(__name__)
                    _logger.warning("CharacterEngine scheduled task 异常", exc_info=True)

            loop.create_task(_safe_wrapper())
        except RuntimeError:
            pass  # no event loop running — test environment, ignore
