"""PromptAssembler —— 构建发送给 LLM 的最终 messages[]。

胶水层：创建 PromptSection 实例、接入 SystemPromptManager，
并将消息组装委托给 MessageBuilder。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.personality.protocol import CharacterEngineProtocol
    from core.cognitive.turn_context import TurnCharacterContext
    from core.character.runtime_profile import RuntimeCharacterProfile

from core.prompt.message_builder import MessageBuilder
from Module.system_prompt.system_prompt_manager import SystemPromptManager
from Module.system_prompt.sections.conflict_rules import ConflictRulesSection
from Module.system_prompt.sections.output_format import OutputFormatSection
from Module.system_prompt.sections.tts_markup import TTSMarkupSection
from Module.system_prompt.sections.sensory import SensorySection
from Module.system_prompt.sections.emotion import EmotionSection
from Module.system_prompt.sections.memory import MemorySection
from Module.system_prompt.sections.personality import PersonalitySection
from Module.system_prompt.sections.relationship import RelationshipSection
from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
from Module.system_prompt.sections.proactive import ProactiveSection
from Module.system_prompt.sections.own_life import OwnLifeSection
from Module.system_prompt.sections.self_perception import SelfPerceptionSection
from core.config.character_config import CharacterConfig
from core.memory.session import SessionMemory
from core.memory.long_term import LongTermMemory
from core.profile.user_identity import (
    LOCAL_DEFAULT_PRONOMS,
    LOCAL_DEFAULT_TIMEZONE,
)
from core.utils.text_utils import content_text_from_content as content_text_from_raw

logger = logging.getLogger(__name__)

from paths import ProjectPath
_SECTION = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())


class PromptAssembler:
    """胶水层：创建 section，委托给 SystemPromptManager。"""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory | None,
        long_term: LongTermMemory | None,
        personality_engine: "CharacterEngineProtocol | None" = None,
    ):
        """初始化 PromptAssembler，创建所有 system prompt section 和管理器。

        Args:
            character: 角色配置。
            session: 会话记忆实例（无状态预览传 None——会话依赖 section 空渲染）。
            long_term: 长期记忆实例（预览传 None 即禁用 C4 检索）。
            personality_engine: 人格引擎实例（可选），用于获取 mood、C1/C6/C9 数据。
        """
        self._character = character
        self._session = session
        self._long_term = long_term
        self._personality_engine = personality_engine
        # Task 7（B1）：_prepare_turn 冻结的回合上下文（含用户身份快照），由 loop 注入
        self._turn_context: TurnCharacterContext | None = None
        # BE-STAGEH-03：无状态预览模式——不读会话历史
        self._preview_mode = False

        self._personality_section = PersonalitySection(
            f"{_SECTION}/c1_personality.j2",
            character_name=character.name,
            core_personality=character.base_personality,
        )
        self._self_perception_section = SelfPerceptionSection(
            f"{_SECTION}/c9_self_perception.j2",
        )

        # 会话依赖的 section — set_session 时重绑 session 引用
        self._emotion_section = EmotionSection(self._resolve_template("c3_emotion.j2"), session)
        self._memory_section = MemorySection(f"{_SECTION}/c4_memory.j2", long_term, session)
        self._own_life_section = OwnLifeSection(f"{_SECTION}/c8_own_life.j2", session)
        self._relationship_section = RelationshipSection(
            self._resolve_template("c2_relationship.j2"), character.name
        )
        self._speaking_style_section = SpeakingStyleSection(
            f"{_SECTION}/c6_speaking_style.j2",
            speaking_style=character.speaking_style,
        )
        self._proactive_section = ProactiveSection(
            self._resolve_template("c7_proactive.j2")
        )

        sections = [
            ConflictRulesSection(f"{_SECTION}/f1_conflict_rules.j2"),
            OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
            TTSMarkupSection(f"{_SECTION}/f3_tts_markup.j2"),
            SensorySection(f"{_SECTION}/c5_sensory.j2"),
            self._emotion_section,
            self._memory_section,
            self._personality_section,
            self._relationship_section,
            self._speaking_style_section,
            self._proactive_section,
            self._own_life_section,
            self._self_perception_section,
        ]
        self._manager = SystemPromptManager(sections)
        self._builder = MessageBuilder()

    @property
    def character_name(self) -> str:
        """当前角色名（公开只读）。

        跨模块消费面：`CognitiveLoop` 构造 TTS 流管理器时需要角色名做
        per-character 配置。此前经 `prompt._character.name` 私有穿透读取。
        """
        return self._character.name if self._character else "unknown"

    def _resolve_template(self, name: str) -> str:
        """按角色名解析模板路径：角色目录存在则用，否则回退共享。"""
        role_dir = f"{_SECTION}/{self._character.name.lower()}/{name}"
        if Path(role_dir).exists():
            return role_dir
        return f"{_SECTION}/{name}"

    def set_personality_engine(self, engine: "CharacterEngineProtocol") -> None:
        """更换人格引擎实例（会话切换后 C1/C6/C9 数据来源随角色更新）。"""
        self._personality_engine = engine

    def enable_preview_mode(self) -> None:
        """BE-STAGEH-03：切到无状态预览——`build_messages` 不再读会话历史。

        会话依赖 section（C3/C4/C8）在 session/long_term 为 None 时自行空渲染，
        因此预览不会读到任何真实会话或长期记忆。
        """
        self._preview_mode = True

    @staticmethod
    def _switch_template(section: Any, template_path: str) -> None:
        """Switch one trusted local template and invalidate its source cache.

        缓存失效是 `Jinja2Section` 的内部不变式，经其公开 `switch_template()`
        完成——消费方不再戳 `_template_path` / `_cached_template`。
        """
        section.switch_template(template_path)

    @staticmethod
    def _runtime_template(role: str | None, name: str) -> str:
        if role:
            candidate = f"{_SECTION}/{role}/{name}"
            if Path(candidate).exists():
                return candidate
        return f"{_SECTION}/custom/{name}"

    def set_character(
        self,
        character_id: str,
        display_name: str,
        profile: "RuntimeCharacterProfile | None" = None,
    ) -> None:
        """Bind all authored/static prompt inputs to the turn's exact revision.

        ``profile=None`` preserves the legacy name-only call for older tests and
        adapters.  Runtime turns always provide a compiled profile.
        """
        self._personality_section.character_name = display_name
        if profile is None:
            return

        self._character = profile.config
        role = profile.builtin_template_role
        authored = role is None
        self._personality_section.set_authored_profile(
            authored=authored,
            examples=tuple(
                (example.user, example.assistant) for example in profile.examples
            ) if authored else (),
        )
        self._speaking_style_section.set_character_profile(
            profile.config.speaking_style,
            authored=authored,
        )

        self._switch_template(
            self._relationship_section,
            self._runtime_template(role, "c2_relationship.j2"),
        )
        self._switch_template(
            self._emotion_section,
            self._runtime_template(role, "c3_emotion.j2"),
        )
        self._switch_template(
            self._proactive_section,
            self._runtime_template(role, "c7_proactive.j2"),
        )
        self._relationship_section.set_character_profile(
            name=display_name,
            relationship_style=(profile.relationship_style if authored else ""),
        )
        self._emotion_section.set_character_profile(
            profile.emotion_style if authored else ""
        )
        self._proactive_section.set_character_profile(
            profile.conversation_initiative if authored else ""
        )

    def set_session(self, session: SessionMemory) -> None:
        """会话切换时重绑 session 引用（本对象 + 三个会话依赖 section）。

        批次 3 (multi-session): EmotionSection/OwnLifeSection/MemorySection
        在渲染时读取 session 内容，切换后必须指向新会话对象。
        """
        self._session = session
        self._emotion_section._session = session
        self._memory_section._session = session
        self._own_life_section._session = session

    def set_memory_view(self, view: Any) -> None:
        """_prepare_turn 注入本回合 scoped memory view（阶段 F §2.4 C4）。

        与 set_session 重绑 _memory_section._session 同构，这里重绑 _long_term 为
        runtime.memory_view（ScopedLongTermMemory，按本回合 instance scope 过滤）。
        MemorySection 保持 duck-typed（不 import core.memory），search 签名兼容。
        """
        self._memory_section._long_term = view  # noqa: SLF001 — MemorySection 字段

    def _refresh_personality_data(self) -> None:
        """从 PersonalityEngine 拉取最新数据，更新 C1/C9 section。"""
        if self._personality_engine is None:
            return
        c1 = self._personality_engine.render_c1()
        self._personality_section.update_personality(
            core=c1.core_personality,
            self_p=c1.self_personality,
        )
        c9 = self._personality_engine.render_c9()
        self._self_perception_section.update_awareness(
            text=c9.awareness_text,
        )

    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                                   stt_emotion: str = "", stt_events: list | None = None,
                                   tts_provider: str = "",
                                   user_pronoun_override: str | None = None,
                                   user_timezone_override: str | None = None,
                                   prompt_budget_tokens: int | None = None) -> str:
        """构建渲染后的 system prompt 字符串。

        Args:
            user_message: 当前用户消息文本。
            user_name: 用户名（无回合上下文时的回退值）。
            stt_emotion: STT 识别的情感标签。
            stt_events: STT 识别的事件列表。
            tts_provider: TTS 提供商名称。
            user_pronoun_override: 显式用户代词（预览用；优先于 _turn_context）。
            user_timezone_override: 显式用户时区（预览用；优先于默认，弱于 _turn_context）。
            prompt_budget_tokens: prompt 总预算；system prompt 只取其中一份
                （`budget.system_prompt_budget`），其余留给历史与当前消息（S2-H9）。

        Returns:
            渲染后的完整 system prompt 字符串。
        """
        # 从 CharacterEngine 获取当前 mood 和 C6 数据
        mood = "neutral"
        speaking_style_c6 = ""
        if self._personality_engine is not None:
            mood = self._personality_engine.state.mood
            c6 = self._personality_engine.render_c6()
            speaking_style_c6 = c6.speaking_style_c6
        ctx = self._turn_context
        # P0-2：本轮身份快照优先——称呼（preferredAddress 优先，否则 displayName）、
        # 代词、时区一律取自回合冻结的 UserIdentity，不再读 config.agent.user_name。
        if ctx is not None:
            user_name = ctx.effectiveUserAddress
        # B1 正位：优先读 _turn_context.userPronouns（Task 6/7 冻结的全局唯一她），
        # 否则兜底常量「她」——不回退 _character.user_pronoun（spec §5.2 无 per-role 回退）。
        user_pronoun = (
            user_pronoun_override
            or (ctx.userPronouns if ctx is not None and ctx.userPronouns else None)
            or LOCAL_DEFAULT_PRONOMS
        )
        user_timezone = (
            (ctx.userTimezone if ctx is not None and ctx.userTimezone else None)
            or user_timezone_override
            or LOCAL_DEFAULT_TIMEZONE
        )
        self._manager.set_context(
            user_message=user_message,
            user_name=user_name,
            user_pronoun=user_pronoun,
            user_timezone=user_timezone,
            stt_emotion=stt_emotion,
            stt_events=stt_events or [],
            tts_provider=tts_provider,
            mood=mood,
            speaking_style_c6=speaking_style_c6,
        )
        self._refresh_personality_data()
        if prompt_budget_tokens is None:
            return await self._manager.render()
        from Module.system_prompt.budget import system_prompt_budget

        return await self._manager.render(
            budget_tokens=system_prompt_budget(prompt_budget_tokens))

    async def build_messages(self, user_message: str = "",
                              user_name: str = "",
                              stt_emotion: str = "",
                              stt_events: list | None = None,
                              content: list[dict] | str | None = None,
                              supports_multimodal: bool = False,
                              tts_provider: str = "",
                              user_pronoun_override: str | None = None,
                              user_timezone_override: str | None = None,
                              prompt_budget_tokens: int | None = None) -> list[dict]:
        """构建发送给 LLM 的完整消息列表。

        Args:
            user_message: 当前用户消息文本。
            user_name: 用户名（无回合上下文时的回退值）。
            stt_emotion: STT 识别的情感标签。
            stt_events: STT 识别的事件列表。
            content: 多模态内容（如图片），为 None 时使用 user_message。
            supports_multimodal: 当前模型是否支持多模态。
            tts_provider: TTS 提供商名称。
            user_pronoun_override: 显式用户代词（预览用）。
            user_timezone_override: 显式用户时区（预览用）。
            prompt_budget_tokens: prompt 总预算（S2-H9）。超预算时先裁 system
                prompt 的弹性节（C4 记忆），再按成对单元由旧到新裁历史；
                当前用户消息永不丢。None = 不设预算（预览/无模型上下文的调用点）。

        Returns:
            LLM API 的 messages 列表，包含 system prompt + 历史 + 用户消息。
        """
        text = content_text_from_raw(content) if content else user_message
        system = await self.build_system_prompt(
            user_message=text, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
            tts_provider=tts_provider,
            user_pronoun_override=user_pronoun_override,
            user_timezone_override=user_timezone_override,
            prompt_budget_tokens=prompt_budget_tokens,
        )
        # BE-STAGEH-03：预览模式不读任何会话历史；无 session 时同样空历史
        history = (
            []
            if self._preview_mode or self._session is None
            else self._session.recent_messages(8)
        )
        user_content = content if content else user_message
        return self._builder.build_messages(
            system, history, user_content,
            supports_multimodal=supports_multimodal,
            prompt_budget_tokens=prompt_budget_tokens,
        )
