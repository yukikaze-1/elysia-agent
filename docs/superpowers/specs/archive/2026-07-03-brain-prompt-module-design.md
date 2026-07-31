# Brain Prompt 模块化重构设计

**日期**: 2026-07-03
**状态**: 已完成 (2026-07-03) — 实现见 `docs/superpowers/plans/2026-07-03-brain-prompt-module.md`
**范围**: Phase 1 — 系统 prompt（Brain.j2）模块化；后续扩展到所有模板

---

## 动机

当前 [Brain.j2](../../../prompt/Brain.j2) (~244行) 是一个 Jinja2 单体宏文件，包含身份/性格、感官数据、记忆、请求分类、工具规则、输出格式、说话风格等多种职责的内容。所有内容混在一个文件里：

- **难以独立调整**：改输出格式也得 touch 整个 Brain.j2
- **无法独立版本化**：无法追踪"identity 改过几次，output_format 没动过"
- **测试困难**：只能测整体渲染，不能测单个 section
- **无法 A/B**：想做性格变体对比实验，只能手动改模板
- **reload 粒度过粗**：改一行就要重新加载整个文件

## 目标

将 Brain.j2 从单体模板重构为**模块化 section 管理引擎**，每个 section 独立文件、独立版本、独立加载。Phase 1 聚焦系统 prompt，架构为后续所有模板（Amygdala、ActiveSpeak、MicroReflector 等）迁入预留接口。

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    PromptAssembler                           │
│  准备 context dict {name, user_name, sensory_ctx, ...}       │
│  （不再直接调 PromptManager，不感知 section 结构）             │
└──────────────────────┬──────────────────────────────────────┘
                       │ context dict
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                 BrainPromptRegistry                          │
│                                                              │
│  render(profile, context)                                    │
│    ├─ 加载 profile → 确定启用列表 + 变体选择                  │
│    ├─ 按 metadata.yaml order 遍历                            │
│    │   ├─ SectionParser 从 .j2 文件提取模板 + 元数据           │
│    │   ├─ 注入 section 声明的 @params                        │
│    │   └─ Jinja2 渲染                                        │
│    └─ 拼接 → 返回 system prompt 字符串                         │
│                                                              │
│  reload() — 文件监听 + 手动 API + SIGHUP                      │
│  get_profile_for_session(id) — A/B 分流                       │
│  get_versions() — 版本查询                                    │
└─────────────────────────────────────────────────────────────┘
```

### 模块位置

```
core/prompt/                        # 新增：brain prompt 引擎
├── __init__.py                     # 公开 API
├── section.py                      # PromptSection dataclass
├── parser.py                       # SectionParser — 解析 .j2 头标记
├── registry.py                     # BrainPromptRegistry — 加载、渲染、A/B、热更新
└── profile.py                      # ProfileConfig — YAML profile 加载

