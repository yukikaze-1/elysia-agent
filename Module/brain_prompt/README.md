# Brain Prompt Module

Brain system prompt 模块化引擎。将 Jinja2 单体模板拆分为独立 section，支持 profile 组合、A/B 分流和热更新。

## 文件结构

```
Module/brain_prompt/
├── __init__.py              # 公开 API
├── section.py               # PromptSection frozen dataclass + 5 个异常类型
├── parser.py                # SectionParser — 解析 .j2 @section 头标记
├── profile.py               # ProfileConfig — YAML profile 加载
├── registry.py              # BrainPromptRegistry — 渲染/reload/A/B/版本查询
├── README.md
└── tests/
    ├── unit/
    │   ├── test_section.py
    │   ├── test_parser.py
    │   ├── test_profile.py
    │   └── test_registry.py
    └── integration/
        ├── test_golden_diff.py    # 新旧输出逐字节对比
        └── test_render_full.py    # 完整渲染验证
```

模板数据在项目根 `prompt/brain/`：

```
prompt/brain/
├── sections/                # 9 个独立 section 文件
│   ├── identity.j2          #   角色身份 + 性格
│   ├── self_perception.j2   #   角色进化感知（CharacterReflector 产出）
│   ├── relationship.j2      #   与用户的关系
│   ├── sensory.j2           #   感官输入（时间/季节/STT 情绪）
│   ├── memory.j2            #   记忆库（召回条目）
│   ├── current_state.j2     #   当前心情
│   ├── behavior.j2          #   请求分类（A/B）+ 工具规则
│   ├── voice.j2             #   说话风格（长度/节奏/语气/克制）
│   └── output_format.j2     #   inner_voice 双轨协议 + 代码块格式
├── profiles/
│   ├── default.yaml         #   完整 profile（9 sections）
│   └── minimal.yaml         #   极简 profile（sensory + behavior + output_format）
└── metadata.yaml            #   版本历史 / section 顺序 / 依赖关系
```

## 快速开始

```python
from Module.brain_prompt import BrainPromptRegistry

# 加载
registry = BrainPromptRegistry("prompt/brain")

# 渲染 system prompt（default profile，全部 9 sections）
context = {
    "name": "Elysia",
    "user_name": "妖梦",
    "user_preferences": "",
    "self_perception": "",
    "sensory_ctx": {
        "current_time": "2024-01-01 12:00:00",
        "time_of_day": "下午",
        "day_of_week": "周一",
        "season": "冬",
        "perception": "妖梦说：Hello\n[当前心情: neutral]",
        "stt_emotion": "",
        "stt_emotion_emoji": "",
        "stt_events": "",
    },
    "memory_ctx": {"micro": [], "macro": []},
    "mood": "neutral",
    "tool_list": "",
}
system_prompt = registry.render(profile="default", context=context)
```

## 核心概念

### Section

每个 section 是一个独立的 `.j2` 文件，头部用 Jinja2 注释声明元数据：

```jinja2
{#
@section identity
@version 1.2.0
@description 角色身份、性格描述、与用户的关系
@params name, user_name, user_preferences
#}
你是 {{ name }}。你不是一个"性格温柔、偶尔毒舌的 AI"——...
```

- **`@section`** — 唯一标识名（必需）
- **`@version`** — 语义版本（必需）
- **`@description`** — 人类可读说明（必需）
- **`@params`** — 该 section 接受的模板参数，逗号分隔（必需）

### Profile

Profile 定义"用哪些 section、用哪个变体"。YAML 格式：

```yaml
name: default
description: 完整系统 prompt
enabled:
  - identity
  - self_perception
  - relationship
  - sensory
  - memory
  - current_state
  - behavior
  - voice
  - output_format
```

同一个 section 可以有多个变体文件，用 `@` 后缀区分（如 `identity@soft.j2`），Profile 中通过 `identity@soft` 引用。

### 渲染流程

```
BrainPromptRegistry.render(profile, context)
  ├── 加载 profile → 确定启用列表 + 变体选择
  ├── 按 metadata.yaml order 遍历
  │   ├── SectionParser 从 .j2 文件提取模板 + 元数据
  │   ├── 注入 section 声明的 @params（缺失则抛 MissingContextError）
  │   └── Jinja2 渲染
  └── 拼接 → 返回 system prompt 字符串
```

## API

### BrainPromptRegistry(template_dir, *, watch=False)

- **template_dir** (`str | Path`): `prompt/brain/` 目录路径
- **watch** (`bool`): 是否启动文件监听（Phase 2，当前未实现）

### render(profile="default", context) → str

按 profile 渲染 system prompt。

- **profile** (`str`): profile 名称（`"default"`、`"minimal"`）或 `"__session__"` 触发 A/B 分流
- **context** (`dict`): 所有 section 可能用到的参数字典
- **Raises**: `MissingContextError`（context 缺少必需参数）、`SectionRenderError`（渲染失败）

### reload() → None

重新扫描 `sections/` 和 `profiles/`，原子替换内部状态。热更新使用，失败时保持旧状态不变。

### get_versions() → dict[str, str]

返回 `{section_name: version}` 映射。

### get_profile_for_session(session_id) → str

基于 `hashlib.md5(session_id + exp_name)` 确定性分流。无 A/B 实验时返回 `"default"`。

## 数据模型

### PromptSection

```python
@dataclass(frozen=True)
class PromptSection:
    name: str              # @section 标识
    version: str           # @version 语义版本
    description: str       # @description 说明
    params: tuple[str, ...] # @params 参数列表
    template: str          # 去掉头部标记后的纯模板体
    source_file: str       # 来源文件路径
```

### ProfileConfig

```python
@dataclass
class ProfileConfig:
    name: str
    description: str
    enabled: list[str]     # ["identity", "identity@soft", "sensory", ...]

    @property
    def enabled_set(self) -> set[str]: ...
```

### SectionParser

```python
class SectionParser:
    def parse(self, filepath: str | Path) -> PromptSection: ...
    def scan_all(self, sections_dir: str | Path) -> dict[str, dict[str, PromptSection]]: ...
```

## 异常类型

| 异常 | 基类 | 触发场景 |
|------|------|---------|
| `SectionParseError` | `ValueError` | `@section` 标记缺失或格式错误 |
| `SectionNotFoundError` | `FileNotFoundError` | 引用的 section 文件不存在 |
| `MissingContextError` | `KeyError` | context 缺少 section 声明的 `@params` |
| `SectionRenderError` | `RuntimeError` | 单个 section Jinja2 渲染失败 |
| `ProfileValidationError` | `ValueError` | Profile 引用了不存在的 section |

## A/B 测试

在 `metadata.yaml` 中声明实验：

```yaml
ab_experiments:
  identity_soft:
    variant_profile: experimental_soft
    base_profile: default
    ratio: 0.1           # 10% 流量
```

渲染时传 `profile="__session__"` 并在 context 中提供 `session_id`，Registry 自动按 session hash 分流。Phase 1 只做分流，不做结果收集/分析。

## 设计原则

- **Fail-fast**: Section 缺失或格式错误在启动时报错，不静默降级
- **Fallback**: Registry 加载/渲染失败时，`PromptAssembler` 三层回退（Registry → Brain.j2 → 硬编码）
- **不可变**: `PromptSection` 为 frozen dataclass，渲染是纯读操作
- **原子替换**: `reload()` 构建新状态后一次性 swap，不锁

## 测试

```bash
# 单元测试（45 tests）
python -m pytest Module/brain_prompt/tests/ -v

# 黄金对比（迁移验证核心）
python -m pytest Module/brain_prompt/tests/integration/test_golden_diff.py -v
```
