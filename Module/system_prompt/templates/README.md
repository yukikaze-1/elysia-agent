# System Prompt 模板

> 文档导航：[项目文档](../../../README.md) → [Module 文档](../../README.md) → [System Prompt 文档](../README.md)

本目录包含 `SystemPromptManager` 使用的 Jinja2 模板文件。每个模板对应一个 section（prompt 子章节），由 12 个 section 类按固定顺序渲染，最终拼接为完整的 system prompt。

## 格式约定

### 外层：XML 语义标签包裹

每个模板的**渲染内容**必须用 XML 语义标签包裹，作为 section 边界标记。

```
<tagname>
... content ...
</tagname>
```

- 标签名使用 `snake_case`，与 section 名对应
- 标签必须闭合
- 标签内是模板渲染的实际内容，不含注释

目前使用的标签：`<conflict_rules>` `<output_format>` `<tts_markup>` `<sensory_input>` `<emotion>` `<memory_bank>` `<personality>` `<relationship>` `<speaking_style>` `<proactive>` `<own_life>` `<self_perception>`

### 内层：Markdown / 纯文本

XML 标签内部的文本使用 Markdown 或纯文本，按内容需要选择：

- **纯文本段落**：用于描述性内容（人格、关系）
- **`- ` 无序列表**：用于并列规则（情绪传染、说话风格）
- **`1. ` 有序列表**：用于有优先级的规则（冲突规则）
- **`#` / `##` 子标题**：用于较长的结构化内容（说话风格的心情分段）

以下格式**不使用**（对 LLM 无意义，增加 token 消耗）：

- `**粗体**` `*斜体*` — LLM 不渲染文字样式
- `` `代码` `` — 无代码语义时不用
- `> 引用块` — 无引用语义时不用
- HTML 标签（除上述 section 包裹标签和内容中需 LLM 输出的标签如 `<inner_voice>` 外）

### 跨 section 引用

模板中可以引用其他 section 定义的标签名（如 `<inner_voice>`、`<facts>`、`<perception>`），LLM 通过标签名建立语义关联。

```jinja2
例：C6 中引用 C5 的标签
心里想的：感受<perception>里的情绪信号，注意到<facts>里的时间背景
```

### Jinja2 注释

模板文件使用 Jinja2 注释 `{# ... #}` 写头部元数据和实现说明。这些注释不会出现在渲染输出中。

### 条件渲染

当模板依赖的运行时数据可能为空时，使用 `{% if %}` 包裹整个 section：

```jinja2
{% if text %}
<own_life>
{{ text }}
</own_life>
{% endif %}
```

渲染空字符串的 section 会被 `SystemPromptManager` 跳过（不拼接、不添加空行）。

### 变量注入

模板变量通过 section 类的 `render()` 方法传入。变量名使用 `snake_case`。透传数据直接使用原始变量名（如 `{{ text }}`、`{{ self_perception }}`），结构化数据使用上下文对象（如 `{{ sensory_ctx.current_time }}`、`{{ memory_ctx.micro }}`）。

## 命名规则

### 文件名

```
{prefix}{index}_{name}.j2
```

- **prefix**: `f` = framework（框架规则），`c` = character（角色定义）
- **index**: 在同类中的序号，两位数字
- **name**: 与对应 section 类名的 snake_case 一致

| 文件 | Section 类 | 角色 |
|------|-----------|------|
| `f1_conflict_rules.j2` | `ConflictRulesSection` | 规则优先级 + 安全边界 |
| `f2_output_format.j2` | `OutputFormatSection` | 输出格式（inner_voice 协议 + 任务优先） |
| `f3_tts_markup.j2` | `TTSMarkupSection` | TTS 合成标记 + 使用限制 |
| `c1_personality.j2` | `PersonalitySection` | 人格（L1 + L2，数据驱动） |
| `c2_relationship.j2` | `RelationshipSection` | 关系（四阶段发展 + 边界处理） |
| `c3_emotion.j2` | `EmotionSection` | 情感（8 种情绪响应规则） |
| `c4_memory.j2` | `MemorySection` | 记忆（长期记忆召回 + 可靠性规则） |
| `c5_sensory.j2` | `SensorySection` | 感官输入（环境事实 + 用户消息 + 语音情绪） |
| `c6_speaking_style.j2` | `SpeakingStyleSection` | 说话风格（五模式切换 + 称呼/调情/夸奖） |
| `c7_proactive.j2` | `ProactiveSection` | 主动性（何时主动 + 冷却机制） |
| `c8_own_life.j2` | `OwnLifeSection` | 自己的生活 |
| `c9_self_perception.j2` | `SelfPerceptionSection` | 自我感知（AwarenessTrigger 条件激活） |

### 文件头部

每个模板文件以 Jinja2 注释块开头，包含：

```jinja2
{#
@section 英文名（与 section.name 一致）
@version 语义化版本
@updated YYYY-MM-DD（最后修改日期）
@description 一句话描述这个 section 是什么
@params varname: type          # 参数说明（可选）
@note 可选备注
#}
```

- `@params` 每行一个，格式为 `变量名: 类型  # 说明`
- 类型可用中文描述（如 `str | None`、`dict[str, ...]`）
- `@note` 用于标注已知限制、待办事项、设计决策

## 渲染顺序

模板按 `SystemPromptManager._RENDER_ORDER` 排序后拼接，顺序是有意设计的：

```
F1 ConflictRules    → 先建立规则优先级 + 安全边界
F2 OutputFormat     → 定义输出格式 + 任务优先规则
F3 TTSMarkup        → TTS 合成标记 + 使用限制
C2 Relationship     → 关系（四阶段发展 + 边界处理）
C7 Proactive        → 主动性（何时主动 + 冷却机制）
C6 SpeakingStyle    → 说话风格（五模式切换 + 夸奖/调情/称呼规则）
C1 Personality      → 人格（L1 Core + L2 Self，数据驱动）
C9 SelfPerception   → 自我感知（AwarenessTrigger 条件激活）
C8 OwnLife          → 自己的生活（可能为空）
C3 Emotion          → 情绪（8 种情绪响应规则）
C4 Memory           → 记忆（长期记忆召回 + 可靠性规则）
C5 Sensory          → 感官输入（当前环境上下文）
```

## 添加新模板

1. 在 `templates/` 新建 `.j2` 文件，按命名规则命名
2. 在 `sections/` 新建 section 类，继承 `Jinja2Section`，设置 `name`/`version`/`description`
3. 在 `system_prompt_manager.py` 的 `_RENDER_ORDER` 中添加 section name
4. 在 `core/prompt/prompt_assembler.py` 中创建 section 实例并传入模板路径
5. 在 `tests/` 中添加对应的单元测试

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [System Prompt 文档](../README.md) | System Prompt 引擎 + Section Provider |
| [Module 文档](../../README.md) | 所有 Module 服务总览 |
