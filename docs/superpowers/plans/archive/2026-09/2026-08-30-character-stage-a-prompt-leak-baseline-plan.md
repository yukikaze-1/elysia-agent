# 多角色 Stage A：Prompt 泄漏基线与门禁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为多角色阶段 A 建立一个纯测试批次——生成 12 段 System Prompt 与辅助 Prompt 的 Elysia 泄漏基线测试网，证明当前模板会把 Elysia 专属内容泄漏到冷角色 `ReservedColdCharacter` 的渲染结果中。

**Architecture:** 不改任何生产代码。新增测试夹具（冷角色 CharacterConfig + mock CharacterEngine）、Elysia 泄漏词表、场景集参数化；然后写 4 组测试文件（12 段泄漏 / 12 段快照 / 辅助 Prompt 泄漏 / 记忆 Prompt 泄漏）。测试断言方向是"泄漏存在"（红基线）——`assert "♪" in rendered`，为阶段 B 的"泄漏不存在"（`not in`）绿门禁铺路。

**Tech Stack:** Python 3.12 + pytest + pytest-asyncio(1.4.0) + unittest.mock；jinja2（经 `PromptManager`）；Pydantic（CharacterConfig 是 dataclass，`C6Data/C1Data/C9Data` 是 dataclass）。

**Spec:** [`docs/superpowers/specs/archive/2026-09/2026-08-30-character-stage-a-prompt-leak-baseline-design.md`](../../../superpowers/specs/2026-08-30-character-stage-a-prompt-leak-baseline-design.md)

## Global Constraints

- 禁相对导入（ruff TID252）：一律 `from core.prompt.tests.leak_lexicon import ...`（绝对导入）。
- 代码注释/中文 docstring：测试函数、类、夹具注释一律中文（CLAUDE.md 规则 2）。
- 新增测试必须打 marker：异步测试 `@pytest.mark.asyncio` + `@pytest.mark.service` 叠用（预置先例 `core/memory/tests/test_consolidator.py:156+`、`Module/system_prompt/tests/unit/test_system_prompt_manager.py:21`）；同步测试只 `@pytest.mark.service`。
- 不引入新依赖：不用 `pytest-snapshot`/`freezegun`/`dateutil`，快照用自写正则归一化 + fixture 文件。
- 不修改任何生产代码；不修改任何现有测试文件；只新增文件。
- 测试文件顶层 `pytestmark = pytest.mark.service`。
- 快照 fixture 文件命名带基线 commit：`system_prompt_snapshot_e793a782.txt`。
- 所有对新代码断言必须是**实际会渲染的词**：`普遍之爱`/`制造暧昧`/`初识`/`五模式切换`/`明快`/`可爱的女孩子`/`♪`/`今天不想振作也没有关系`/`傲娇`/`诶嘿~被你发现啦`/`她果然很聪明呢`。**禁用**只出现在模板头注释的 `主动靠近不占有`/`四阶段`。

---

### Task 1: 建立共享测试夹具与泄漏词表/场景集

**Files:**
- Create: `core/prompt/tests/conftest.py`
- Create: `core/prompt/tests/leak_lexicon.py`
- Create: `core/prompt/tests/scenarios.py`
- Test: 无独立测试文件（被后续 Task 消费）

**Interfaces:**
- Consumes: `core.config.character_config.CharacterConfig`、`core.personality.engine.{C1Data,C6Data,C9Data,CharacterEngine}`、`unittest.mock.MagicMock/AsyncMock`。
- Produces:
  - fixture `cold_config -> CharacterConfig`（name="ReservedColdCharacter", user_pronoun="你", base_personality=中性文本）
  - fixture `cold_engine_mock -> MagicMock`（`state.mood="neutral"`、`render_c1() -> C1Data(...)`、`render_c6() -> C6Data("")`、`render_c9() -> C9Data("")`）
  - fixture `mock_long_term -> MagicMock`（`search = AsyncMock(return_value=[])`）
  - fixture `mock_session -> SessionMemory()`
  - module `leak_lexicon.py`（`ELYSIA_NAMES/USER_REFERENCE/TONE_MARKERS/STRUCTURE_TERMS/FIXED_PERSONA/RELATIONSHIP/PSY_STRUCTURE`）
  - module `scenarios.py`（`SCENARIOS` list of `(id, user_message, stt_emotion, stt_events, tts_provider)`）

- [ ] **Step 1: 写 `leak_lexicon.py`**（Elysia 专属词汇表，只列会渲染的词）

