> 文档导航：[项目文档](../../README.md)

# CharacterEngine — 角色性格 + 情绪引擎

合并原 PersonalityEngine + EmotionEngine，四层性格模型 (Core→Self→State→Mood)，让 AI 角色能从对话经历中演化性格。

## 接口层（`protocol.py`）

**消费方依赖的是契约，不是 `engine.py` 的具体类。**

```python
from core.personality.protocol import CharacterEngineProtocol, CharacterReflectorProtocol
```

| 协议 | 覆盖 | 实现 |
|---|---|---|
| `CharacterEngineProtocol` | 渲染（C1/C6/C9）、事件（`consume_trigger`）、消息缓冲、情绪分析、演化状态生命周期 | `CharacterEngine` |
| `CharacterReflectorProtocol` | 反思缓冲/固化引用/复位/恢复 + 遥测 | `CharacterReflector` |

**为什么是 `typing.Protocol` 而不是 ABC**：ABC 要求替换实现继承本模块，而「引擎可能被
单独迭代或整体替换」正是这层的理由——替换方不该反向依赖 `core.personality`。
结构性契约让任何形状相符的实现直接接入。

代价是**没有实例化期强制**（ABC 少实现一个方法会 `TypeError`，Protocol 不会），由两道补上：

1. `make pyright` 在「具体实现 → 接口类型」的赋值点做结构校验（`Agent.character_engine`
   的返回类型就是接口；建立本层时它当场抓出 `buffer_message` 的 `reflector` 参数比契约更窄）；
2. `core/tests/test_character_engine_protocol.py` 逐项断言成员齐备 + 方法签名一致，
   并守住「引擎**不得继承**协议」——继承会让协议里 `...` 的空方法体变成静默 no-op，
   漏实现反而不再报错。

**演化状态生命周期**是这层收口的核心：消费方此前必须分别戳 `_reflector._reflect_lock`、
`_compression._compression_lock`、`_last_awareness_time`、`_awareness._last_awareness_time`
才能完成「busy 判定 / 复位 / 恢复」三件事，现在各自是一个语义方法：

| 方法 | 语义 |
|---|---|
| `evolution_in_flight()` | 是否有反思/压缩在途（角色替换的快速拒绝路径） |
| `reset_evolution_state()` | 复位反思缓冲/上下文/遥测 + 觉察冷却 |
| `restore_evolution_state(last_awareness_time=...)` | 实例/会话切换后恢复 |
| `reflector` / `version_repo` | 只读访问面 |

## 架构

```
┌──────────────────────────────────────────────────────┐
│ L1: Core（核心底色）                  变化: 从不       │
│   文件: character/{name}/personality.md              │
│   启动加载，不可修改                                  │
│   → C1 模板的锚点                                    │
├──────────────────────────────────────────────────────┤
│ L2: Self（长期自我）                变化: 月级         │
│   存储: CharacterState.self_l2                       │
│   从 L3 持续状态压缩沉淀（已实现）    │
│   → C1 模板的主体——"我是谁"                           │
├──────────────────────────────────────────────────────┤
│ L3: State（短期状态）              变化: 数十轮/24h    │
│   存储: CharacterState.state_l3                      │
│   来源: CharacterReflector + 事件触发                 │
│   → C9 模板的条件素材——"我最近怎么了"                  │
├──────────────────────────────────────────────────────┤
│ L4: Mood（当前情绪）                变化: 每轮         │
│   存储: CharacterState.mood (str, 自由文本)          │
│   来源: LLM `<mood>` 标签（主回复中内联），关键词为 fallback │
│   → C3 模板的内容                                    │
└──────────────────────────────────────────────────────┘
```

## 数据流

```
启动:
  elysia.yaml → CharacterConfig.from_yaml()
    → 读取 elysia_personality.md
    → CharacterEngine.__init__(base_personality=L1, state=session.character_state)
    → engine.state 与 session.character_state 共用同一对象（engine 是权威来源，session 负责持久化）

每轮对话:
  PromptAssembler.build_system_prompt()
    → _refresh_personality_data()
      → engine.render_c1()  → C1 section → c1_personality.j2
      → engine.render_c9()  → C9 section → c9_self_perception.j2
      → engine.render_c6()  → C6 section → c6_speaking_style.j2

L4 Mood 更新（每轮）:
  LLM 输出 <mood> → _apply_mood() → engine.state.mood
  → session.save() 持久化到 MongoDB
  → C3 模板: 当前心情: {{ mood }}

L3 更新:
  CharacterReflector._reflect() → 收集 10 个上下文变量
    → CharacterReflector.j2 渲染（含 L1/L2/L3/L4 全层上下文）
    → LLM 反思（区分情境反应 vs 人格偏移）
    → engine.consume_trigger("reflector", {"content": "..."})
      → _set_state_l3() → CharacterState.state_l3
      → _maybe_compress() → 检查触发条件 → _compress()（条件满足时）

L2 更新（L3→L2 压缩，编辑模式）:
  state_history 新增 >= 5 条 OR 距上次 > 7 天 → 触发
    → _compress() → LLM 编辑当前 L2 文档 → 返回 <new_self> + <changes>
      → CharacterState.self_l2 = new_self
      → append_version() 写入 character_versions（永久保留）
    → C1 模板开始渲染 <long_term_self> 块

C6 更新（L2 演化后联动）:
  _compress() 成功后 → _refresh_c6()
    → LLM 编辑当前 C6 文本（<new_speaking_style>）
    → CharacterState.speaking_style_c6
    → append_version(content_type="c6")
    → C6 模板条件渲染
```

