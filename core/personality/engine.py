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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from Module.system_prompt import PromptManager
from Module.output import OutputManager
from core.schema.compression import CompressionTrigger
from core.schema._utils import now_iso
from .emotion import EmotionDetector
from .awareness import AwarenessJudge
from ._utils import _extract_tag, _schedule
if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.character import CharacterState, StateHistoryEntry
    from core.storage.character_version_repo import CharacterVersionRepository

logger = logging.getLogger(__name__)

# AwarenessTrigger 配置
AWARENESS_COOLDOWN_SECONDS = 3600  # 同一主题冷却 1 小时，避免频繁输出"我变了"
AWARENESS_MAX_HISTORY = 10         # state_history 保留条数

# L3→L2 压缩配置
COMPRESSION_COUNT_THRESHOLD = 5    # state_history 新增 N 条触发压缩
COMPRESSION_TIME_THRESHOLD = 604800  # 距上次压缩 > 7 天也触发
COMPRESSION_MIN_ENTRIES = 3        # 首次压缩最少需要 state_history 条目数

@dataclass
class C1Data:
    """C1 Personality section 渲染数据。"""
    core_personality: str
    self_personality: str


@dataclass
class C9Data:
    """C9 SelfPerception section 渲染数据。"""
    awareness_text: str


@dataclass
class C6Data:
    """C6 SpeakingStyle section 渲染数据。"""
    speaking_style_c6: str  # "" 表示使用默认模板