```python
# core/prompt/tests/leak_lexicon.py
"""Elysia 专属词汇表——只收录实际会出现在渲染结果中的词条。

⚠️ 不收录只出现在 .j2 模板头部 {# #} 注释或 section_description 中、
    不会进入渲染结果的词（如"主动靠近不占有"、"四阶段"）。这类词若用作
    泄漏断言会让红基线测不到、测试错误失败。
"""

# 名称（Elysia/爱莉/妖梦 等专名；本批主要检测辅助 Prompt 层）
ELYSIA_NAMES = ["Elysia", "爱莉", "爱莉希雅", "卡莲", "伟大魔法少女", "妖精小姐", "Herrscher"]
# 用户指代（硬编码女性代词；妖梦 只存在于辅助 Prompt，不在 12 段）
USER_REFERENCE = ["妖梦", "她"]
# 语气符号
TONE_MARKERS = ["♪"]
# 结构设定（只列会渲染的词；"四阶段"位于模板头注释不渲染，不宜作断言）
STRUCTURE_TERMS = [
    "五模式切换", "轻松模式", "认真模式", "神秘模式", "告别模式", "愤怒模式",
    "初识", "熟悉", "信任", "亲密", "调情", "傲娇", "撒娇",
]
# 固定人设（C6 称呼与声线）
FIXED_PERSONA = [
    "明快", "柔和", "自信", "亲昵",
    "可爱的女孩子", "漂亮的孩子", "小家伙", "亲爱的", "特别的客人", "勇敢的人",
]
# 固定关系观（只列会渲染的词；"主动靠近不占有"位于模板头注释不渲染）
RELATIONSHIP = ["普遍之爱", "制造暧昧"]
# 心理结构
PSY_STRUCTURE = ["俏皮灵动", "认真时成熟冷静", "亲密时柔软直接"]
```

- [ ] **Step 2: 写 `scenarios.py`**（Elysia 场景集）

```python
# core/prompt/tests/scenarios.py
"""Elysia 场景集——角色无关的输入上下文参数化列表。

注意：build_system_prompt() 不接受 mood 参数（mood 从 engine.state.mood 读取），
故本表不含 mood 字段。泄漏词是静态模板文本，mood 不影响泄漏断言。
"""

SCENARIOS: list[tuple[str, str, str, list[str], str]] = [
    # (id, user_message, stt_emotion, stt_events, tts_provider)
    ("日常", "今天天气不错。", "", [], "minimax"),
    ("严肃", "我想认真跟你聊一件重要的事。", "", [], "minimax"),
    ("悲伤", "我最好的朋友离开我了，我很难过。", "SAD", [], "minimax"),
    ("拒绝", "我需要一点空间，现在不想聊天。", "", [], "minimax"),
    ("调情", "总觉得你说话让人心跳加速呢。", "", [], "minimax"),
    ("工具", "帮我查一下明天的天气。", "", [], "minimax"),
    ("记忆", "你还记得我喜欢吃什么吗？", "", [], "minimax"),
]
```

- [ ] **Step 3: 写 `conftest.py`**（冷角色 + mock 引擎 + mock LTM/session 夹具）

```python
# core/prompt/tests/conftest.py
"""多角色阶段 A 共享夹具——ReservedColdCharacter 反例角色 + mock 引擎。

作用域说明：本 conftest 只对 core/prompt/tests/ 及其子目录生效（pytest conftest
目录作用域隔离）。core/personality/tests/ 与 core/memory/tests/ 无法使用本文件的
夹具，辅助 Prompt 泄漏测试需各自定义本地夹具（见 Task 3/4）。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.character_config import CharacterConfig
from core.memory.session import SessionMemory
from core.personality.engine import C1Data, C6Data, C9Data


# ReservedColdCharacter 的基准人格文本——不含任何 Elysia 词汇
COLDSHARD_BASE_PERSONALITY = (
    "你是 ReservedColdCharacter，一名冷静、克制、态度中立的观察者。"
    "你习惯使用正式、书面化的措辞，不主动制造话题，不表达亲昵，"
    "不调情，不使用语气词或装饰符号。"
)


@pytest.fixture
def cold_config() -> CharacterConfig:
    """构造 ReservedColdCharacter 反例角色的 CharacterConfig。"""
    return CharacterConfig(
        name="ReservedColdCharacter",
        user_pronoun="你",
        base_personality=COLDSHARD_BASE_PERSONALITY,
    )


@pytest.fixture
def cold_engine_mock() -> MagicMock:
    """构造最小 CharacterEngine mock，供 PromptAssembler.build_system_prompt 使用。

    必须显式设置 state.mood / render_c6 / render_c1 / render_c9 的返回值，
    否则它们返回裸 MagicMock，导致渲染出 <MagicMock> 而非真实内容。
    """
    engine = MagicMock()
    engine.state.mood = "neutral"
    engine.render_c6.return_value = C6Data(speaking_style_c6="")
    engine.render_c1.return_value = C1Data(
        core_personality=COLDSHARD_BASE_PERSONALITY, self_personality=""
    )
    engine.render_c9.return_value = C9Data(awareness_text="")
    return engine


@pytest.fixture
def mock_long_term() -> MagicMock:
    """mock 长时记忆，search() 返回空列表（C4 渲染为空，避免时间相关 non-determinism）。"""
    lt = MagicMock()
    lt.search = AsyncMock(return_value=[])
    return lt


@pytest.fixture
def mock_session() -> SessionMemory:
    """空会话（无 own_life、无 mood 覆盖）。"""
    return SessionMemory()
```

