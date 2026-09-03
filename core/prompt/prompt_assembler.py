"""PromptAssembler —— 构建发送给 LLM 的最终 messages[]。

胶水层：创建 PromptSection 实例、接入 SystemPromptManager，
并将消息组装委托给 MessageBuilder。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.personality.engine import CharacterEngine
    from core.cognitive.turn_context import TurnCharacterContext

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
from core.utils.text_utils import content_text_from_content as content_text_from_raw

logger = logging.getLogger(__name__)

from paths import ProjectPath
_SECTION = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())


class PromptAssembler:
    """胶水层：创建 section，委托给 SystemPromptManager。"""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
        personality_engine: "CharacterEngine | None" = None,
    ):
        """初始化 PromptAssembler，创建所有 system prompt section 和管理器。

        Args:
            character: 角色配置。
            session: 会话记忆实例。
            long_term: 长期记忆实例。
            personality_engine: 人格引擎实例（可选），用于获取 mood、C1/C6/C9 数据。
        """
        self._character = character
        self._session = session
        self._long_term = long_term
        self._personality_engine = personality_engine
        # Task 7（B1）：_prepare_turn 冻结的回合上下文（含 userPronoun），由 loop 注入
        self._turn_context: TurnCharacterContext | None = None

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

        sections = [
            ConflictRulesSection(f"{_SECTION}/f1_conflict_rules.j2"),
            OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
            TTSMarkupSection(f"{_SECTION}/f3_tts_markup.j2"),
            SensorySection(f"{_SECTION}/c5_sensory.j2"),
            self._emotion_section,
            self._memory_section,
            self._personality_section,
            RelationshipSection(self._resolve_template("c2_relationship.j2"), character.name),
            SpeakingStyleSection(f"{_SECTION}/c6_speaking_style.j2", speaking_style=character.speaking_style),
            ProactiveSection(self._resolve_template("c7_proactive.j2")),
            self._own_life_section,
            self._self_perception_section,
        ]
        self._manager = SystemPromptManager(sections)
        self._builder = MessageBuilder()

    def _resolve_template(self, name: str) -> str:
        """按角色名解析模板路径：角色目录存在则用，否则回退共享。"""
        role_dir = f"{_SECTION}/{self._character.name.lower()}/{name}"
        if Path(role_dir).exists():
            return role_dir
        return f"{_SECTION}/{name}"

    def set_session(self, session: SessionMemory) -> None:
        """会话切换时重绑 session 引用（本对象 + 三个会话依赖 section）。

        批次 3 (multi-session): EmotionSection/OwnLifeSection/MemorySection
        在渲染时读取 session 内容，切换后必须指向新会话对象。
        """
        self._session = session
        self._emotion_section._session = session
        self._memory_section._session = session
        self._own_life_section._session = session

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
                                   tts_provider: str = "") -> str:
        """构建渲染后的 system prompt 字符串。

        Args:
            user_message: 当前用户消息文本。
            user_name: 用户名。
            stt_emotion: STT 识别的情感标签。
            stt_events: STT 识别的事件列表。
            tts_provider: TTS 提供商名称。

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
        self._manager.set_context(
            user_message=user_message,
            user_name=user_name,
            user_pronoun=self._character.user_pronoun,
            stt_emotion=stt_emotion,
            stt_events=stt_events or [],
            tts_provider=tts_provider,
            mood=mood,
            speaking_style_c6=speaking_style_c6,
        )
        self._refresh_personality_data()
        return await self._manager.render()

    async def build_messages(self, user_message: str = "",
                              user_name: str = "",
                              stt_emotion: str = "",
                              stt_events: list | None = None,
                              content: list[dict] | str | None = None,
                              supports_multimodal: bool = False,
                              tts_provider: str = "") -> list[dict]:
        """构建发送给 LLM 的完整消息列表。

        Args:
            user_message: 当前用户消息文本。
            user_name: 用户名。
            stt_emotion: STT 识别的情感标签。
            stt_events: STT 识别的事件列表。
            content: 多模态内容（如图片），为 None 时使用 user_message。
            supports_multimodal: 当前模型是否支持多模态。
            tts_provider: TTS 提供商名称。

        Returns:
            LLM API 的 messages 列表，包含 system prompt + 历史 + 用户消息。
        """
        text = content_text_from_raw(content) if content else user_message
        system = await self.build_system_prompt(
            user_message=text, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
            tts_provider=tts_provider,
        )
        history = self._session.recent_messages(8)
        user_content = content if content else user_message
        return self._builder.build_messages(
            system, history, user_content,
            supports_multimodal=supports_multimodal,
        )
