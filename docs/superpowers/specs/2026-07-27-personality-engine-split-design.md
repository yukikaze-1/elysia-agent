# CharacterEngine 子模块拆分设计

> 状态: 待实现 | 日期: 2026-07-27

## 动机

`core/personality/engine.py` 当前 700 行，包含三个清晰可分离的子系统（情绪检测、awareness 判定、L3→L2 压缩 + C6 刷新），全部嵌在 `CharacterEngine` 大类中。拆分为子模块提出来，每个文件 100~220 行。

## 设计概览

**方案 B — 拆成独立类，CharacterEngine 做 facade**。外部调用方（agent、lifecycle、loop、debug/info）零改动。

```
core/personality/
├── __init__.py              # 不变: re-export CharacterEngine + CharacterReflector
├── engine.py                # CharacterEngine facade (~200 行)
├── reflector.py             # 不变
├── emotion.py               # NEW: EmotionDetector (~90 行)
├── awareness.py             # NEW: AwarenessJudge (~110 行)
├── compression.py           # NEW: CompressionManager (~220 行)
└── _utils.py                # NEW: _schedule() + _extract_tag() (~20 行)
```

## 子组件接口

### emotion.py — `EmotionDetector`

纯规则引擎，无 LLM 依赖。接管 engine.py L610-700 的所有情绪逻辑。

```python
class EmotionDetector:
    def __init__(self, *, state: CharacterState, output: OutputManager,
                 idle_expressions: list[str])

    def analyze_user(self, text: str) -> None
    def analyze_assistant(self, text: str) -> None
    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> None
    async def push_idle_expression(self) -> None
```

`MOOD_KEYWORDS` 常量、`_detect_mood()`、`_push_expression()`、`_push_idle()` 均作为此类的私有实现。

`analyze_user_stt` 签名保留 `emotion_tag` 参数（当前实现未使用）——去掉它会
迫使 `loop.py:626` 修改调用方式，破坏"外部零改动"约束。

### awareness.py — `AwarenessJudge`

LLM 语义判定 + 缓存门控。接管 engine.py L235-332。

```python
class AwarenessJudge:
    def __init__(self, *, llm: LLMAdapter, state: CharacterState,
                 character_name: str, user_name: str, pm: PromptManager)

    def is_allowed(self) -> bool                    # 缓存+冷却+次数 三重门控
    def maybe_judge(self) -> None                   # fire-and-forget 触发（同步返回）
    async def _judge(self, l3_text: str) -> None    # LLM 调用 + 解析 <approved>/<skip>
```

`AWARENESS_JUDGMENT_COOLDOWN`、`AWARENESS_MAX_SHOWS` 常量跟此类走。
`_schedule()` 和 `_extract_tag()` 从 `_utils.py` 导入。

### compression.py — `CompressionManager`

L3→L2 压缩 + C6 刷新。接管 engine.py L334-542。

```python
class CompressionManager:
    def __init__(self, *, llm: LLMAdapter, state: CharacterState,
                 character_name: str, l1_core: str, pm: PromptManager,
                 version_repo: CharacterVersionRepository | None,
                 character_config)              # 拿 user_name 做 C6 默认模板渲染

    async def check_and_compress(self) -> None  # 满足条件则触发（替代 _maybe_compress）
    async def flush(self) -> None               # shutdown 强制压缩（5s 超时）
    async def _compress(self, trigger) -> None  # 核心流程
    async def _refresh_c6(self) -> None         # L2 更新后刷新说话风格
```

`COMPRESSION_COUNT_THRESHOLD`、`COMPRESSION_TIME_THRESHOLD`、`COMPRESSION_MIN_ENTRIES` 常量跟此类走。
`_extract_tag()` 从 `_utils.py` 导入。

### engine.py — `CharacterEngine`（精简后 ~200 行）

保留内容：
- `__init__`: 创建 4 个子组件 + CharacterReflector，注入依赖
- `render_c1/c9/c6`: 数据提供方法（不变）
- `consume_trigger`: 触发分发 → `awareness.maybe_judge()` + `compression.check_and_compress()`
- `_set_state_l3`, `_append_state_l3`, `_append_history`: 内部状态管理（太简单不值得抽）
- `sync_reflector_to_state`, `buffer_message`, `flush_reflector`: Reflector 代理（不变）
- 公开 API 方法委托给子组件

`last_awareness_time` 属性 engine 仍保留（供 `lifecycle.py` 恢复用），同步到 `AwarenessJudge`。

`_last_awareness_time` 实现为 engine 上的 property，getter/setter 代理到
`self._awareness._last_awareness_time`。这是为了保持向后兼容 — 现有代码
（含 `lifecycle.py:177-178` 和 `test_engine.py` 中 6 处直接赋值）无需修改。

### 外部调用方：零改动

所有调用 `CharacterEngine` 公开方法的地方签名不变：
- `agent.py` — 构造 `CharacterEngine(...)`、透传 `_character_reflector`
- `lifecycle.py` — `flush_reflector()`、`flush_compression()`、`_last_awareness_time` 恢复
- `loop.py` — `analyze_user()`、`analyze_assistant()`、`analyze_user_stt()`、`buffer_message()`
- `debug/info.py` — 读 `engine.state`、`engine._reflector`
- `prompt_assembler.py` — 通过 section 系统间接使用 `render_c1/c9/c6`

## 内部影响

| 内容 | 去向 |
|------|------|
| `_schedule()` | `_utils.py` |
| `_extract_tag()` | `_utils.py` |
| `_set_state_l3()`, `_append_state_l3()`, `_append_history()` | 保留 `engine.py` |
| `MOOD_KEYWORDS` | `emotion.py` |
| `AWARENESS_*` 常量 | `awareness.py` |
| `COMPRESSION_*` 常量 | `compression.py` |
| `_last_awareness_time` | `engine.py` 保留（同步给 awareness judge） |

## 迁移顺序

1. **`_utils.py`** — 抽取 `_schedule()`, `_extract_tag()` — 无行为变化，无测试
2. **`emotion.py`** — 最独立、无 LLM 依赖 — `make test-quick` 验证
3. **`awareness.py` + `compression.py`** — 并行抽取 — `pytest core/personality/tests/ -v` 验证

每步独立 commit，出问题能干净回滚。