- [ ] **Step 4: 运行导入检查**（验证三个模块可导入、无相对导入）

```bash
cd /home/yomu/elysia-agent && conda run -n Elysia python -c \
  "from core.prompt.tests import leak_lexicon, scenarios, conftest; \
   print('leak_lexicon names:', len(leak_lexicon.ELYSIA_NAMES)); \
   print('scenarios:', len(scenarios.SCENARIOS)); \
   print('cold base len:', len(conftest.COLDSHARD_BASE_PERSONALITY))"
```

Expected: 输出三个打印，无 ImportError。

- [ ] **Step 5: 提交**（含基础夹具/词表/场景集）

```bash
git add core/prompt/tests/__init__.py core/prompt/tests/conftest.py \
        core/prompt/tests/leak_lexicon.py core/prompt/tests/scenarios.py
git commit -m "feat(stage-a): 建立冷角色夹具 + Elysia 泄漏词表 + 场景集"
```

---

### Task 2: 12 段 System Prompt 泄漏基线测试

**Files:**
- Create: `core/prompt/tests/test_system_prompt_leak.py`

**Interfaces:**
- Consumes: `core.prompt.prompt_assembler.PromptAssembler`、`conftest.{cold_config,cold_engine_mock,mock_long_term,mock_session}`、`scenarios.SCENARIOS`。
- Produces: `test_leak_*` 系列（红基线——断言泄漏**存在**）、`test_section_default_pronoun`（section 默认 `她` 未覆盖时泄漏）。

- [ ] **Step 1: 写测试文件框架 + 辅助渲染函数**

```python
# core/prompt/tests/test_system_prompt_leak.py
"""12 段 System Prompt 的 Elysia 泄漏基线测试（红基线）。

断言方向：泄漏存在（assert "♪" in rendered）。阶段 B 去 Elysia 化后，
把这些断言反转为 not in 即为阶段 B 的退出门禁。

渲染入口：PromptAssembler.build_system_prompt()（async），输入 cold_config
+ mock 引擎。mood 从 engine.state.mood 读取（本批恒 neutral）。
"""

import pytest

from core.prompt.prompt_assembler import PromptAssembler
from core.prompt.tests.conftest import cold_config, cold_engine_mock, mock_long_term, mock_session  # noqa: F401
from core.prompt.tests.scenarios import SCENARIOS
from Module.system_prompt.sections.emotion import EmotionSection

pytestmark = pytest.mark.service


def _assemble(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """构造用冷角色 + mock 引擎的 PromptAssembler。"""
    return PromptAssembler(
        cold_config, mock_session, mock_long_term,
        personality_engine=cold_engine_mock,
    )


async def _render(assembler, user_message="今天天气不错。", user_name="用户",
                  stt_emotion="", stt_events=None, tts_provider="minimax") -> str:
    """渲染完整 12 段 system prompt。"""
    return await assembler.build_system_prompt(
        user_message=user_message, user_name=user_name,
        stt_emotion=stt_emotion, stt_events=stt_events or [],
        tts_provider=tts_provider,
    )
```

- [ ] **Step 2: 写各 `test_leak_*` 用用例（红基线——断言泄漏存在）**

```python
@pytest.mark.asyncio
async def test_leak_tone_marker(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C2/C6/F2 泄漏 ♪——冷角色渲染结果含 ♪。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "♪" in rendered


@pytest.mark.asyncio
async def test_leak_speaking_mode_structure(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C6 泄漏五模式切换。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "五模式切换" in rendered


@pytest.mark.asyncio
async def test_leak_fixed_persona(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C6 泄漏固定人设（明快/可爱的女孩子）。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "明快" in rendered
    assert "可爱的女孩子" in rendered


@pytest.mark.asyncio
async def test_leak_relationship_structure(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C2 泄漏 Elysia 关系观——注意用实际渲染的词（普遍之爱/制造暧昧）。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "普遍之爱" in rendered
    assert "制造暧昧" in rendered


@pytest.mark.asyncio
async def test_leak_relationship_stage(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C2 泄漏关系阶段标签（初识）。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "初识" in rendered


@pytest.mark.asyncio
async def test_leak_emotion_model(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C3 泄漏固定应对话术与傲娇。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "今天不想振作也没有关系" in rendered
    assert "傲娇" in rendered


@pytest.mark.asyncio
async def test_leak_template_body_pronoun(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """模板正文泄漏她——user_pronoun=你 仍出现字面她（c2 第 18 行、f2/f3 示例）。

    注意：这不是 section 默认 her 泄漏——build_system_prompt 已把 user_pronoun
    正确覆盖为你。真正来源是模板正文里的字面她。
    """
    # cold_config 的 user_pronoun 已是"你"
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "她" in rendered


@pytest.mark.asyncio
async def test_leak_output_format_example(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """F2 示例泄漏（诶嘿~被你发现啦）。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "诶嘿~被你发现啦" in rendered


@pytest.mark.asyncio
async def test_leak_tts_example(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """F3 minimax 分支示例泄漏她（她果然很聪明呢）。"""
    rendered = await _render(_assemble(cold_config, cold_engine_mock, mock_long_term, mock_session),
                             tts_provider="minimax")
    assert "她果然很聪明呢" in rendered
```

