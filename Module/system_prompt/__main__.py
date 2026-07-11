"""集成测试：渲染 Section 模板。运行: python -m Module.system_prompt"""

import os

from Module._common.main_runner import run_module_main
from .system_prompt_manager import SystemPromptManager
from .sections.conflict_rules import ConflictRulesSection
from .sections.output_format import OutputFormatSection
from .sections.sensory import SensorySection
from .sections.emotion import EmotionSection
from .sections.memory import MemorySection
from .sections.personality import PersonalitySection
from .sections.relationship import RelationshipSection
from .sections.speaking_style import SpeakingStyleSection
from .sections.proactive import ProactiveSection
from .sections.own_life import OwnLifeSection
from .sections.self_perception import SelfPerceptionSection

_SECTION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


async def main():
    print("Module.system_prompt — Section 模板渲染测试")
    print(f"  模板目录: {_SECTION_DIR}")
    print()

    sections = [
        ConflictRulesSection(),
        OutputFormatSection(f"{_SECTION_DIR}/f2_output_format.j2"),
        SensorySection(f"{_SECTION_DIR}/c5_sensory.j2"),
        EmotionSection(f"{_SECTION_DIR}/c3_emotion.j2"),
        MemorySection(f"{_SECTION_DIR}/c4_memory.j2"),
        PersonalitySection(f"{_SECTION_DIR}/c1_personality.j2", "Elysia"),
        RelationshipSection(f"{_SECTION_DIR}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_SECTION_DIR}/c6_speaking_style.j2"),
        ProactiveSection(f"{_SECTION_DIR}/c7_proactive.j2"),
        OwnLifeSection(f"{_SECTION_DIR}/c8_own_life.j2"),
        SelfPerceptionSection(f"{_SECTION_DIR}/c9_self_perception.j2"),
    ]
    mgr = SystemPromptManager(sections)
    mgr.set_context(user_message="你好", user_name="妖梦", user_pronoun="她")

    try:
        prompt = await mgr.render()
        print(f"总长度: {len(prompt)} 字符")
        print(f"活跃 section: {len([s for s in mgr._sections if (await s.render()).strip()])}/{len(sections)}")
        print()
        for s in mgr._sections:
            result = await s.render()
            status = f"{len(result)} 字符" if result.strip() else "(空)"
            print(f"── [{s.name}] {status} ──")
            if result.strip():
                print(result)
                print()
        print()
        print("校验:")
        failed = mgr.validate_all()
        print(f"  模板语法: {'全部通过' if not failed else f'{len(failed)} 失败: {failed}'}")
        versions = mgr.get_versions()
        print(f"  版本: {len(versions)} sections")
    except Exception as e:
        print(f"  渲染失败: {e}")

    print()
    print("全部测试完成!")


if __name__ == "__main__":
    run_module_main(main)
