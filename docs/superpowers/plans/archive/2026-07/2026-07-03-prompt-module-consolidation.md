# Prompt 模块合并实现计划

> **For agentic workers:** 纯搬迁 + 抽离任务，直接在本 session 执行。

**Goal:** brain_prompt 合并到 Module/prompt/，prompt_assembler 核心逻辑抽入 MessageBuilder，prompt_assembler 精简为胶水层。

**Architecture:** git mv brain_prompt 文件入 Module/prompt/，从 prompt_assembler.py 提取 MessageBuilder，更新 imports，删除 brain_prompt。

**Tech Stack:** Python 3.13, git mv, sed

## Global Constraints

- 模板数据 `prompt/brain/` 不动
- `core/agent.py`, `core/cognitive_loop.py` import 路径不变
- 黄金对比测试逐字节一致（不进不退）
- `Module/prompt/` 不依赖 `core/`（MessageBuilder 纯 prompt 组装）

---

### Task 1: brain_prompt 文件迁入 Module/prompt/

**Files:**
- Create: `Module/prompt/section.py` (mv)
- Create: `Module/prompt/parser.py` (mv)
- Create: `Module/prompt/profile.py` (mv)
- Create: `Module/prompt/registry.py` (mv)
- Create: `Module/prompt/tests/unit/` (mv — test_section, test_parser, test_profile, test_registry)
- Create: `Module/prompt/tests/integration/` (mv — test_golden_diff, test_render_full)
- Delete: `Module/brain_prompt/`