prompt/brain/                       # 模板内容（设计师/调参者编辑这里）
├── __init__.py                     # 留空
├── sections/
│   ├── identity.j2
│   ├── self_perception.j2
│   ├── sensory.j2
│   ├── memory.j2
│   ├── current_state.j2
│   ├── behavior.j2
│   ├── voice.j2
│   └── output_format.j2
├── profiles/
│   ├── default.yaml
│   └── minimal.yaml
└── metadata.yaml                   # 版本历史、section 顺序、依赖关系
```

`Module/prompt/` 保持不动（通用 Jinja2 工具）。旧的 `prompt/Brain.j2` 在验证通过后删除。

---

## Section 设计

### Section 拆分

从 Brain.j2 按职责和变化频率拆分为 8 个 section：

| # | Section | 当前内容 | 类型 | 变化频率 | 声明的参数 |
|---|---------|---------|------|---------|-----------|
| 1 | `identity` | 角色身份、性格描述、与用户的关系 | 静态人设 | 很少 | `name`, `user_name`, `user_preferences` |
| 2 | `self_perception` | 角色进化感知（CharacterReflector 产出） | 半动态 | 偶尔 | `self_perception` |
| 3 | `sensory` | 时间、季节、STT 情绪等感官输入 | 动态数据 | 每次请求 | `sensory_ctx` |
| 4 | `memory` | 召回的长期记忆条目 | 动态数据 | 每次请求 | `memory_ctx` |
| 5 | `current_state` | 当前心情 | 动态数据 | 每次请求 | `mood` |
| 6 | `behavior` | 请求分类（A/B）、工具使用规则 | 指令 | 偶尔调整 | `user_name`, `tool_list` |
| 7 | `voice` | 回复长度/节奏/语气/克制/心情影响 | 风格 | 很少 | `user_name` |
| 8 | `output_format` | `<inner_voice>` 双轨协议 + 代码块格式 | 协议 | 几乎不变 | 无 |

> **注1**: 当前 Brain.j2:169 硬编码了 "妖梦"，迁移后替换为 `{{ user_name }}`。`tool_list` 由 `PromptAssembler` 从 `ToolManager` 获取后传入 context。
> **注2**: 当前 voice 段仅使用 `{{ user_name }}`（Brain.j2:206），不引用 `name`。迁移时按实际使用声明参数。

**类型分类**：
- **静态人设/风格/协议**：换角色或大版本调优才改
- **半动态**：由后台 Reflector 任务周期产出
- **动态数据**：PromptAssembler 每次请求注入
- **指令**：行为策略调整时改

### 子宏内联

Brain.j2 中 `BrainSystemPrompt` 内部调用了三个子宏：

| 子宏 | 位置 | 迁移方式 |
|------|------|---------|
| `L0SensoryBlockPrompt` | Brain.j2:23 | **内联**到 `sensory.j2` — 子宏的模板体直接成为 section 文件内容 |
| `L2MemoryBlockPrompt` | Brain.j2:56 | **内联**到 `memory.j2` |
| `CurrentStatePrompt` | Brain.j2:93 | **内联**到 `current_state.j2` |

内联后 section 文件是独立的 Jinja2 片段——不再依赖宏调用。`BrainSystemPrompt` 中的 `<sensory_input>`、`<memory_bank>`、`<current_state>` 包装标签也一并迁入对应 section 文件，由外层（Registry）通过 `"\n\n".join()` 拼接时自然形成分隔。

### Section 文件格式

每个 `.j2` 文件用 Jinja2 注释写头部元数据标记：

```jinja2
{#
@section identity
@version 1.2.0
@description 角色身份、性格描述、与用户的关系
@params name, user_name, user_preferences
#}
你是 {{ name }}。你不是一个"性格温柔、偶尔毒舌的 AI"——你是这样的一个人：

温柔是你的底色——...
...
关于 {{ user_name }}：
- 你们很熟了...
{% if user_preferences %}你注意到的：{{ user_preferences }}{% endif %}
```

**标记说明**：
- `@section` — 唯一标识名（必需）
- `@version` — 语义版本（必需）
- `@description` — 人类可读说明（必需）
- `@params` — 该 section 接受的模板参数，逗号分隔（必需）

解析后的 `PromptSection` dataclass：

```python
# 注：实际实现用 frozen=True + tuple params（不可变），此处仅示意字段
@dataclass(frozen=True)
class PromptSection:
    name: str
    version: str
    description: str
    params: tuple[str, ...]
    template: str          # 去掉头部标记后的纯模板体
    source_file: str       # e.g. "sections/identity.j2"
```

### metadata.yaml

```yaml
version: "1.0.0"
updated: "2026-07-03"
changelog:
  - date: "2026-07-03"
    summary: "从 Brain.j2 单体拆分为 8 个 section"
    sections: ["*"]

# Section 渲染顺序
order:
  - identity
  - self_perception
  - sensory
  - memory
  - current_state
  - behavior
  - voice
  - output_format

# Section 间依赖（Phase 1 仅作文档参考，render() 不强制执行。
# 渲染时各 section 独立处理，不感知其他 section 的渲染结果。
# 后续 Phase 可在此基础之上实现拓扑排序、自动注入等）
dependencies:
  identity: []
  self_perception: []
  sensory: []
  memory: []
  current_state: []
  behavior: [identity]       # behavior 引用 {{ name }} → 建议 identity 先渲染
  voice: [identity]          # voice 引用 {{ user_name }} → 建议 identity 先渲染
  output_format: []
```

---

## Profile 系统

Profile 定义"用哪些 section、用哪个变体"。每个 profile 是一个 YAML 文件。

### default.yaml

```yaml
name: default
description: 完整系统 prompt，用于正常对话
enabled:
  - identity
  - self_perception
  - sensory
  - memory
  - current_state
  - behavior
  - voice
  - output_format
```

### minimal.yaml

```yaml
name: minimal
description: 极简模式，仅感官 + 行为指令 + 输出格式。用于调试/低 token 场景
enabled:
  - sensory
  - behavior
  - output_format
```

### 变体选择

同一个 section 可以有多个变体文件，用 `@` 后缀区分：

```
sections/
├── identity.j2              # 主版本（默认）
├── identity@direct.j2       # 变体：更直接的性格描述
├── identity@soft.j2         # 变体：更温柔的语气
```

Profile 中通过 `section@variant` 引用变体：

```yaml
name: experimental_soft
enabled:
  - identity@soft
  - self_perception
  - sensory
  - memory
  - current_state
  - behavior
  - voice
  - output_format
```

---

## 核心类设计

### PromptSection (`core/prompt/section.py`)

纯数据对象，持有解析后的 section 信息。

```python
@dataclass(frozen=True)  # 不可变 — 渲染是纯读
class PromptSection:
    name: str
    version: str
    description: str
    params: tuple[str, ...]   # tuple 因为 frozen
    template: str
    source_file: str
```

### SectionParser (`core/prompt/parser.py`)

负责从 `.j2` 文件中提取头部元数据和模板体。

```python
class SectionParser:
    """解析 section 文件的 @section 头标记。"""

    def parse(self, filepath: str | Path) -> PromptSection:
        """解析单个 section 文件，返回 PromptSection。"""

    def scan_all(self, sections_dir: str | Path) -> dict[str, dict[str, PromptSection]]:
        """扫描目录下所有 .j2 文件。
        
        返回: {section_name: {variant_name_or_default: PromptSection}}
           e.g. {"identity": {"": PromptSection, "soft": PromptSection}}
        """
```

**解析规则**：
- `{#` 开始、`#}` 结束的注释块 = 头部元数据区
- 头部区中提取 `@section`, `@version`, `@description`, `@params`
- 头部区之后的所有内容 = 模板体
- 缺少必需标记 → 抛出 `SectionParseError(filepath, missing_field)`

### ProfileConfig (`core/prompt/profile.py`)

```python
@dataclass
class ProfileConfig:
    name: str
    description: str
    enabled: list[str]  # ["identity", "identity@soft", "sensory", ...]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ProfileConfig":
        """从 YAML 文件加载。"""
```

### BrainPromptRegistry (`core/prompt/registry.py`)

总控类，PromptAssembler 的唯一交互点。

```python
class BrainPromptRegistry:
    """Brain system prompt 的 section 注册、渲染、热更新中心。"""

    def __init__(self, template_dir: str | Path, *, watch: bool = False):
        """加载所有 section、profile、metadata。
        
        Args:
            template_dir: prompt/brain/ 目录路径
            watch: 是否启动文件监听（开发环境）
        """

    def render(self, profile: str = "default", context: dict[str, Any]) -> str:
        """按 profile 渲染 system prompt。
        
        Args:
            profile: profile 名称（如 "default", "minimal"）或 "__session__" 触发 AB 分流
            context: 所有 section 可能用到的参数字典
        
        Returns:
            完整的 system prompt 字符串
        
        Raises:
            MissingContextError: context 缺少 section 声明的必需参数
            SectionRenderError: 某个 section 渲染失败
        """

    def reload(self) -> None:
        """重新扫描 sections/ 和 profiles/，原子替换。"""

    def get_versions(self) -> dict[str, str]:
        """返回 {section_name: version} 映射。"""

    def get_profile_for_session(self, session_id: str) -> str:
        """基于 session hash 选择 AB profile。"""
```

**`render()` 核心逻辑**：

```python
def render(self, profile: str = "default", context: dict) -> str:
    if profile == "__session__":
        profile = self.get_profile_for_session(context.get("session_id", ""))

    prof = self._profiles[profile]
    parts = []

    for name in self._order:
        section_name, variant = _parse_ref(name)

        if section_name not in prof.enabled_set:
            continue

        section = self._resolve_section(section_name, variant)

        # 只传 section 声明的参数；缺失的参数抛出 MissingContextError
        section_ctx = {}
        for param in section.params:
            if param not in context:
                raise MissingContextError(
                    section=section.name,
                    param=param,
                    msg=f"Section '{section.name}' requires '{param}' but context missing it"
                )
            section_ctx[param] = context[param]

        rendered = self._jinja_env.from_string(section.template).render(**section_ctx)
        parts.append(rendered)

    return "\n\n".join(parts)
```

---

## A/B 测试

在 `metadata.yaml` 中声明实验：

```yaml
ab_experiments:
  identity_soft:
    variant_profile: experimental_soft
    base_profile: default
    ratio: 0.1           # 10% 流量
```

分配算法（基于 session_id hash，确定性分流）：

```python
def get_profile_for_session(self, session_id: str) -> str:
    for exp_name, cfg in self._ab_experiments.items():
        bucket = hash(session_id + exp_name) % 100
        if bucket < cfg["ratio"] * 100:
            return cfg["variant_profile"]
    return "default"
```

Phase 1 只做分流，不做结果收集/分析（后续迭代）。

---

## 热更新

三种触发路径：

| 路径 | 场景 | 实现 |
|------|------|------|
| 文件监听 | 开发环境，改 section 即时生效 | `watchdog.Observer` 监听 `sections/` + `profiles/`，变更时自动 `reload()` |
| 手动 API | 调试页面触发 | `POST /debug/api/reload-prompt` → `registry.reload()` |
| SIGHUP | 生产环境 | Server 收到 SIGHUP 时调 `registry.reload()` |

**线程安全**：`render()` 是热点路径（每次 LLM 调用都走），`reload()` 是冷路径。使用原子引用替换——构建新 dict 后一次性 swap `self._sections`。Python 引用赋值是原子的，无需锁。

```python
def reload(self):
    new_sections = self._parser.scan_all(self._section_dir)
    new_profiles = self._load_all_profiles()
    self._sections = new_sections    # 原子
    self._profiles = new_profiles    # 原子
```

---

## PromptAssembler 适配

`PromptAssembler` 的职责从"渲染 prompt"缩减为"准备数据"：

```python
class PromptAssembler:
    def __init__(self, character, session, long_term):
        # ...existing init...
        try:
            self._brain = BrainPromptRegistry("prompt/brain")
        except Exception:
            logger.warning("BrainPromptRegistry 加载失败，fallback")
            self._brain = None

    async def build_system_prompt(self, ...) -> str:
        # 准备 context dict（跟现在一样构建 sensory、memory 等数据）
        # 工具列表（由 ToolManager 生成，注入 behavior section）
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
        
        if self._brain is not None:
            try:
                return self._brain.render(profile="default", context=context)
            except Exception:
                logger.warning("BrainPromptRegistry 渲染失败，fallback", exc_info=True)
        
        return await self._render_fallback(user_name, user_message, now, mood)
```

`_render_fallback()` 保持不变，作为双重保险。

---

## 错误处理

| 场景 | 策略 | 错误类型 |
|------|------|---------|
| Section 文件缺失 | 启动时报错，列出缺失文件，阻止启动（fail-fast） | `SectionNotFoundError` |
| `@section` 标记格式错误 | 启动时报错，指明文件+行号+具体问题 | `SectionParseError` |
| `@params` 声明了但 context 未提供 | render() 时抛出，附带 section 名和缺失参数名 | `MissingContextError` |
| Profile 引用了不存在的 section | 启动时校验，报错指明 profile 和 section 名 | `ProfileValidationError` |
| 单个 section 渲染失败 | 抛出异常，包含 section 名和原始异常 | `SectionRenderError` |
| Registry 整体加载失败 | PromptAssembler try/except → fallback | — |
| 热更新中途失败 | 保持旧 sections 不变，记错误日志 | — |

原则：**尽早暴露、明确信息、始终有 fallback**。

---

## 测试策略

### 测试位置

```
core/prompt/tests/
├── unit/
│   ├── test_parser.py          # SectionParser — 正常/异常标记解析
│   ├── test_section.py         # PromptSection — 不可变性、字段完整性
│   ├── test_profile.py         # ProfileConfig — YAML 加载、变体解析
│   └── test_registry.py        # BrainPromptRegistry — render/reload/AB 分流
└── integration/
    ├── test_render_full.py     # 完整 8 section 渲染，验证输出完整性
    ├── test_golden_diff.py     # 黄金对比：新旧 system prompt 输出一致
    └── test_hot_reload.py      # 文件变更 → reload → 输出变化
```

### 黄金对比测试

迁移验证的核心：用同样的 context 输入，分别跑旧 `Brain.j2` 的 `BrainSystemPrompt` macro 和新 `BrainPromptRegistry.render()`，断言输出逐字符一致。这条测试通过 = 迁移成功，旧 Brain.j2 可安全删除。

---

## 迁移步骤

### Phase 1a: 搭架子（不影响现有功能）

1. 创建 `core/prompt/` 模块（`section.py`, `parser.py`, `registry.py`, `profile.py`）
2. 创建 `prompt/brain/` 目录结构
3. 从 `Brain.j2` 提取内容，写入 8 个 section 文件
4. 编写 `metadata.yaml` + `default.yaml` + `minimal.yaml`
5. 编写单元测试 + 黄金对比测试
6. 在 `PromptAssembler` 中通过 `try/except` 实现优雅降级：优先用 Registry，任何异常回退到旧的 `Brain.j2` 渲染路径（无需额外配置开关，异常即回退）
7. 确认所有现有测试通过

### Phase 1b: 切换（2-3 天验证期）

8. 在开发环境跑，验证与旧版行为一致
9. 关闭 feature flag，正式使用 Registry
10. `Brain.j2` 保留为只读备份（不加载）

### Phase 1c: 清理

11. 删除 `Brain.j2`
12. 清理 `PromptAssembler` 中不再需要的 `PromptManager` 直接调用
13. 更新 `core/input/server.py` 的 prompt viewer API（从读 Brain.j2 改为读 section 文件）

---

## 不做的事项（YAGNI）

- **Section 回滚**：不需要。真需要回滚时切 profile。
- **A/B 结果分析**：Phase 1 只做分流，不收集指标。
- **跨模板 section 复用**：Phase 1 只有 Brain。后续扩展时再抽象通用接口。
- **Web UI 编辑器**：Section 文件直接编辑即可，Jinja2 很直观。
- **数据库存储 prompt 版本**：文件系统即真实来源。
- **动态 section 排序**：order 由 `metadata.yaml` 固定，不运行时调整。

---

## 后续扩展路径

Phase 1 架构设计时预留了后续扩展点：

- **新模板迁入**：Amygdala、ActiveSpeak 等可以各建一个目录（如 `prompt/amygdala/`），复用 `SectionParser` + `ProfileConfig`，只需各自实现自己的 Registry
- **通用基类**：当有 2+ 个 Registry 时，提取 `BasePromptRegistry` ABC
- **跨模板 section 复用**：如 `output_format` section 可能被 Brain 和 TaskSystem 共用，届时引入 `prompt/common/` 或 symbolic link
