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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from paths import ProjectPath

from Module.system_prompt import PromptManager
from Module.output import OutputManager
from core.personality.compression import CompressionManager
from core.personality.emotion import EmotionDetector
from core.personality.awareness import AwarenessJudge
from core.schema.compression import CompressionTrigger
from core.personality._utils import _schedule, _extract_tag  # _extract_tag 保持向后兼容
from core.schema.utils import now_iso
if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.config.character_config import CharacterConfig
    from core.memory.session import SessionMemory
    from core.personality.reflector import CharacterReflector
    from core.schema.character import CharacterState
    from core.storage.character_version_repo import CharacterVersionRepository

logger = logging.getLogger(__name__)

# AwarenessTrigger 配置
AWARENESS_COOLDOWN_SECONDS = 3600  # 同一主题冷却 1 小时，避免频繁输出"我变了"
AWARENESS_MAX_HISTORY = 10         # state_history 保留条数

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
        character_config: "CharacterConfig",
        output: OutputManager,
        prompt_dir: str | None = None,
        version_repo: "CharacterVersionRepository | None" = None,
        instance_id: str | None = None,
        character_revision: int = 1,
    ):
        """初始化角色引擎。

        Args:
            llm: LLM 适配器。
            state: 角色状态对象。
            character_name: 角色名。
            user_name: 用户名。
            base_personality: L1 核心性格文本。
            character_config: CharacterConfig 配置对象。
            output: 输出管理器。
            prompt_dir: prompt 模板目录路径，None 时使用默认路径。
            version_repo: 角色版本仓库，用于 L2/C6 版本历史持久化。
            instance_id: 角色实例标识（M1/M2 仅记录标记，不绑定 CharacterRuntime）。
            character_revision: 角色版本号（M1/M2 仅记录标记）。
        """
        self._llm = llm
        self._instance_id = instance_id
        self._character_revision = character_revision
        self.state = state
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality
        self._character = character_config
        self._output = output

        # PromptManager（压缩 + awareness 共用）
        if prompt_dir is None:
            prompt_dir = str(ProjectPath.PROMPTS.resolve())
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None

        # 子组件: CompressionManager
        self._compression = CompressionManager(
            llm=llm,
            state=state,
            character_name=character_name,
            l1_core=base_personality,
            pm=self._pm,
            version_repo=version_repo,
            character_config=character_config,
        )

        self._version_repo: "CharacterVersionRepository | None" = version_repo

        # 子组件: CharacterReflector
        from core.personality.reflector import CharacterReflector
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
        """产出 C1 Personality section 的数据。

        Returns:
            C1Data（core_personality + self_personality）。
        """
        l2 = self.state.self_l2
        return C1Data(
            core_personality=self._l1_core,
            self_personality=l2 if l2 else "",
        )

    def render_c9(self) -> C9Data:
        """产出 C9 SelfPerception section 的数据。

        Phase 2: 从 judge 缓存读取，不再直接使用 raw L3。
        加冷却 + 展示次数门控。

        Returns:
            C9Data（awareness_text，不允许时为空字符串）。
        """
        if not self._awareness.is_allowed():
            return C9Data(awareness_text="")

        self._awareness._last_awareness_time = time.time()
        self.state.last_awareness_time = self._awareness._last_awareness_time
        self.state.awareness_show_count += 1
        return C9Data(awareness_text=self.state.approved_awareness_text)

    def render_c6(self) -> C6Data:
        """产出 C6 SpeakingStyle section 的数据。

        Returns:
            C6Data（speaking_style_c6，空字符串表示使用默认模板）。
        """
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
        """检查当前是否允许 C9 渲染。

        Returns:
            True 表示允许展示觉察文本。
        """
        return self._awareness.is_allowed()

    def _maybe_judge_awareness(self) -> None:
        """触发异步 awareness 判定（fire-and-forget）。"""
        self._awareness.maybe_judge()

    async def _judge_awareness(self, l3_text: str) -> None:
        """执行 LLM 觉察判定。

        Args:
            l3_text: 当前 L3 状态文本。
        """
        if self._pm is None:
            return
        await self._awareness._judge(l3_text)

    # ---- L3→L2 压缩（委托给 CompressionManager）----

    def _should_compress(self) -> bool:
        """检查是否应触发 L3→L2 压缩。

        Returns:
            True 表示满足压缩触发条件。
        """
        return self._compression._should_compress()

    async def _maybe_compress(self) -> None:
        """检查并触发 L3→L2 压缩（如满足条件）。"""
        await self._compression.check_and_compress(_refresh_cb=self._refresh_c6)

    async def _compress(self, trigger: CompressionTrigger = CompressionTrigger.COMPRESSION) -> None:
        """执行 L3→L2 压缩。

        Args:
            trigger: 压缩触发类型（COMPRESSION / FLUSH）。
        """
        await self._compression._compress(trigger, _refresh_cb=self._refresh_c6)

    async def _refresh_c6(self) -> None:
        await self._compression._refresh_c6()

    @property
    def _compression_lock(self):
        return self._compression._compression_lock

    async def flush_compression(self) -> None:
        await self._compression.flush()

    # ---- 内部状态管理 ----

    def _set_state_l3(self, text: str) -> None:
        """设置 L3 State，追加历史。

        Args:
            text: 要设置的 L3 状态文本。
        """
        self.state.state_l3 = text
        self._append_history(text)

    def _append_state_l3(self, text: str) -> None:
        """追加内容到 L3 State。

        Args:
            text: 要追加的内容。
        """
        current = self.state.state_l3
        new = f"{current}\n{text}" if current else text
        self.state.state_l3 = new
        self._append_history(text)

    def _append_history(self, content: str) -> None:
        """追加条目到 state_history，控制长度。

        Args:
            content: 要追加的历史内容。
        """
        history = self.state.state_history
        history.append({"timestamp": now_iso(), "content": content})
        overflow = len(history) - AWARENESS_MAX_HISTORY
        if overflow > 0:
            del history[:-AWARENESS_MAX_HISTORY]
            # 截断时同步调整 compression_last_count，避免计数触发永久失效。
            # 被删除的最旧条目已在 last_count 中计数，需减去等量。
            lc = self.state.compression_last_count
            if lc > 0:
                self.state.compression_last_count = max(0, lc - overflow)

    # ---- 持久化 ----

    def sync_reflector_to_state(self) -> None:
        """同步 reflector 内部 buffer 到 CharacterState，供 session.save() 前调用。"""
        if self._reflector:
            self.state.reflector_buffer = [
                (u, a) for u, a in self._reflector._buffer
            ]
            self.state.reflector_context = list(self._reflector._context)

    def attach_session(self, session: "SessionMemory") -> None:
        """会话切换时重绑 state 视图（M1/M2 阶段）。

        本批（M1/M2）保留为 state 视图换绑 shim——batch-3 多会话 state 隔离依赖它。
        角色身份（instance_id/character_revision）由构造时绑定，attach_session 不再代表
        角色身份切换。完整删除并改由多 instance engine 替代，是 M3 语义。

        具体重绑:
          - self.state 与三个子组件（compression/emotion/awareness）的 _state
          - session._pre_save_hook = sync_reflector_to_state
          - _last_awareness_time（新会话的 awareness 冷却计时，经 property 代理到
            _awareness._last_awareness_time）

        批次 3 review 修复（F2）：换绑前 flush outgoing 的 reflector buffer 到其
        character_state → 清空 live buffer/context → 重绑 → _restore_from_state
        恢复 incoming。flush+clear+restore 只**迁移**在途 buffer，不覆盖——多会话
        常规交替下 A.save()/B.save() 各写各的（A 的 buffer 落 A.cs、B 的落 B.cs），
        杜绝「A.save() 的 _pre_save_hook 把 A 的 buffer 写进 B.character_state」。
        修正原 spec D-o「不 restore」注释（基于 load 已恢复假设；多会话切换下
        flush 后清空使整体替换等价于 restore）。
        """
        # F2：换绑前 flush outgoing 的 reflector buffer/context 到其 character_state
        if self.state is not None and self._reflector is not None:
            self.state.reflector_buffer = [(u, a) for u, a in self._reflector._buffer]
            self.state.reflector_context = list(self._reflector._context)
        # 清空 live buffer/context——随后 restore 整体替换，不残留 outgoing 在途数据
        if self._reflector is not None:
            self._reflector._buffer.clear()
            self._reflector._context.clear()

        new_state = session.character_state
        self.state = new_state
        self._compression._state = new_state
        self._emotion._state = new_state
        self._awareness._state = new_state
        session._pre_save_hook = self.sync_reflector_to_state
        # 同步新会话的 awareness 冷却状态
        self._last_awareness_time = new_state.last_awareness_time or 0.0
        # F2：换绑后从 incoming.character_state 恢复 buffer/context（live 已清空，
        # 恢复只迁移不覆盖）
        if self._reflector is not None and hasattr(self._reflector, "_restore_from_state"):
            self._reflector._restore_from_state()

    # ---- Reflector 代理 ----

    async def _safe_buffer(self, user_msg: str, assistant_msg: str,
                           reflector: "CharacterReflector | None" = None) -> None:
        """安全缓冲：捕获 reflector.buffer 异常，避免 fire-and-forget task 异常丢失。

        C3/P1-10：target 在调用时固化（发起会话的 reflector）——session switch
        rebind engine._reflector 不影响在途 buffer 的目标。

        Args:
            user_msg: 用户消息文本。
            assistant_msg: 助手回复文本。
            reflector: 显式目标 reflector；None 回退 self._reflector。
        """
        target = reflector if reflector is not None else self._reflector
        if target is None:
            return
        try:
            await target.buffer(user_msg, assistant_msg)
        except Exception:
            logger.warning("buffer_message: reflector.buffer 失败", exc_info=True)

    def buffer_message(self, user_msg: str, assistant_msg: str,
                       reflector: "CharacterReflector | None" = None
                       ) -> "asyncio.Task | None":
        """缓冲一轮对话到 CharacterReflector。

        C3/P1-10：reflector 在调用时固化（发起会话的 reflector）——跨 session 切换
        的 in-flight after-response 不写到新会话的 reflector；返回 task 供调用方
        统一登记 tail registry（不再内部创建不可见 task）。

        Args:
            user_msg: 用户消息文本。
            assistant_msg: 助手回复文本。
            reflector: 显式目标 reflector（发起会话固化）；None 回退 self._reflector。

        Returns:
            缓冲 task（调用方登记统一 tail registry），无 reflector 时 None。
        """
        target = reflector if reflector is not None else self._reflector
        if target is None:
            return None
        return asyncio.create_task(
            self._safe_buffer(user_msg, assistant_msg, reflector=target))

    async def flush_reflector(self) -> None:
        """Agent shutdown 时调用 — 触发未完成的反思。"""
        if self._reflector:
            await self._reflector.flush()

    # ================================================================
    # 情绪引擎方法（委托给 EmotionDetector）
    # ================================================================

    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> None:
        """处理 STT 音频情绪事件。

        Args:
            emotion_tag: 音频识别的情绪标签。
            events: 音频事件列表（如 Laughter、Cry、Applause）。
        """
        self._emotion.analyze_user_stt(emotion_tag, events)

    def analyze_user(self, text: str) -> None:
        """关键词检测用户消息中的情绪。

        Args:
            text: 用户消息文本。
        """
        self._emotion.analyze_user(text)

    def analyze_assistant(self, text: str) -> None:
        """衰减助手回复后的情绪强度。

        Args:
            text: 助手回复文本（当前未使用，保留接口兼容）。
        """
        self._emotion.analyze_assistant(text)

    async def push_idle_expression(self) -> None:
        await self._emotion.push_idle_expression()

    # ---- 内部委托方法（向后兼容：测试直接访问这些私有 API）----

    def _detect_mood(self, text: str) -> str:
        """关键词情绪检测。

        Args:
            text: 要检测的文本。

        Returns:
            检测到的情绪标签字符串。
        """
        return self._emotion._detect_mood(text)

    def _push_idle(self) -> None:
        """推送随机空闲表情。"""
        self._emotion._push_idle()

    def _push_expression(self, mood_text: str, intensity: float) -> None:
        """推送表情到输出通道。

        Args:
            mood_text: 情绪描述文本。
            intensity: 情绪强度（0.0-1.0）。
        """
        self._emotion._push_expression(mood_text, intensity)

    @property
    def _idle_expressions(self):
        return self._emotion._idle_expressions

    @_idle_expressions.setter
    def _idle_expressions(self, value: list[str]):
        self._emotion._idle_expressions = value