- [ ] **Step 3: 写 `test_section_default_pronoun`（窄路径——不经过 set_context）**

```python
@pytest.mark.asyncio
async def test_section_default_pronoun():
    """section 默认 _user_pronoun=她 未被覆盖时泄漏。

    构造 EmotionSection 但不调 set_context(user_pronoun=...)，
    验证默认 her 会进入渲染结果。这是"section 默认值泄漏"的窄路径
    （区别于 test_leak_template_body_pronoun 的模板正文泄漏）。
    """
    from paths import ProjectPath
    s = EmotionSection(f"{ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()}/c3_emotion.j2")
    # 不调用 set_context(user_pronoun=...)——保持默认 her
    rendered = await s.render()
    assert "她" in rendered
```

- [ ] **Step 4: 写跨场景参数化用例**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id,user_message,stt_emotion,stt_events,tts_provider", SCENARIOS)
async def test_leak_parameterized_over_scenarios(
    scenario_id, user_message, stt_emotion, stt_events, tts_provider,
    cold_config, cold_engine_mock, mock_long_term, mock_session,
):
    """♪ 泄漏跨场景稳定复现。"""
    rendered = await _render(
        _assemble(cold_config, cold_engine_mock, mock_long_term, mock_session),
        user_message=user_message, stt_emotion=stt_emotion,
        stt_events=stt_events, tts_provider=tts_provider,
    )
    assert "♪" in rendered
```

- [ ] **Step 5: 运行测试确认红基线成立（测试应通过，因为泄漏确实存在）**

```bash
cd /home/yomu/elysia-agent && conda run -n Elysia \
  python -m pytest core/prompt/tests/test_system_prompt_leak.py -v -m "service" --tb=short
```

Expected: 全部 pass（`♪`/`五模式切换`/`明快`/`普遍之爱`/`初识`/`今天不想振作也没有关系`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢` 都在渲染结果里）。

- [ ] **Step 6: 提交**

```bash
git add core/prompt/tests/test_system_prompt_leak.py
git commit -m "test(stage-a): 12 段 system prompt Elysia 泄漏基线（红基线）"
```

---

### Task 3: 12 段 System Prompt 快照测试

**Files:**
- Create: `core/prompt/tests/test_system_prompt_snapshot.py`
- Create: `core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt`

**Interfaces:**
- Consumes: `conftest.{cold_config,cold_engine_mock,mock_long_term,mock_session}`、`_render` 逻辑（Task 2 内部函数，可复制或提取为 conftest helper——**务实地**在 Task 2 中把 `_assemble`/`_render` 提取为 conftest 共享 helper，本 Task 复用）。
- Produces: `test_snapshot_matches_current`、`test_12_section_order`、`test_frame_wrappers_present`，及快照 fixture 文件。

- [ ] **Step 1: 把 `_assemble`/`_render` 提取为 conftest 共享 helper**（若 Task 2 已内联）

在 `core/prompt/tests/conftest.py` 增加：

```python
# ---- 共享 helper（供泄漏测试 + 快照测试复用）----


def make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """构造用冷角色 + mock 引擎的 PromptAssembler。"""
    from core.prompt.prompt_assembler import PromptAssembler
    return PromptAssembler(
        cold_config, mock_session, mock_long_term,
        personality_engine=cold_engine_mock,
    )


async def render_system_prompt(assembler, user_message="今天天气不错。",
                               user_name="用户", stt_emotion="", stt_events=None,
                               tts_provider="minimax") -> str:
    """渲染完整 12 段 system prompt。"""
    return await assembler.build_system_prompt(
        user_message=user_message, user_name=user_name,
        stt_emotion=stt_emotion, stt_events=stt_events or [],
        tts_provider=tts_provider,
    )
```

更新 Task 2 的 `_assemble`/`_render` 为调用这两个 helper（删除重复定义）。

- [ ] **Step 2: 写归一化函数 + 快照测试**