## 模板渲染

C1 人格 (`Module/system_prompt/templates/c1_personality.j2`):
```jinja2
<personality>
你是 {{ name }}。               {# 来自 section.__init__ #}
{{ core_personality }}           {# L1 Core，文件加载 #}
{% if self_personality %}        {# L2 Self，条件渲染 #}
<long_term_self>
{{ self_personality }}
</long_term_self>
{% endif %}
</personality>
```

C9 自我觉察 (`Module/system_prompt/templates/c9_self_perception.j2`):
```jinja2
{% if awareness_text %}          {# L3 State，AwarenessJudge 判定 #}
<self_perception>
你最近注意到自己的一些变化：
{{ awareness_text }}
</self_perception>
{% endif %}
```

## 关键机制

### 阻尼机制

当 L3 偏移了 L1 中的特质时，L1 的表达方式优先：
- **L3 影响"说什么"（内容倾向）**，**L1 影响"怎么说"（表达方式）**
- 例：L1"嘴硬心软" + L3"敷衍/无力" → "随你怎么熬夜，药放桌上了"

Phase 1 通过 C1 文本末尾提示 LLM 遵守 L1 约束；Phase 2 移入 C6 SpeakingStyle。

### AwarenessJudge（C9 激活判定）

Phase 2: LLM-judge 语义判定。Reflector 产出 L3 后，异步 judge 比较 L2 vs L3：

1. **降级模式**：L2 为空时，L3 原文直接缓存（不调 LLM）
2. **Judge 判定**：L2 非空时，渲染 `AwarenessJudge.j2` → LLM 判断是否批准
   - `<approved>`：编辑后文本写入缓存，替换旧值
   - `<skip>`：保留旧缓存不变
3. **`render_c9()` 三重门控**：
   - 缓存非空
   - 展示次数 < 3
   - 距上次展示 >= 5 分钟

### 向上回流

- L4 → L3：连续 N 轮某情绪占比 > 阈值 → LLM 总结为 L3 状态描述（Phase 2 待实现）
- L3 → L2：state_history 新增 >= 5 条或距上次 > 7 天 → LLM 提炼 + 去噪 + 合并入 L2（已实现）

## 文件布局

```
core/personality/
├── __init__.py        — 重导出 CharacterEngine, CharacterReflector
├── protocol.py        — **接口层**：CharacterEngineProtocol / CharacterReflectorProtocol
├── engine.py          — CharacterEngine 类（合并 PersonalityEngine + EmotionEngine）
├── reflector.py       — CharacterReflector 类（子组件）
├── awareness.py       — AwarenessJudge: C9 觉察判定 + cooldown 管理
├── compression.py     — CompressionManager: L3→L2 压缩 + C6 联动编辑
├── emotion.py         — 情绪分析：LLM mood tag 解析 + keyword 回退
├── _utils.py          — 内部工具函数
├── tests/
│   ├── test_engine.py
│   └── test_reflector.py
└── README.md

关联文件:
character/
├── elysia/config.yaml              — 角色配置（含 base_personality_file）
└── elysia/personality.md           — L1 Core 文本

core/
├── prompts/
│   ├── CharacterReflector.j2           — 性格反思模板
│   ├── PersonalityCompression.j2       — L3→L2 压缩模板（LLM 编辑模式 + 结构化输出）
│   ├── AwarenessJudge.j2              — C9 觉察判定模板
│   └── SpeakingStyleEdit.j2           — C6 编辑模板
├── agent/agent.py                  — 创建并接线 CharacterEngine
├── prompt/prompt_assembler.py       — 数据注入 section
├── schema/character.py            — CharacterState 定义（Pydantic 模型，合并 mood + reflector 字段）
├── schema/mood.py                 — STT emoji 工具函数（Mood 已迁为 str）
├── schema/compression.py          — CompressionTrigger + VersionEntry 类型
└── storage/character_version_repo.py — CharacterVersionRepository（character_versions 集合，支持 l2/c6 等 content_type，append-only）

Module/system_prompt/
├── sections/personality.py        — C1 section 类
├── sections/self_perception.py    — C9 section 类
├── sections/emotion.py            — C3 section 类（通过 set_context(mood=...) 传入）
├── templates/c1_personality.j2    — C1 模板
└── templates/c9_self_perception.j2 — C9 模板
```

## L1 Core 文件路径解析

`CharacterConfig._load_base_personality()` 按以下顺序查找：

1. YAML 所在目录 → `yaml_dir / base_personality_file`
2. 项目根目录 → `cwd / base_personality_file`
3. 自动推导 → `yaml_dir / {name}_personality.md`
4. 都找不到 → WARN，C1 不含 core 文本

## 调试

访问 `http://localhost:8765/debug` → **CharacterEngine** 卡片，可查看：
- L2 Self 当前状态（L3→L2 压缩产出）
- L3 State 当前状态
- L4 Mood 当前心情（LLM 输出的自由文本）
- C9 冷却状态（就绪 / 剩余 X min）
- State 历史（最近 5 条）
- L2 / C6 版本历史（版本数 + 在线浏览 + 回滚）

后端 API: `GET /debug/api` → `info.character_engine_exists` / `info.self_l2` / `info.state_l3` / `info.state_history`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 提示词组装 |
| [character/README.md](../../character/README.md) | 角色配置说明 |