class CharacterEngine:
    """角色的性格 + 情绪状态管理器。

    合并了原 PersonalityEngine（L1-L4 性格模型 + 压缩）和
    EmotionEngine（keyword/STT 情绪检测 + expression 推送）。
    """

    # 向后兼容：_schedule 从 _utils 导入，保留为类属性以便测试访问
    _schedule = _schedule

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
        prompt_dir: str | None = None,
        version_repo: "CharacterVersionRepository | None" = None,
    ):
        self._llm = llm
        self.state = state
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality
        self._character = character_config
        self._output = output

        # L3→L2 压缩
        self._compression_lock = asyncio.Lock()
        if prompt_dir is None:
            prompt_dir = str(Path(__file__).resolve().parent.parent / "prompts")
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None

        self._version_repo: "CharacterVersionRepository | None" = version_repo

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

        # 子组件: EmotionDetector
        self._emotion = EmotionDetector(
            state=state,
            output=output,
            idle_expressions=character_config.emotion.idle_expressions,
        )

        # 子组件: AwarenessJudge
        self._awareness = AwarenessJudge(
            llm=llm,
            state=state,
            character_name=character_name,
            user_name=user_name,
            pm=self._pm,
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
        """产出 C9 SelfPerception section 的数据。

        Phase 2: 从 judge 缓存读取，不再直接使用 raw L3。
        加冷却 + 展示次数门控。
        """
        if not self._awareness.is_allowed():
            return C9Data(awareness_text="")

        self._awareness._last_awareness_time = time.time()
        self.state.last_awareness_time = self._awareness._last_awareness_time
        self.state.awareness_show_count += 1
        return C9Data(awareness_text=self.state.approved_awareness_text)

    def render_c6(self) -> C6Data:
        """产出 C6 SpeakingStyle section 的数据。"""
        return C6Data(speaking_style_c6=self.state.speaking_style_c6)

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

        logger.debug("CharacterEngine: consume_trigger source=%s, content=%d chars", source, len(content))

        if source == "reflector":
            # Reflector 产出 → 直接设为 L3 State
            self._set_state_l3(content)
            # 触发异步 awareness 判定
            self._awareness.maybe_judge()

        elif source == "event":
            # 重大事件 → 追加到 L3 State
            self._append_state_l3(content)

        else:
            # idle / feedback → 直接设为 L3 State
            self._set_state_l3(content)

        # L3→L2 压缩检查 — 在状态更新后触发
        await self._maybe_compress()

    # ---- awareness 属性与方法（代理到 AwarenessJudge）----

    @property
    def _last_awareness_time(self) -> float:
        return self._awareness._last_awareness_time

    @_last_awareness_time.setter
    def _last_awareness_time(self, value: float) -> None:
        self._awareness._last_awareness_time = value

    def _awareness_allowed(self) -> bool:
        return self._awareness.is_allowed()

    def _maybe_judge_awareness(self) -> None:
        self._awareness.maybe_judge()

    async def _judge_awareness(self, l3_text: str) -> None:
        if self._pm is None:
            return
        await self._awareness._judge(l3_text)

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

    async def _compress(self, trigger: CompressionTrigger = CompressionTrigger.COMPRESSION) -> None:
        """执行 L3→L2 压缩：渲染模板 → 调 LLM → 解析 <new_self> + <changes> → 写版本历史。"""
        if self._pm is None:
            return

        current_self = self.state.self_l2
        new_entries = self._get_new_entries()
        history = self.state.state_history

        logger.info("CharacterEngine: 开始 L3→L2 压缩 (%d 新条目, 当前 L2 %d chars)",
                    len(new_entries), len(current_self))

        try:
            system = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionSystemPrompt",
                character_name=self._character_name,
                l1_core=self._l1_core,
            )
            user_prompt = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionUserPrompt",
                character_name=self._character_name,
                current_self=current_self,
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

        new_self = _extract_tag(text, "new_self")
        if not new_self:
            logger.warning("CharacterEngine: 压缩 LLM 返回无 <new_self>")
            # 仍更新追踪，避免重复在同一批条目上重试
            self.state.compression_last_at = time.time()
            self.state.compression_last_count = len(history)
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [c.strip("- ") for c in changes_raw.strip().split("\n") if c.strip()]

        # 更新 L2
        self.state.self_l2 = new_self
        self.state.compression_last_at = time.time()
        self.state.compression_last_count = len(history)

        logger.info("CharacterEngine: L3→L2 压缩完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_self), elapsed_ms, len(changes))

        # 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self.state.session_id,
                text=new_self,
                timestamp=now_iso(),
                trigger=trigger,
                change_summary=changes,
            )
        else:
            logger.info("CharacterEngine: version_repo 未配置，版本历史未保存")

        # 触发 C6 刷新
        await self._refresh_c6()

    async def _refresh_c6(self) -> None:
        """L2 更新后刷新 C6 说话风格。LLM 编辑模式 + 版本历史。失败不阻塞。"""
        if self._pm is None:
            return

        # 1. 获取当前 C6 文本
        current = self.state.speaking_style_c6
        if not current:
            # 首次：渲染 L1 默认模板获取基准文本
            try:
                from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
                sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")
                sec.set_context(user_name="")
                current = await sec.render()
            except Exception:
                logger.warning("CharacterEngine: C6 默认模板渲染失败，跳过刷新")
                return

        if not current.strip():
            return

        # 2. 渲染 prompt
        try:
            prompt = self._pm.render(
                "SpeakingStyleEdit.j2",
                character_name=self._character_name,
                self_l2=self.state.self_l2,
                current_speaking_style=current,
            )
        except Exception:
            logger.warning("CharacterEngine: C6 prompt 渲染失败", exc_info=True)
            return

        # 3. LLM 调用（与 _compress() 相同模式）
        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("CharacterEngine: C6 refresh LLM 调用失败", exc_info=True)
            return

        # 4. 解析 XML 标签
        new_style = _extract_tag(text, "new_speaking_style")
        if not new_style:
            logger.warning("CharacterEngine: C6 refresh <new_speaking_style> 解析失败")
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [l.strip("- ").strip() for l in (changes_raw or "").splitlines() if l.strip()]

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 5. 更新 state
        self.state.speaking_style_c6 = new_style

        logger.info("CharacterEngine: C6 刷新完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_style), elapsed_ms, len(changes))

        # 6. 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self.state.session_id,
                text=new_style,
                timestamp=now_iso(),
                trigger=CompressionTrigger.C6_REFRESH,
                change_summary=changes,
                content_type="c6",
            )

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
                await asyncio.wait_for(
                    self._compress(trigger=CompressionTrigger.FLUSH), timeout=5.0)
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
    # 情绪引擎方法（委托给 EmotionDetector）
    # ================================================================

    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> None:
        self._emotion.analyze_user_stt(emotion_tag, events)

    def analyze_user(self, text: str) -> None:
        self._emotion.analyze_user(text)

    def analyze_assistant(self, text: str) -> None:
        self._emotion.analyze_assistant(text)

    async def push_idle_expression(self) -> None:
        await self._emotion.push_idle_expression()

    # ---- 内部委托方法（向后兼容：测试直接访问这些私有 API）----

    def _detect_mood(self, text: str) -> str:
        return self._emotion._detect_mood(text)

    def _push_idle(self) -> None:
        self._emotion._push_idle()

    def _push_expression(self, mood_text: str, intensity: float) -> None:
        self._emotion._push_expression(mood_text, intensity)

    @property
    def _idle_expressions(self):
        return self._emotion._idle_expressions

    @_idle_expressions.setter
    def _idle_expressions(self, value):
        self._emotion._idle_expressions = value