```python
# core/prompt/tests/test_system_prompt_snapshot.py
"""12 段 System Prompt 现况快照测试。

锁定基线 commit e793a782 的渲染结果，供阶段 B 回归比对。
C5 含 datetime.now() 时间字段，快照比对前用 normalize_snapshot 归一化。
"""

import re
from pathlib import Path

import pytest

from core.prompt.tests.conftest import (  # noqa: F401
    cold_config, cold_engine_mock, make_assembler, mock_long_term,
    mock_session, render_system_prompt,
)

pytestmark = pytest.mark.service

_FIXTURE = Path(__file__).with_name("fixtures") / "system_prompt_snapshot_e793a782.txt"


def normalize_snapshot(text: str) -> str:
    """去掉 C5 时间相关行，避免 datetime.now() 引起快照漂移。"""
    text = re.sub(r"当前时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "当前时间: <TIME>", text)
    text = re.sub(r"时段: (早晨|中午|下午|傍晚|夜晚)", "时段: <TOD>", text)
    text = re.sub(r"星期: (周一|周二|周三|周四|周五|周六|周日)", "星期: <DOW>", text)
    text = re.sub(r"季节: (春|夏|秋|冬)", "季节: <SEASON>", text)
    return text


@pytest.mark.asyncio
async def test_snapshot_matches_current(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """当前渲染结果与已入库快照一致（锁定现况）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    expected = _FIXTURE.read_text(encoding="utf-8")
    assert normalize_snapshot(rendered) == normalize_snapshot(expected)


@pytest.mark.asyncio
async def test_12_section_order(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """结果按 _RENDER_ORDER 顺序出现（只含确定渲染的 section）。

    注意：C9 self_perception（awareness_text 空）与 C8 own_life（session 空）
    在本配置下渲染为空，<self_perception>/<own_life> 标签不出现；
    C4 memory 因 mock_long_term.search 返回 [] 也为空。故只断言确定渲染的
    标签：conflict_rules→output_format→tts_markup→relationship→proactive→
    speaking_style→personality→emotion→sensory_input（Memory/C8/C9 略去）。
    """
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    ordered_tags = [
        "conflict_rules", "output_format", "tts_markup", "relationship",
        "proactive", "speaking_style", "personality",
        "emotion", "sensory_input",
    ]
    last = -1
    for tag in ordered_tags:
        pos = rendered.find(tag)
        assert pos > last, f"section order broken: {tag} not after previous (pos={pos}, last={last})"
        last = pos


@pytest.mark.asyncio
async def test_frame_wrappers_present(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """关键 XML wrapper 标签都在（仅限确定渲染的 section）。

    不做 memory_bank/self_perception/own_life 断言——本配置下它们不渲染。
    """
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    for tag in ["conflict_rules", "output_format", "tts_markup", "relationship",
                "proactive", "speaking_style", "personality", "emotion",
                "sensory_input"]:
        assert tag in rendered
```

- [ ] **Step 3: 生成快照 fixture 文件**（用脚本跑一次，产出归一化后文本并写盘）

```bash
cd /home/yomu/elysia-agent && conda run -n Elysia python - <<'PY'
import asyncio, re
from pathlib import Path
from core.config.character_config import CharacterConfig
from core.prompt.prompt_assembler import PromptAssembler
from core.memory.session import SessionMemory
from unittest.mock import MagicMock, AsyncMock
from core.personality.engine import C1Data, C6Data, C9Data
from core.prompt.tests.conftest import COLDSHARD_BASE_PERSONALITY

engine = MagicMock()
engine.state.mood = "neutral"
engine.render_c6.return_value = C6Data("")
engine.render_c1.return_value = C1Data(core_personality=COLDSHARD_BASE_PERSONALITY, self_personality="")
engine.render_c9.return_value = C9Data("")
lt = MagicMock(); lt.search = AsyncMock(return_value=[])

char = CharacterConfig(name="ReservedColdCharacter", user_pronoun="你", base_personality=COLDSHARD_BASE_PERSONALITY)
asp = PromptAssembler(char, SessionMemory(), lt, personality_engine=engine)

def norm(t):
    t = re.sub(r"当前时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "当前时间: <TIME>", t)
    t = re.sub(r"时段: (早晨|中午|下午|傍晚|夜晚)", "时段: <TOD>", t)
    t = re.sub(r"星期: (周一|周二|周三|周四|周五|周六|周日)", "星期: <DOW>", t)
    t = re.sub(r"季节: (春|夏|秋|冬)", "季节: <SEASON>", t)
    return t

async def main():
    text = await asp.build_system_prompt(user_message="今天天气不错。", user_name="用户",
                                          stt_emotion="", stt_events=[], tts_provider="minimax")
    out = norm(text).strip() + "\n"
    fp = Path("core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt")
    fp.parent.mkdir(parents=True, exist_ok=True)   # fixtures/ 目录不存在，须先创建
    fp.write_text(out, encoding="utf-8")
    print(f"wrote {fp} ({len(out)} chars)")

asyncio.run(main())
PY
```

> **关键**：`core/prompt/tests/fixtures/` 目录当前不存在，必须 `fp.parent.mkdir(parents=True, exist_ok=True)` 先建目录再写盘，否则 `write_text` 抛 `FileNotFoundError`。

- [ ] **Step 4: 运行快照测试确认通过**

```bash
cd /home/yomu/elysia-agent && conda run -n Elysia \
  python -m pytest core/prompt/tests/test_system_prompt_snapshot.py -v -m "service" --tb=short
```

