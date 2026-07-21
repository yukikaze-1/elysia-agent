> 文档导航：[项目文档](../../README.md)

# CharacterEngine — 角色性格 + 情绪引擎

合并原 PersonalityEngine + EmotionEngine，四层性格模型 (Core→Self→State→Mood)，让 AI 角色能从对话经历中演化性格。

## 架构

```
┌──────────────────────────────────────────────────────┐
│ L1: Core（核心底色）                  变化: 从不       │
│   文件: character/{name}_personality.txt             │
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
    → 读取 elysia_personality.txt
    → CharacterEngine.__init__(base_personality=L1, state=CharacterState(...))

每轮对话:
  PromptAssembler.build_system_prompt()
    → _refresh_personality_data()
      → engine.render_c1()  → C1 section → c1_personality.j2
      → engine.render_c9()  → C9 section → c9_self_perception.j2

L3 更新:
  CharacterReflector._reflect() → 收集 10 个上下文变量
    → CharacterReflector.j2 渲染（含 L1/L2/L3/L4 全层上下文）
    → LLM 反思（区分情境反应 vs 人格偏移）
    → engine.consume_trigger("reflector", {"content": "..."})
      → _set_state_l3() → CharacterState.state_l3
      → _maybe_compress() → 检查触发条件 → _compress()（条件满足时）

L2 更新（L3→L2 压缩）:
  state_history 新增 >= 5 条 OR 距上次 > 7 天 → 触发
    → _compress() → LLM 总结 → CharacterState.self_l2
    → C1 模板开始渲染 <long_term_self> 块
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
{% if awareness_text %}          {# L3 State，AwarenessTrigger 判定 #}
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

### AwarenessTrigger（C9 激活判定）

C9 不每轮渲染。触发条件：
1. `state_l3` 非空
2. 距上次触发超过 `AWARENESS_COOLDOWN_SECONDS`（默认 3600s = 1h）
3. 冷却时间戳持久化到 `session.params["last_awareness_time"]`，重启不丢失

Phase 2 加入 L2/L3 语义偏差检测和用户话题触发。

### 向上回流

- L4 → L3：连续 N 轮某情绪占比 > 阈值 → LLM 总结为 L3 状态描述（Phase 2 待实现）
- L3 → L2：state_history 新增 >= 5 条或距上次 > 7 天 → LLM 提炼 + 去噪 + 合并入 L2（已实现）

## 文件布局

```
core/personality/
├── __init__.py        — 重导出 CharacterEngine, CharacterReflector
├── engine.py          — CharacterEngine 类（合并 PersonalityEngine + EmotionEngine）
├── reflector.py       — CharacterReflector 类（子组件）
├── tests/
│   ├── test_engine.py
│   └── test_reflector.py
└── README.md

关联文件:
character/
├── elysia.yaml                    — 角色配置（含 base_personality_file）
└── elysia_personality.txt         — L1 Core 文本

prompt/
├── CharacterReflector.j2           — 性格反思模板
└── PersonalityCompression.j2       — L3→L2 压缩模板

core/
├── agent.py                       — 创建并接线 CharacterEngine
├── prompt_assembler.py            — 数据注入 section
├── schema/character.py            — CharacterState 定义（Pydantic 模型，合并 mood + reflector 字段）
└── schema/mood.py                 — STT emoji 工具函数（Mood 已迁为 str）

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
3. 自动推导 → `yaml_dir / {name}_personality.txt`
4. 都找不到 → WARN，C1 不含 core 文本

## 调试

访问 `http://localhost:8765/debug` → **CharacterEngine** 卡片，可查看：
- L2 Self 当前状态（L3→L2 压缩产出）
- L3 State 当前状态
- C9 冷却状态（就绪 / 剩余 X min）
- State 历史（最近 5 条）
- 压缩追踪：上次压缩时间 / 上次压缩时 history 条数

后端 API: `GET /debug/api` → `info.character_engine_exists` / `info.self_l2` / `info.state_l3` / `info.state_history`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 提示词组装 |
| [character/README.md](../../character/README.md) | 角色配置说明 |