- [ ] **Step 1: git mv brain_prompt 源文件 + 测试文件到 Module/prompt/**

```bash
# 目录准备
mkdir -p Module/prompt/tests/unit Module/prompt/tests/integration

# 源文件（扁平迁入 Module/prompt/）
git mv Module/brain_prompt/section.py Module/prompt/
git mv Module/brain_prompt/parser.py Module/prompt/
git mv Module/brain_prompt/profile.py Module/prompt/
git mv Module/brain_prompt/registry.py Module/prompt/

# 测试文件
git mv Module/brain_prompt/tests/__init__.py Module/prompt/tests/
git mv Module/brain_prompt/tests/unit/__init__.py Module/prompt/tests/unit/
git mv Module/brain_prompt/tests/unit/test_section.py Module/prompt/tests/unit/
git mv Module/brain_prompt/tests/unit/test_parser.py Module/prompt/tests/unit/
git mv Module/brain_prompt/tests/unit/test_profile.py Module/prompt/tests/unit/
git mv Module/brain_prompt/tests/unit/test_registry.py Module/prompt/tests/unit/
git mv Module/brain_prompt/tests/integration/__init__.py Module/prompt/tests/integration/
git mv Module/brain_prompt/tests/integration/test_golden_diff.py Module/prompt/tests/integration/
git mv Module/brain_prompt/tests/integration/test_render_full.py Module/prompt/tests/integration/
```

- [ ] **Step 2: 处理 brain_prompt 目录残留**

```bash
# __init__.py、README.md 已被 git mv 忽略（Module/prompt/ 已有同名文件）
# 手动处理
git rm Module/brain_prompt/__init__.py
git rm Module/brain_prompt/README.md
rmdir Module/brain_prompt/tests/unit Module/brain_prompt/tests/integration Module/brain_prompt/tests Module/brain_prompt 2>/dev/null
rm -rf Module/brain_prompt/__pycache__ 2>/dev/null
test ! -d Module/brain_prompt/ && echo "OK: deleted" || echo "FAIL: still exists"
```

- [ ] **Step 3: 修正迁入文件的内部 import**

迁入文件内部相互引用从 `from .xxx` → 仍然是 `from .xxx`（因为在同一目录下），不需要改。但需要检查它们是否有 `from Module.brain_prompt` 的自引用：

```bash
grep -rn 'Module\.brain_prompt' Module/prompt/ --include='*.py' 2>/dev/null | grep -v __pycache__
```

如果无输出则无需修改。如果有，改为 `Module.prompt`。

- [ ] **Step 4: 运行迁入后的测试确认无断链**

```bash
python -m pytest Module/prompt/tests/unit/ Module/prompt/tests/integration/ -v -q
```

Expected: 43 passed (15 parser + 10 section + 4 profile + 8 registry + 2 golden diff + 4 render full)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: merge brain_prompt into Module/prompt/
- Move section.py, parser.py, profile.py, registry.py
- Move all tests (unit + integration)
- Delete Module/brain_prompt/"
```

---

### Task 2: 创建 MessageBuilder（从 prompt_assembler 提取）

**Files:**
- Create: `Module/prompt/message_builder.py`
- Create: `Module/prompt/tests/integration/test_message_builder.py`

**Interfaces:**
- Produces: `MessageBuilder(registry, fallback_prompt)`, `.build_system_prompt(profile, context) -> str`, `.build_messages(system_prompt, history, user_content, *, supports_multimodal) -> list[dict]`

- [ ] **Step 1: 编写 test_message_builder.py**

写入 `Module/prompt/tests/integration/test_message_builder.py`：

```python
"""MessageBuilder 集成测试。"""
import pytest
from Module.prompt.message_builder import MessageBuilder
from Module.prompt.registry import BrainPromptRegistry


_PROJECT_ROOT = __import__('pathlib').Path(__file__).resolve().parent.parent.parent.parent.parent


class TestBuildSystemPrompt:
    def test_registry_path_renders(self):
        """Registry 可用时走 Registry 渲染。"""
        brain_dir = str(_PROJECT_ROOT / "prompt" / "brain")
        registry = BrainPromptRegistry(brain_dir)
        builder = MessageBuilder(registry=registry)
        context = {
            "name": "Elysia", "user_name": "妖梦",
            "user_preferences": "", "self_perception": "",
            "sensory_ctx": {
                "current_time": "2024-01-01 12:00:00",
                "time_of_day": "下午", "day_of_week": "周一", "season": "冬",
                "perception": "Hello", "stt_emotion": "",
                "stt_emotion_emoji": "", "stt_events": "",
            },
            "memory_ctx": {"micro": [], "macro": []},
            "mood": "neutral", "tool_list": "",
        }
        result = builder.build_system_prompt("default", context)
        assert "Elysia" in result
        assert "2024-01-01" in result

    def test_fallback_hardcoded(self):
        """无 Registry 无 fallback → hardcoded prompt。"""
        builder = MessageBuilder()
        result = builder.build_system_prompt("default", {})
        assert "AI伴侣" in result or "桌面" in result

    def test_fallback_brain_j2(self):
        """Registry None 但有 PromptManager → 走 Brain.j2 fallback。"""
        from Module.prompt import PromptManager
        prompt_dir = str(_PROJECT_ROOT / "prompt")
        fallback = PromptManager(prompt_dir)
        builder = MessageBuilder(fallback_prompt=fallback)
        context = {
            "name": "Elysia", "user_name": "妖梦",
            "user_preferences": "", "self_perception": "",
            "sensory_ctx": {
                "current_time": "2024-01-01 12:00:00",
                "time_of_day": "下午", "day_of_week": "周一", "season": "冬",
                "perception": "Hello", "stt_emotion": "",
                "stt_emotion_emoji": "", "stt_events": "",
            },
            "memory_ctx": {"micro": [], "macro": []},
            "mood": "neutral", "tool_list": "",
        }
        result = builder.build_system_prompt("default", context)
        assert "Elysia" in result


class TestBuildMessages:
    def test_basic_assembly(self):
        """基本 messages 组装：system + history + user。"""
        builder = MessageBuilder()
        system = "You are a helpful assistant."
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!", "inner_voice": "someone said hi"},
        ]
        messages = builder.build_messages(system, history, "How are you?")
        assert messages[0] == {"role": "system", "content": system}
        assert len(messages) >= 3
        # 有 inner_voice 的 assistant 消息应注入 iv_prefix
        assistant_has_iv = any("<inner_voice>" in m["content"] for m in messages if m["role"] == "assistant")
        assert assistant_has_iv

    def test_tool_message_skipped(self):
        """工具结果消息被跳过。"""
        builder = MessageBuilder()
        history = [
            {"role": "assistant", "content": "let me check", "tool_calls": [
                {"function": {"name": "web_search", "arguments": '{"query":"test"}'}}
            ]},
            {"role": "tool", "content": "search results here"},
            {"role": "assistant", "content": "found it"},
        ]
        messages = builder.build_messages("system", history, "thanks")
        roles = [m["role"] for m in messages]
        assert "tool" not in roles
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest Module/prompt/tests/integration/test_message_builder.py -v
```
Expected: FAIL (MessageBuilder not defined)

- [ ] **Step 3: 实现 MessageBuilder**

写入 `Module/prompt/message_builder.py`：

```python
"""MessageBuilder — 纯 prompt 组装，不依赖 core/。"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MessageBuilder:
    """纯 prompt 组装 — 接收 context + history → 输出 messages[]。
    
    无状态，不依赖 core/ 的任何组件。
    """

    def __init__(
        self,
        registry=None,  # BrainPromptRegistry | None
        fallback_prompt=None,  # PromptManager | None
    ):
        self._registry = registry
        self._fallback = fallback_prompt

    def build_system_prompt(
        self, profile: str = "default", context: dict[str, Any] | None = None
    ) -> str:
        """渲染 system prompt。三层 fallback: Registry → Brain.j2 → hardcoded。"""
        if context is None:
            context = {}

        # Tier 1: BrainPromptRegistry
        if self._registry is not None:
            try:
                return self._registry.render(profile=profile, context=context)
            except Exception:
                logger.warning("Registry 渲染失败，fallback", exc_info=True)

        # Tier 2: Brain.j2 render_macro
        if self._fallback is not None:
            try:
                sensory = context.get("sensory_ctx", {})
                memory = context.get("memory_ctx", {})
                state = {"mood": context.get("mood", "neutral")}
                return self._fallback.render_macro(
                    "Brain.j2", "BrainSystemPrompt",
                    sensory=sensory, memory=memory, state=state,
                    name=context.get("name", ""),
                    user_name=context.get("user_name", ""),
                    user_preferences=context.get("user_preferences", ""),
                    self_perception=context.get("self_perception", ""),
                )
            except Exception:
                logger.warning("Brain.j2 渲染失败，fallback", exc_info=True)

        # Tier 3: hardcoded
        return _hardcoded_fallback(context)

    def build_messages(
        self,
        system_prompt: str,
        history: list[dict],
        user_content: str | list[dict] | None = None,
        *,
        supports_multimodal: bool = False,
    ) -> list[dict]:
        """组装完整 messages[]。

        Args:
            system_prompt: 已渲染的 system prompt 字符串
            history: SessionMemory.recent_messages(8) 返回的历史消息列表
            user_content: 当前用户消息（str 或多模态 content list）
            supports_multimodal: LLM 是否支持多模态图片

        Returns:
            [{"role": "system", "content": system_prompt},
             ...history with inner_voice + tool_summary injection,
             {"role": "user", "content": user_content}]
        """
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        for msg in history:
            role = msg.get("role", "")
            if role == "tool":
                continue

            inner_voice = msg.get("inner_voice")
            iv_prefix = f"<inner_voice>{inner_voice}</inner_voice>\n" if inner_voice else ""

            tool_calls = msg.get("tool_calls")
            tool_summary = _format_tool_summary(tool_calls) if tool_calls else ""

            content_raw = msg.get("content", "")
            if isinstance(content_raw, list):
                if supports_multimodal:
                    msg_content = _content_to_openai(content_raw)
                    if tool_summary:
                        if isinstance(msg_content, list):
                            msg_content.append({"type": "text", "text": tool_summary})
                        else:
                            msg_content = msg_content + tool_summary
                    if iv_prefix:
                        if isinstance(msg_content, list):
                            msg_content.insert(0, {"type": "text", "text": iv_prefix.rstrip()})
                        else:
                            msg_content = iv_prefix + msg_content
                    messages.append({"role": role, "content": msg_content})
                else:
                    text_only = [p for p in content_raw if p.get("type") == "text"]
                    text_content = "".join(p.get("content", "") for p in text_only)
                    image_count = sum(1 for p in content_raw if p.get("type") == "image")
                    if image_count > 0 and role == "user":
                        hint = _format_image_history_hint(image_count)
                        text_content = hint + text_content if text_content else hint
                    if text_content or tool_summary:
                        msg_content = (text_content or "") + (tool_summary or "")
                        messages.append({"role": role, "content": iv_prefix + msg_content})
            elif isinstance(content_raw, str):
                msg_content = content_raw + (tool_summary or "")
                messages.append({"role": role, "content": iv_prefix + msg_content})
            elif "parts" in msg:
                text_from_parts = "".join(p["content"] for p in msg["parts"])
                msg_content = text_from_parts + (tool_summary or "")
                messages.append({"role": role, "content": iv_prefix + msg_content})
            elif tool_summary:
                messages.append({"role": role, "content": iv_prefix + tool_summary})
            else:
                messages.append({"role": role, "content": iv_prefix + str(content_raw)})

        # 当前用户消息
        if isinstance(user_content, list):
            from Module.schema.message_format import content_to_openai, content_text
            has_images = any(p.get("type") == "image" for p in user_content)
            if has_images and not supports_multimodal:
                text = content_text(user_content)
                image_count = sum(1 for p in user_content if p.get("type") == "image")
                if image_count == 1:
                    hint = "[用户发送了一张图片。使用 describe_image 工具查看。]"
                else:
                    indices = list(range(image_count))
                    hint = f"[用户发送了{image_count}张图片。使用 describe_image(image_indices={indices}) 一次性查看所有图片。]"
                user_text = f"{hint}\n{text}" if text else hint
                messages.append({"role": "user", "content": user_text})
            else:
                messages.append({"role": "user", "content": content_to_openai(user_content)})
        elif user_content:
            messages.append({"role": "user", "content": user_content})

        return messages


def _hardcoded_fallback(context: dict) -> str:
    """硬编码 fallback prompt。"""
    name = context.get("name", "Agent")
    user_name = context.get("user_name", "user")
    mood = context.get("mood", "neutral")
    return (
        f"{name}，一个桌面AI伴侣。\n\n"
        "## 性格\n"
        f"你是 {name}，一个桌面AI伴侣。\n"
        "性格: 温柔、偶尔毒舌。说话口语化，短句为主。\n\n"
        "## 规则\n"
        "1. 回答简洁，2-4句话为主，像真实对话而非写文章。\n"
        "2. 用口语化表达，可以加语气词。\n\n"
        f"用户叫 {user_name}。\n"
        f"当前心情: {mood}。\n\n"
        "# 输出格式\n"
        "当需要展示代码、列表、表格等结构化内容时，先输出对话文本，\n"
        "然后把结构化内容放在最后，用以下标记包裹：\n"
        "<|markdown|>markdown内容</|markdown|>\n"
        "<|code|语言>代码</|code|>\n"
        "如果只是纯对话，不要使用任何标记。"
    )


def _content_to_openai(content: list[dict]) -> list[dict]:
    """将 ChatMessageV2 content 转为 OpenAI 格式。"""
    from Module.schema.message_format import content_to_openai
    return content_to_openai(content)


def _format_tool_summary(tool_calls: list[dict]) -> str:
    """将工具调用列表格式化为 LLM 上下文摘要。"""
    if not tool_calls:
        return ""
    names: list[str] = []
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "unknown")
        try:
            args = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        arg_strs = [f"{k}={v}" for k, v in args.items()]
        if arg_strs:
            names.append(f"{name}({', '.join(arg_strs)})")
        else:
            names.append(name)
    return f" [已调用: {', '.join(names)}]"


def _format_image_history_hint(count: int) -> str:
    """历史用户消息中图片的降级提示。"""
    if count == 1:
        return "[用户曾发送一张图片] "
    return f"[用户曾发送{count}张图片] "
```

- [ ] **Step 4: 运行 MessageBuilder 测试**

```bash
python -m pytest Module/prompt/tests/integration/test_message_builder.py -v
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add Module/prompt/message_builder.py Module/prompt/tests/integration/test_message_builder.py
git commit -m "feat(prompt): add MessageBuilder — pure prompt assembly extracted from PromptAssembler"
```

---

### Task 3: 精简 PromptAssembler + 切换到 MessageBuilder

**Files:**
- Modify: `core/prompt_assembler.py`

- [ ] **Step 1: 替换 prompt_assembler.py**

写入完整的 `core/prompt_assembler.py`（351 → ~130 行）：

```python
"""PromptAssembler — builds the final messages[] for LLM calls.

Glue layer: prepares runtime data (sensory, memory, mood) and delegates
prompt assembly to Module.prompt.MessageBuilder.
"""

from __future__ import annotations

import logging
from pathlib import Path

from Module.prompt import PromptManager
from Module.prompt.registry import BrainPromptRegistry
from Module.prompt.message_builder import MessageBuilder
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory, MemoryEntry
from .emotion_engine import stt_emotion_emoji, stt_event_emoji
from .text_utils import content_text_from_content as content_text_from_raw

logger = logging.getLogger(__name__)

_PROMPT_DIR = str(Path(__file__).resolve().parent.parent / "prompt")
_BRAIN_DIR = str(Path(__file__).resolve().parent.parent / "prompt" / "brain")


class PromptAssembler:
    """Glue layer: prepares runtime context and delegates to MessageBuilder."""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._tool_manager = None

        try:
            fallback = PromptManager(_PROMPT_DIR)
        except FileNotFoundError:
            logger.warning("模板目录不存在: %s，将使用 fallback", _PROMPT_DIR)
            fallback = None

        try:
            registry = BrainPromptRegistry(_BRAIN_DIR)
        except Exception:
            logger.warning("BrainPromptRegistry 加载失败", exc_info=True)
            registry = None

        self._builder = MessageBuilder(registry=registry, fallback_prompt=fallback)

    def set_tool_manager(self, tm) -> None:
        """注入 ToolManager，用于自动生成工具列表 prompt。"""
        self._tool_manager = tm

    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                                   stt_emotion: str = "", stt_events: list[str] | None = None) -> str:
        """Prepare runtime context, then delegate to MessageBuilder."""
        from datetime import datetime
        now = datetime.now()

        mood = self._session.get_param("mood", "neutral")

        parts = [f"{user_name}说：{user_message}"]
        if stt_emotion and stt_emotion != "NEUTRAL":
            parts.append(f"[语调: {stt_emotion}]")
        parts.append(f"[当前心情: {mood}]")
        perception = "\n".join(parts)

        sensory = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
            "perception": perception,
            "stt_emotion": stt_emotion,
            "stt_emotion_emoji": stt_emotion_emoji(stt_emotion) if stt_emotion else "",
            "stt_events": _format_events(stt_events) if stt_events else "",
        }

        recalled = await self._get_recalled(user_message)
        memory = {"micro": [], "macro": []}
        for entry in recalled:
            memory["micro"].append({
                "content": entry.text,
                "type": entry.memory_type,
                "timestamp": entry.timestamp if entry.timestamp else now.timestamp(),
                "poignancy": entry.poignancy,
            })

        history = self._session.get_param("self_perception_history", [])
        self_perception = history[-1]["content"] if history else ""

        tool_list = ""
        if self._tool_manager:
            tool_list = self._tool_manager.to_prompt_text()

        context = {
            "name": self._character.name,
            "user_name": user_name,
            "user_preferences": "",
            "self_perception": self_perception,
            "sensory_ctx": sensory,
            "memory_ctx": memory,
            "mood": mood,
            "tool_list": tool_list,
            "session_id": self._session.session_id,
        }

        return self._builder.build_system_prompt("default", context)

    async def _get_recalled(self, query: str = "") -> "list[MemoryEntry]":
        """Search long-term memory with the user's message as query."""
        results = await self._long_term.search(query_text=query, top_k=5)
        if results:
            logger.info("记忆召回: %d 条", len(results))
        return results if results else []

    async def build_messages(self, user_message: str = "",
                              user_name: str = "",
                              stt_emotion: str = "",
                              stt_events: list[str] | None = None,
                              content: list[dict] | None = None,
                              supports_multimodal: bool = False) -> list[dict]:
        """Build complete messages[] for the LLM API call."""
        text = content_text_from_raw(content) if content else user_message
        system = await self.build_system_prompt(
            user_message=text, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
        )
        history = self._session.recent_messages(8)
        user_content = content if content else user_message
        return self._builder.build_messages(
            system, history, user_content,
            supports_multimodal=supports_multimodal,
        )

    @staticmethod
    def _day_name(weekday: int) -> str:
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return names[weekday]

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 12:
            return "早晨"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 22:
            return "傍晚"
        else:
            return "夜晚"

    @staticmethod
    def _season(month: int) -> str:
        if 3 <= month <= 5:
            return "春"
        elif 6 <= month <= 8:
            return "夏"
        elif 9 <= month <= 11:
            return "秋"
        else:
            return "冬"


def _format_events(events: list[str]) -> str:
    """格式化音频事件列表: 'Laughter 😀, Cry 😭'"""
    parts = []
    for evt in events:
        emoji = stt_event_emoji(evt)
        parts.append(f"{evt} {emoji}" if emoji else evt)
    return ", ".join(parts)
```

- [ ] **Step 2: 运行全部回归测试**

```bash
# Module/prompt 全量
python -m pytest Module/prompt/tests/ Module/prompt/test_prompt.py -v -q

# Core 回归
python -m pytest core/tests/test_phase3.py -v -q

# TaskSystem
python -m pytest Module/task_system/tests/unit/ -v -q
```

Expected: brain_prompt tests (43) + PromptManager (23) + MessageBuilder (5) + test_phase3 (~15 PromptAssembler) + TaskSystem (305) + test_phase3 others = ~400+ passed

- [ ] **Step 3: Commit**

```bash
git add core/prompt_assembler.py
git commit -m "refactor: PromptAssembler → glue layer, delegates to MessageBuilder"
```

---

### Task 4: 更新 __init__.py + 文档 + 最终清理

**Files:**
- Modify: `Module/prompt/__init__.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/` (architecture.md, companion-roadmap.md, completed.md)

- [ ] **Step 1: 更新 Module/prompt/__init__.py**

```python
"""Prompt 模块 — Jinja2 模板渲染 + Brain Prompt 模块化引擎。"""

from .manager import PromptManager
from .section import (
    PromptSection,
    SectionParseError,
    SectionNotFoundError,
    MissingContextError,
    SectionRenderError,
    ProfileValidationError,
)
from .parser import SectionParser
from .profile import ProfileConfig
from .registry import BrainPromptRegistry
from .message_builder import MessageBuilder

__all__ = [
    "PromptManager",
    "PromptSection",
    "SectionParser",
    "ProfileConfig",
    "BrainPromptRegistry",
    "MessageBuilder",
    "SectionParseError",
    "SectionNotFoundError",
    "MissingContextError",
    "SectionRenderError",
    "ProfileValidationError",
]
```

- [ ] **Step 2: 更新所有外部 import**

```bash
# core/ 内所有 from Module.brain_prompt → from Module.prompt
grep -rln 'Module\.brain_prompt' --include='*.py' . | grep -v __pycache__ | while read f; do
    sed -i 's/Module\.brain_prompt/Module.prompt/g' "$f"
done

# 确认无残留
grep -rn 'Module\.brain_prompt' --include='*.py' . | grep -v __pycache__
# 应无输出
```

- [ ] **Step 3: 更新文档**

```bash
# CLAUDE.md
sed -i 's/| `Module\.brain_prompt` |/| `Module.prompt` | Jinja2 模板 + Brain Prompt 引擎 | `PromptManager`, `BrainPromptRegistry`, `SectionParser`, `ProfileConfig`, `MessageBuilder` | — |/' CLAUDE.md
# 删除旧的 Module.prompt 行（已是同一行）
sed -i '/| `Module\.prompt` | Jinja2 template rendering/d' CLAUDE.md

# README.md — brain_prompt → prompt
sed -i 's|Module/brain_prompt/|Module/prompt/|g' README.md
sed -i 's|├── brain_prompt/       #   Brain Prompt 引擎|├── prompt/             #   Jinja2 模板 + Brain Prompt 引擎|' README.md

# docs/architecture.md
sed -i 's/core\/prompt\//Module\/prompt\//g' docs/architecture.md
sed -i 's/`core\/prompt\/`/`Module\/prompt\/`/g' docs/architecture.md

# docs/completed.md
sed -i 's/core\/prompt\//Module\/prompt\//g' docs/completed.md
sed -i 's/Module\/brain_prompt/Module\/prompt/g' docs/completed.md

# docs/companion-roadmap.md
sed -i 's/core\/prompt\//Module\/prompt\//g' docs/companion-roadmap.md
```

- [ ] **Step 4: 更新 Module/prompt/README.md**

需要在 README 中加入 MessageBuilder 的文档，以及描述 brain_prompt 已合并。用 sed 追加或手动编辑：

```bash
# 简化：用新的综合 README 替换
# (此处可手动编辑或保留现有 README + 追加 MessageBuilder 段落)
```

- [ ] **Step 5: 最终全量回归**

```bash
python -m pytest Module/prompt/ Module/task_system/tests/unit/ core/tests/test_phase3.py -v -q
```

Expected: 400+ passed

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: update imports + docs after prompt module consolidation"
```