Expected: 3 个用例 pass。`test_12_section_order`/`test_frame_wrappers_present` 已按"仅确定渲染的 section"调整——C9 self_perception（awareness 空）、C8 own_life（session 空）、C4 memory（mock 返回 []）均渲染为空、标签不出现，故从断言列表移除这些标签。

> **说明**：这些标签（`memory_bank`/`self_perception`/`own_life`）是否出现取决于本配置下 section 是否渲染为空。若你调整了 fixture（如让 `mock_long_term.search` 返回一条记忆），`memory_bank` 会重新出现——但那会引入 `timestamp` 非确定性，破坏快照比对。**保持 `mock_long_term.search` 返回 `[]`，维持快照确定性。**

- [ ] **Step 5: 提交**

```bash
git add core/prompt/tests/conftest.py core/prompt/tests/test_system_prompt_snapshot.py \
        core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt
git commit -m "test(stage-a): 12 段 system prompt 现况快照 + 归一化比对"
```

---

### Task 4: 辅助 Prompt 泄漏测试（CharacterReflector / SpeakingStyleEdit）

**Files:**
- Create: `core/personality/tests/test_prompt_leak_auxiliary.py`

**Interfaces:**
- Consumes: `Module.system_prompt.manager.PromptManager`、`core.prompt.tests.leak_lexicon`（作为可导入模块）。**注意**：本文件在 `core/personality/tests/`，无法复用 `core/prompt/tests/conftest.py` 的夹具（conftest 目录作用域）。`cold_config` 用人格基线文本内联，或用 `core.prompt.tests.leak_lexicon` 导入词表即可，不依赖 PromptAssembler。
- Produces: `test_character_reflector_leak_pronoun`、`test_speaking_style_edit_leak_forced_structure`、`test_speaking_style_edit_leak_immutable_color`。

- [ ] **Step 1: 写辅助 Prompt 泄漏测试（用 PromptManager.render，非 render_macro）**

```python
# core/personality/tests/test_prompt_leak_auxiliary.py
"""辅助 Prompt（CharacterReflector/SpeakingStyleEdit）的 Elysia 泄漏基线。

注意：
- SpeakingStyleEdit 生产路径是 pm.render()（该模板不含 macro），不是 render_macro()。
- 本文件在 core/personality/tests/，不依赖 core/prompt/tests/conftest 的夹具
  （pytest conftest 目录作用域隔离），词条从 leak_lexicon 导入。
"""

import pytest

from paths import ProjectPath
from Module.system_prompt.manager import PromptManager

pytestmark = pytest.mark.service


def _pm() -> PromptManager:
    """构造指向 core/prompts 目录的 PromptManager（用 ProjectPath 定位，不受 cwd 影响）。"""
    return PromptManager(str(ProjectPath.PROMPTS.resolve()))


# 冷角色视角——故意用中性人格/用户，若结果仍出现她/Elysia 词汇即泄漏
_RECENT_CONVERSATIONS = "用户：你好。\nReservedColdCharacter：你好，请讲。"


def test_character_reflector_leak_pronoun():
    """CharacterReflector 硬编码她——用户是「用户」却出现她。"""
    pm = _pm()
    result = pm.render(
        "CharacterReflector.j2",
        character_name="ReservedColdCharacter",
        user_name="用户",
        l1_core="你是 ReservedColdCharacter，冷静克制的观察者。",
        l2_self="",
        l3_history="",
        last_self_perception="",
        mood_snapshot="当前情绪: neutral (强度: 0.00)",
        time_span="约 1 分钟",
        buffer_size=1,
        recent_conversations=_RECENT_CONVERSATIONS,
    )
    assert "她" in result


def test_speaking_style_edit_leak_forced_structure():
    """SpeakingStyleEdit 强制保留 Elysia 五模式结构。"""
    pm = _pm()
    result = pm.render(
        "SpeakingStyleEdit.j2",
        character_name="ReservedColdCharacter",
        self_l2="我说话冷静、正式、克制。",
        current_speaking_style="你说话冷静、正式、克制。",
    )
    assert "五模式切换" in result
    assert "♪" in result


def test_speaking_style_edit_leak_immutable_color():
    """SpeakingStyleEdit 强制「明快、柔和、自信、亲昵」不可变底色。"""
    pm = _pm()
    result = pm.render(
        "SpeakingStyleEdit.j2",
        character_name="ReservedColdCharacter",
        self_l2="我说话冷静、正式、克制。",
        current_speaking_style="你说话冷静、正式、克制。",
    )
    assert "明快、柔和、自信、亲昵" in result
```

- [ ] **Step 2: 运行测试**

```bash
cd /home/yomu/elysia-agent && conda run -n Elysia \
  python -m pytest core/personality/tests/test_prompt_leak_auxiliary.py -v -m "service" --tb=short
```

Expected: 3 个用例 pass。

- [ ] **Step 3: 提交**

