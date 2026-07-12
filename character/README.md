# Character 角色配置目录

每个角色一个子目录，包含该角色的 YAML 配置和 L1 Core 性格文本。

```
character/
├── README.md
└── elysia/                   ← 角色子目录
    ├── config.yaml           ← 角色 YAML 配置
    └── personality.txt       ← L1 Core 性格文本（自然语言）
```

## 目录结构规范

```
character/<角色名>/
├── config.yaml               # 必需：角色 YAML 配置（格式见下方）
└── personality.txt           # 必需：L1 Core 文本（base_personality_file 指向此文件）
```

`<角色名>` 使用小写英文，与 `config.yaml` 中的 `name` 字段对应（name 可包含大写）。

## 字段说明

### config.yaml

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✓ | 角色显示名（注入 system prompt） |
| `user_pronoun` | string | — | 用户人称代词，默认 "她" |
| `base_personality_file` | string | ✓ | L1 Core 文本文件名，相对于 config.yaml 目录 |
| `voice` | string | — | [占位] TTS voice ID。MiniMax/GPT-SoVits 音色由各自的 voices.yaml 管理 |
| `emotion.thresholds` | dict | — | 情绪触发阈值（excitement/sadness 等） |
| `emotion.idle_expressions` | list | — | Live2D 闲置表情列表 |
| `proactive.enabled` | bool | — | 是否启用主动搭话 |
| `proactive.interval` | int | — | 空闲检查间隔（秒） |
| `proactive.triggers` | list | — | 触发条件列表 |
| `wake_words` | list | — | 语音唤醒词列表 |

### personality.txt

- 自然语言撰写的 L1 Core 性格描述（Core 层，不可变）
- 由 PersonalityEngine 在启动时加载，注入 C1 system prompt
- 不包含 "你是 XXX。"——角色名由模板的 `{{ name }}` 变量渲染

## 数据流

```
character/elysia/config.yaml
  → CharacterConfig.from_yaml()
    → _load_base_personality() 读取 personality.txt
      → config.base_personality = "温柔是你的底色..."
        → PersonalityEngine.__init__(base_personality=...)
          → engine.render_c1() → C1 section → system prompt
```

## 添加新角色

1. 创建 `character/<name>/` 目录
2. 编写 `config.yaml`（至少包含 `name` 和 `base_personality_file`）
3. 编写 `personality.txt`（L1 Core 性格文本）
4. 在 `config.yaml` 中将 `agent.character` 指向新角色的 config.yaml
5. 在对应 TTS 的 voices.yaml 中添加音色映射（如需语音输出）
