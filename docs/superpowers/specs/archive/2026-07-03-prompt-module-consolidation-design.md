# Prompt 模块合并设计

**日期**: 2026-07-03
**状态**: 待实现
**范围**: brain_prompt 合并到 Module/prompt/，prompt_assembler 核心逻辑抽入 Module/prompt/message_builder.py

---

## 动机

当前 prompt 相关代码分散在三个位置：

```
Module/prompt/          — PromptManager（通用 Jinja2 渲染）
Module/brain_prompt/    — BrainPromptRegistry（section 管理引擎）
core/prompt_assembler.py — PromptAssembler（数据准备 + messages 组装，317 行）
```

目标：统合到 `Module/prompt/` 下，同时将 `prompt_assembler.py` 中与 `core/` 无关的纯组装逻辑抽出为 `MessageBuilder`，`prompt_assembler` 精简为胶水层。

---

## 最终目录结构

```
Module/prompt/
├── __init__.py           # 合并导出
├── manager.py            # PromptManager（已有）
├── message_builder.py    # NEW — MessageBuilder 纯 prompt 组装
├── section.py            # ← brain_prompt/section.py
├── parser.py             # ← brain_prompt/parser.py
├── profile.py            # ← brain_prompt/profile.py
├── registry.py           # ← brain_prompt/registry.py
├── README.md             # 更新
├── __main__.py           # 已有
├── test_prompt.py        # 已有 — PromptManager 测试
└── tests/
    ├── __init__.py
    ├── unit/
    │   ├── test_section.py
    │   ├── test_parser.py
    │   ├── test_profile.py
    │   └── test_registry.py
    └── integration/
        ├── test_golden_diff.py
        ├── test_render_full.py
        └── test_message_builder.py   # NEW

Module/brain_prompt/     → 删除
```

---

## MessageBuilder

纯 prompt 组装，无状态，不依赖 `core/` 的任何组件。

```python
class MessageBuilder:
    """纯 prompt 组装 — 接收 context + history → 输出 messages[]。
    
    无状态，不依赖 core/ 组件。
    """

    def __init__(self, registry: BrainPromptRegistry | None = None,
                 fallback_prompt: PromptManager | None = None):
        self._registry = registry
        self._fallback = fallback_prompt

    def build_system_prompt(self, profile: str = "default",
                            context: dict[str, Any] | None = None) -> str:
        """三层 fallback: Registry → Brain.j2 → hardcoded."""

    def build_messages(self, system_prompt: str, history: list[dict],
                       user_content: str | list[dict] | None = None,
                       *, supports_multimodal: bool = False) -> list[dict]:
        """组装 messages[]。
        
        处理 inner_voice 注入、tool_summary 格式化、多模态降级。
        """
```

### `build_system_prompt`

```python
def build_system_prompt(self, profile="default", context=None) -> str:
    if context is None:
        context = {}

    # Tier 1: Registry
    if self._registry is not None:
        try:
            return self._registry.render(profile=profile, context=context)
        except Exception:
            pass

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
            pass

    # Tier 3: hardcoded
    return _hardcoded_fallback(context)
```

### `build_messages`

- 将 system_prompt 置于 `messages[0]`
- 遍历 history，注入 `inner_voice` 前缀和 `tool_summary`
- 多模态内容：支持 LLM 原生传输，纯文本 LLM 降级为工具提示
- 追加当前用户消息

逻辑从 `core/prompt_assembler.py` 现有 `build_messages` 复制，去重 `_format_tool_summary`、`_format_image_history_hint`。

---

## PromptAssembler 精简

`core/prompt_assembler.py` 从 317 行精简为 ~40 行胶水：

```python
class PromptAssembler:
    def __init__(self, character, session, long_term):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._tool_manager = None
        registry = BrainPromptRegistry("prompt/brain")
        try:
            fallback = PromptManager("prompt/")
        except FileNotFoundError:
            fallback = None
        self._builder = MessageBuilder(registry, fallback)

    def set_tool_manager(self, tm) -> None:
        self._tool_manager = tm

    async def build_system_prompt(self, user_message="", user_name="",
                                   stt_emotion="", stt_events=None) -> str:
        # 数据准备（sensory/memory/mood/self_perception/tool_list）
        context = {...}
        return self._builder.build_system_prompt("default", context)

    async def build_messages(self, ...) -> list[dict]:
        system = await self.build_system_prompt(...)
        history = self._session.recent_messages(8)
        return self._builder.build_messages(system, history, user_content, ...)
```

保留 `_day_name`、`_time_of_day`、`_season` 静态方法——它们是 core 层特有的时间逻辑。

---

## import 变更

| 文件 | 旧 | 新 |
|------|----|----|
| `core/prompt_assembler.py` | `from Module.brain_prompt import` | `from Module.prompt import` |
| `core/prompt_assembler.py` | 内部 message 组装逻辑 | → `self._builder.build_messages()` |
| `core/agent.py` | `from .prompt_assembler import` | 不变 |
| `core/cognitive_loop.py` | `from .prompt_assembler import` | 不变 |
| `Module/prompt/__init__.py` | 只导出 `PromptManager` | 导出 `PromptManager` + `MessageBuilder` + `BrainPromptRegistry` + ... |
| 测试文件 | `from Module.brain_prompt` | `from Module.prompt` |
| 测试文件 | `Module/brain_prompt/tests/` | `Module/prompt/tests/` |

---

## 删除

- `Module/brain_prompt/` — 整个目录

---

## 验证

```bash
# Module/prompt 全量测试（含迁入 + 新建）
python -m pytest Module/prompt/tests/ -v

# 黄金对比 + 现有 PromptManager 测试
python -m pytest Module/prompt/test_prompt.py -v

# Core 回归
python -m pytest Module/task_system/tests/unit/ -v
python -m pytest core/tests/test_phase3.py -v
```

预期: 394+ tests pass（45 brain_prompt + 23 Manager + ~6 MessageBuilder + 305 TaskSystem + ~15 PromptAssembler）