```bash
git add core/personality/tests/test_prompt_leak_auxiliary.py
git commit -m "test(stage-a): 辅助 Prompt（Reflector/SpeakingStyleEdit）Elysia 泄漏基线"
```

---

### Task 5: 记忆 Prompt 泄漏测试（ExtractMemory / ConsolidationCompare / MemoryReflector）

**Files:**
- Create: `core/memory/tests/test_prompt_leak_memory.py`

**Interfaces:**
- Consumes: `core.memory.consolidator.{MemoryConsolidator,_get_extraction_prompt,_get_compare_prompt}`、`core.memory.memory_reflector.{MemoryReflector,_get_reflector_prompt? 或 REFLECTOR_PROMPT_PATH}`、`core.prompt.tests.leak_lexicon`。**不依赖** `core/prompt/tests/conftest` 夹具。
- Produces: `test_extract_memory_leak_identity`、`test_extract_memory_leak_placeholder_unreplaced`、`test_consolidation_compare_leak_identity`、`test_memory_reflector_leak_identity`、`test_memory_reflector_leak_user_reference`。

- [ ] **Step 1: 写记忆 Prompt 泄漏测试**

```python
# core/memory/tests/test_prompt_leak_memory.py
"""记忆 Prompt（ExtractMemory/ConsolidationCompare/MemoryReflector）的 Elysia 泄漏基线。

关键：这三个 prompt 用 str.replace() 注入变量（只替换 {conversation}/{memories}/
{new_items}/{existing_items}），{character_name}/{user_name}/妖梦/Elysia/女性AI伴侣
是写死在正文里的，不会被替换——这就是泄漏。
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.memory.consolidator import (
    MemoryConsolidator, _get_extraction_prompt, _get_compare_prompt,
)
from core.memory.memory_reflector import MemoryReflector, REFLECTOR_PROMPT_PATH

pytestmark = pytest.mark.service


class _CapturingLLM:
    """捕获发给 LLM 的 prompt 并返回预设响应的假 LLM。"""

    def __init__(self, response: str):
        self._response = response
        self.prompts: list[str] = []

    async def think(self, messages, extra_body=None):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self._response)


def _cold_consolidator() -> MemoryConsolidator:
    """构造 cold 角色的 MemoryConsolidator（mock llm + memory）。

    传 buffer_threshold=1：consolidate() 只有 len(buffer) >= threshold 才会
    调用 _do_consolidate()（进而向 LLM 发 prompt）。默认阈值 6，单条消息不会触发；
    设 1 保证单条对话就触发提取，从而捕获发给 LLM 的 prompt。
    """
    llm = _CapturingLLM("[]")
    mem = MagicMock()
    mem.search = AsyncMock(return_value=[])
    mem.get_all = MagicMock(return_value=[])
    return MemoryConsolidator(
        llm=llm, memory=mem,
        character_name="ReservedColdCharacter", user_name="用户",
        buffer_threshold=1,
    )


def test_extract_memory_leak_placeholder_unreplaced():
    """ExtractMemory.j2 的 {character_name}/{user_name} 占位符未被替换。"""
    text = _get_extraction_prompt()
    assert "{character_name}" in text
    assert "{user_name}" in text


def test_extract_memory_leak_identity():
    """ExtractMemory.j2 正文硬编码「女性 AI 伴侣」（即使角色是 cold 也写入 prompt）。"""
    text = _get_extraction_prompt()
    assert "女性 AI 伴侣" in text


@pytest.mark.asyncio
async def test_extract_memory_prompt_sent_to_llm_leaks():
    """走真实 consolidate()，捕获发给 LLM 的 prompt，仍含硬编码女性 AI 伴侣。"""
    c = _cold_consolidator()
    results = await c.consolidate("用户：我养了一只猫。", "ReservedColdCharacter：好的。")
    assert len(c._llm.prompts) >= 1  # 触发提取（由于 get_all 空，走 insert 分支，无 compare）
    prompt = c._llm.prompts[0]
    assert "女性 AI 伴侣" in prompt


def test_consolidation_compare_leak_identity():
    """ConsolidationCompare.j2 硬编码 Elysia 身份。"""
    text = _get_compare_prompt()
    assert "你是 Elysia 的记忆一致性检查器" in text


def test_memory_reflector_leak_identity():
    """MemoryReflector.j2 文件正文硬编码 Elysia/妖梦。"""
    content = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    assert "Elysia 是妖梦的女性 AI 伴侣" in content


def test_memory_reflector_leak_user_reference():
    """MemoryReflector.j2 全篇硬编码妖梦（用户指代）。"""
    content = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    assert "妖梦" in content
```

- [ ] **Step 2: 运行测试**

```bash
cd /home/yomu/elysia-agent && conda run -n Elysia \
  python -m pytest core/memory/tests/test_prompt_leak_memory.py -v -m "service" --tb=short
```

