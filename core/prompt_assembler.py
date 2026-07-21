"""PromptAssembler — builds the final messages[] for LLM calls.

Glue layer: creates PromptSection instances, wires them into
SystemPromptManager, delegates message assembly to MessageBuilder.
"""

from __future__ import annotations

import logging

from Module.system_prompt.message_builder import MessageBuilder
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
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory
from .text.text_utils import content_text_from_content as content_text_from_raw

logger = logging.getLogger(__name__)

_SECTION = "Module/system_prompt/templates"


class PromptAssembler:
    """Glue layer: creates sections, delegates to SystemPromptManager."""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
        personality_engine=None,  # PersonalityEngine | None
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._personality_engine = personality_engine

        self._personality_section = PersonalitySection(
            f"{_SECTION}/c1_personality.j2",
            character_name=character.name,
            core_personality=character.base_personality,
        )
        self._self_perception_section = SelfPerceptionSection(
            f"{_SECTION}/c9_self_perception.j2",
        )

        sections = [
            ConflictRulesSection(f"{_SECTION}/f1_conflict_rules.j2"),
            OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
            TTSMarkupSection(f"{_SECTION}/f3_tts_markup.j2"),
            SensorySection(f"{_SECTION}/c5_sensory.j2"),
            EmotionSection(f"{_SECTION}/c3_emotion.j2", session),
            MemorySection(f"{_SECTION}/c4_memory.j2", long_term, session),
            self._personality_section,
            RelationshipSection(f"{_SECTION}/c2_relationship.j2", character.name),
            SpeakingStyleSection(f"{_SECTION}/c6_speaking_style.j2"),
            ProactiveSection(f"{_SECTION}/c7_proactive.j2"),
            OwnLifeSection(f"{_SECTION}/c8_own_life.j2", session),
            self._self_perception_section,
        ]
        self._manager = SystemPromptManager(sections)
        self._builder = MessageBuilder()

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
                                   stt_emotion: str = "", stt_events=None,
                                   tts_provider: str = "") -> str:
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
                              stt_events=None,
                              content=None,
                              supports_multimodal: bool = False,
                              tts_provider: str = "") -> list[dict]:
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
