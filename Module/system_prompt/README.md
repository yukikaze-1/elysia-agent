# Prompt Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

System prompt 引擎 + 通用 Jinja2 模板渲染。提供两套 API：

- **SystemPromptManager** — Section Provider 架构：12 个独立 `PromptSection`，自管数据源/模板/生命周期，中央编排器按序组装 system prompt
- **PromptManager** — 通用 Jinja2 渲染器：用于非 system prompt 模板（Amygdala、ActiveSpeak 等）

## 文件结构

```
Module/system_prompt/
├── __init__.py                # 导出 PromptManager, PromptSection, StaticSection, Jinja2Section
├── protocol.py                # PromptSection ABC + StaticSection + Jinja2Section
├── system_prompt_manager.py   # SystemPromptManager 编排器
├── manager.py                 # PromptManager — 通用 Jinja2 渲染器
├── README.md
├── sections/                  # 12 个独立 PromptSection 模块
│   ├── conflict_rules.py      #   F1 框架优先
│   ├── output_format.py       #   F2 输出格式 (inner_voice 双轨协议)
│   ├── tts_markup.py          #   F3 TTS 合成标记
│   ├── sensory.py             #   C5 感官输入 (时间/STT情绪)
│   ├── emotion.py             #   C3 当前心情 + 情绪传染
│   ├── memory.py              #   C4 记忆检索
│   ├── personality.py         #   C1 人格
│   ├── relationship.py        #   C2 关系
│   ├── speaking_style.py      #   C6 说话风格
│   ├── proactive.py           #   C7 主动性 (骨架)
│   ├── own_life.py            #   C8 自己的生活
│   └── self_perception.py     #   C9 自我认知
├── templates/                 # 共享 .j2 模板 + per-role 角色子目录（阶段 B-2）
│   ├── f1_conflict_rules.j2
│   ├── f2_output_format.j2
│   ├── f3_tts_markup.j2
│   ├── c1_personality.j2
│   ├── c2_relationship.j2     # 共享（角色无专属模板时回退到这些）
│   ├── c3_emotion.j2
│   ├── c4_memory.j2
│   ├── c5_sensory.j2
│   ├── c6_speaking_style.j2
│   ├── c7_proactive.j2
│   ├── c8_own_life.j2
│   ├── c9_self_perception.j2
│   ├── elysia/                # Elysia 专属 C2/C3/C7（字节 == 共享原版，byte-for-byte 保护）
│   │   ├── c2_relationship.j2
│   │   ├── c3_emotion.j2
│   │   └── c7_proactive.j2
│   └── cyrene/                # Cyrene 专属 C2/C3/C7（去 Elysia 化，阶段 B-2 新建角色）
│       ├── c2_relationship.j2
│       ├── c3_emotion.j2
│       └── c7_proactive.j2
└── tests/
    ├── test_prompt.py
    ├── unit/
    │   ├── test_protocol.py
    │   ├── test_system_prompt_manager.py
    │   └── test_sections/
    │       └── test_boundary.py
    └── integration/
        └── test_full_render.py
```

## Section Provider 架构

### 核心概念

每个 `PromptSection` 是 system prompt 中的一个独立区块。它：
- **自管数据源** — 持有 SessionMemory、LongTermMemory 等引用
- **自管模板** — `Jinja2Section` 持有独立 `.j2` 模板，有自己的 `jinja2.Environment`
- **自管生命周期** — `render()` / `reload()` / `validate()`

### 类层次

```
PromptSection (ABC)
└── Jinja2Section          # .j2 模板 — 全部 12 个 section（含 ConflictRules F1）
```

### 数据流

```
PromptAssembler.build_system_prompt(user_message, user_name, stt_emotion, stt_events)
  → SystemPromptManager.set_context(**kwargs)     # 扇出到所有 section
  → SystemPromptManager.render()                  # 按 _RENDER_ORDER 依次渲染
  → 用 \n\n 拼接所有非空区块
```