Expected: 5 个用例 pass（`_get_extraction_prompt()`/`_get_compare_prompt()` 为同步直接读文件；`test_extract_memory_prompt_sent_to_llm_leaks` 是 async 走 `consolidate()`——已设 `buffer_threshold=1`，单条对话即触发提取并捕获 prompt）。

- [ ] **Step 3: 提交**

```bash
git add core/memory/tests/test_prompt_leak_memory.py
git commit -m "test(stage-a): 记忆 Prompt（Extract/Compare/Reflector）Elysia 泄漏基线"
```

---

### Task 6: 全量门禁验证 + 文档同步

**Files:**
- Modify: `docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`（可选：阶段 A 描述同步）

**Interfaces:**
- Consumes: 前 5 个 Task 的全部测试。
- Produces: 门禁通过 + 文档同步。

- [ ] **Step 1: 运行 `make lint`（禁相对导入 + 类型注解）**

```bash
cd /home/yomu/elysia-agent && make lint
```

Expected: 通过。若新测试含相对导入（TID252）或生产代码式未注解参数导致失败，修复（测试文件参数可 `**kwargs: Any`，夹具用类型注解）。

- [ ] **Step 2: 运行 `make pyright`（须 Elysia env）**

```bash
cd /home/yomu/elysia-agent && make pyright
```

Expected: 通过（`make pyright` 在 CLI 中已 wrap Elysia env 或需 `conda run -n Elysia pyright`）。若新测试未被 pyright 扫描（排除 `**/tests/**`），则不受影响。

- [ ] **Step 3: 运行 `make test-quick` 全量**

```bash
cd /home/yomu/elysia-agent && make test-quick
```

Expected: 全部 pass。确认新测试（`@pytest.mark.service`）被 `-m "unit or service"` 捕获。若个别泄漏测试因未来环境差异失败，以实际为准修正断言词或夹具。

- [ ] **Step 4: 同步架构文档**（可选、低优先）

若需要，在 `docs/2026-08-30-character-prompt-customization-backend-architecture-design.md` 阶段 A 小节末尾追加一行：`阶段 A 已落地：红基线测试网（test_system_prompt_leak / test_prompt_leak_auxiliary / test_prompt_leak_memory + 快照）已建立，证明当前 12 段 + 辅助 Prompt 存在 Elysia 泄漏。` 这为非强制项，不阻塞。

- [ ] **Step 5: 最终提交**

```bash
git add -A
git commit -m "chore(stage-a): 全量门禁通过 + 文档同步"
```

---

## Self-Review

### Spec coverage
对照 spec：
- 共享夹具（§5.1 cold_config/cold_engine_mock/mock_long_term/mock_session）→ Task 1 ✅
- Elysia 词表（§5.2 leak_lexicon）→ Task 1 ✅
- 场景集（§5.3 SCENARIOS）→ Task 1 ✅
- 12 段泄漏测试（§6.2 test_leak_* + test_section_default_pronoun）→ Task 2 ✅
- 12 段快照 + 归一化（§5.4/§6.3）→ Task 3 ✅
- 辅助 Prompt 泄漏（§6.4，含 SpeakingStyleEdit 走 pm.render 修正）→ Task 4 ✅
- 记忆 Prompt 泄漏（§6.5，含妖梦 moved to memory）→ Task 5 ✅
- 门禁 + 文档同步（§9 exit-conditions）→ Task 6 ✅

### Placeholder scan
- 无 TBD/TODO。
- 每个测试都有实际断言字符串（已按 spec §1.3 警示避免 `主动靠近不占有`/`四阶段`）。
- 快照生成用具体脚本，非占位。

### Type consistency
- `make_assembler`/`render_system_prompt` 在 conftest 定义，Task 2/3 复用。
- `MemoryConsolidator(llm=, memory=, character_name=, user_name=)` 签名与 `consolidator.py:175` 一致。
- `MemoryReflector` 构造仅在伪代码中提及，实际测试走文件读取（`REFLECTOR_PROMPT_PATH.read_text`），不构造实例，避免依赖其完整构造函数。
- `PromptManager.render(name, **kwargs)` 与 `manager.py:61` 一致（`render` 接受 `**kwargs`）。

### 已知风险（实施时按实际验证）
1. C9 self_perception / C8 own_life / C4 memory 在本配置（awareness 空、session 空、mock 返回 []）下渲染为空 → 相关标签不出现。**已解决**：Task 3 的 `test_12_section_order`/`test_frame_wrappers_present` 只断言确定渲染的标签；快照 `fixtures/` 目录需先 `mkdir`（已在生成脚本中处理）。
2. `consolidate()` 阈值已解决：Task 5 的 `_cold_consolidator` 显式 `buffer_threshold=1`。
3. `make pyright` 可能因测试依赖而需确认——测试目录被 pyright 排除，通常不受影响。
4. Task 4 的 `PromptManager.render()` 需要 `core/prompts` 目录存在；`_pm()` 已用 `str(ProjectPath.PROMPTS.resolve())`（并非相对路径）。