### 渲染顺序

为最大化 DeepSeek prefix cache 命中率，按 `_RENDER_ORDER` 分组：

**静态前缀**（低变化频率）→ **动态后缀**（每轮更新）：

```
F1 ConflictRules → F2 OutputFormat → F3 TTSMarkup →
C2 Relationship → C7 Proactive → C6 SpeakingStyle →
C1 Personality → C9 SelfPerception → C8 OwnLife →
C3 Emotion → C4 Memory → C5 Sensory
```

## SystemPromptManager API

```python
from Module.system_prompt.sections import *
from Module.system_prompt.system_prompt_manager import SystemPromptManager

sections = [
    ConflictRulesSection(),
    OutputFormatSection(),
    PersonalitySection("Elysia"),
    # 每个 Jinja2Section 子类通过 name 属性自动推导模板路径
    # ...
]
mgr = SystemPromptManager(sections)

# 注入 per-request 上下文
mgr.set_context(user_message="...", user_name="妖梦", ...)

# 渲染完整 system prompt
prompt = await mgr.render()

# 版本管理
versions = mgr.get_versions()    # {"ConflictRules": "1.0.0", ...}
mgr.reload_all()                 # 热重载所有模板
failed = mgr.validate_all()      # 校验失败的 section 名列表

# 按预算渲染（S2-H9）：超预算先减 C4 记忆条数（5→3→2→1→0），
# 仍超则按 _DROPPABLE_SECTIONS 顺序整节丢；_PROTECTED_SECTIONS 永不丢
prompt = await mgr.render(budget_tokens=4096)
```

### 预算与截断（S2-H9）

预算的唯一执行点是 [`budget.py`](budget.py)（估算 + 换算 + 常量），两个截断点各管一侧：

| 侧 | 位置 | 策略 |
|---|---|---|
| system prompt | `SystemPromptManager.render(budget_tokens=…)` | C4 记忆逐条降（`set_context(memory_limit=n)`）→ 按 `_DROPPABLE_SECTIONS`（OwnLife → SelfPerception → Sensory → Emotion → Memory）整节丢；`_PROTECTED_SECTIONS`（F1/F2/F3/C1/C2/C6/C7）永不丢 |
| 历史 | `MessageBuilder.build_messages(prompt_budget_tokens=…)` | 由旧到新**逐条**丢到装得下为止；system 与当前用户消息永不丢 |

预算 = `providers.yaml::llm.<provider>.context_window` × 0.75（余量给输出/工具/估算误差）；
未声明 → 保守默认 32K。**估算用字符启发式**（CJK 1 字≈1 token，其余 4 字符≈1 token），
是预算不是账本——真实用量以 provider usage 为准（`TokenTracker`）。

## PromptManager API

用于非 system prompt 的模板渲染（CharacterReflector、AwarenessJudge、SpeakingStyleEdit 等）。

```python
from Module.system_prompt import PromptManager

pm = PromptManager(str(Path(__file__).parent / "prompts"))
result = pm.render("CharacterReflector.j2", character_name="Elysia")
system = pm.render_macro("SpeakingStyleEdit.j2", "SpeakingStyleEditPrompt", **vars)
```

## 自定义 Filter

### `| datetime`

Unix 时间戳 → 日期字符串 `%Y-%m-%d %H:%M:%S`。

`PromptManager` 和 `Jinja2Section` 均注册了此 filter。

```jinja2
{{ mem.timestamp | datetime }}
{# → "2025-06-19 00:00:00" #}
```

## 测试

```bash
# 全部 prompt 测试
python -m pytest Module/system_prompt/tests/ -v
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/system_prompt/templates/README.md](templates/README.md) | 模板格式约定 |
| [core/prompts/README.md](../../core/prompts/README.md) | Prompt 模板目录说明 |
| [docs/architecture.md](../../docs/architecture.md) | 提示词组装架构 |
