# Split CognitiveLoop Module Design

> **Target:** 将 `core/cognitive_loop.py`（1029 行）+ `core/tts_stream.py`（308 行）合并拆分为 `core/cognitive/` 包。

## 目标结构

```
core/cognitive/
├── __init__.py     # from .loop import CognitiveLoop
├── loop.py         # 核心管道: __init__ + set_* + process + _think_loop + _after_response + FSM
├── tts.py          # TTS 全模块: TTSStreamManager + emoji utils + deprecated wrappers
└── tools.py        # 工具执行: _execute_tools_directly + _handle_submit_task + _submit_task_definition + _default_user_profile
```

## 各文件职责

### `loop.py` (~800 行)

- `CognitiveLoop.__init__` — 属性初始化
- `CognitiveLoop.cancel_current` — 中断取消
- 10 个 `set_*` 注入方法（`set_tts`, `set_tool_context`, `set_token_tracker`, `set_prompt_recorder`, `set_response_recorder`, `set_task_system`, `set_delegation_preparer`, `set_on_background_task`, `set_user_profile`, `set_memory_reflector`）
- `CognitiveLoop.process()` — 入口
- `CognitiveLoop._think_loop()` — ReAct 循环（含内嵌 `_handle_sentence`）
- `CognitiveLoop._after_response()` — 后处理（consolidator + reflector + compression）
- `CognitiveLoop._transition_idle()` — FSM 转换
- `CognitiveLoop._handle_interrupt()` — 中断处理
- `CognitiveLoop._build_wake_up_message()` — 静态方法

```python
from .tts import CognitiveTTS, TTSStreamManager, is_emoji_only
from .tools import CognitiveTools


class CognitiveLoop(CognitiveTTS, CognitiveTools):
    def __init__(self, ...):
        ...
```

### `tts.py` (~370 行)

**从 `core/tts_stream.py` 合并：**
- `_EMOJI_RE`, `is_emoji_only()` — emoji 检测工具
- `TTSStreamManager` — 流式播放管理器（句级 TTS + emoji 排队 + 热切换）

**从 `cognitive_loop.py` 提取：**
- `CognitiveTTS` mixin:
  - `_start_tts_stream(sentence)` — deprecated → `self._tts_mgr.stream_sentence()`
  - `_synthesize_sentence(sentence)` — deprecated → `self._tts.synthesize()`

### `tools.py` (~190 行) — `CognitiveTools` mixin

- `_default_user_profile()` — module-level 函数（`_handle_submit_task` 的唯一调用方）
- `_execute_tools_directly()` + 内嵌 `_execute_one()`
- `_submit_task_definition()`
- `_handle_submit_task()`

> **循环导入防护**: `_default_user_profile` 放在 `tools.py` 而非 `loop.py`。若放在 `loop.py`，`tools.py` → `loop.py` 会闭合 `loop.py` → `tools.py` → `loop.py` 的循环导入。`_default_user_profile` 只有一个调用方（`_handle_submit_task`），放在 `tools.py` 零依赖。

## 删除文件

- `core/cognitive_loop.py` — 拆分为 loop.py
- `core/tts_stream.py` — 合并到 tts.py

## 外部引用更新

| 文件 | 当前 | 改为 |
|------|------|------|
| `core/agent/agent.py` | `from ..cognitive_loop import CognitiveLoop` | `from ..cognitive import CognitiveLoop` |
| `core/agent/lifecycle.py` | `from ..cognitive_loop import CognitiveLoop` | `from ..cognitive import CognitiveLoop` |
| `core/tests/test_phase3.py` | `from core.cognitive_loop import CognitiveLoop` | `from core.cognitive import CognitiveLoop` |
| `core/tests/test_task_scheduler.py` | `from core.cognitive_loop import CognitiveLoop` | `from core.cognitive import CognitiveLoop` |

## 边界约束

1. **外部 import 路径变更**: `from core.cognitive_loop import CognitiveLoop` → `from core.cognitive import CognitiveLoop`
2. **Mixin 只放方法**: `CognitiveTTS` 和 `CognitiveTools` 不设 `__init__`
3. **不改变逻辑**: 纯文件拆分+合并，不重构内部实现
4. **`tts_stream.py` 合并**: 原文件只有 cognitive_loop 一个消费者，合并到 `cognitive/tts.py` 消除散落文件
5. **test_phase3.py 直接访问 `loop._tts` / `loop._start_tts_stream`**: MRO 保证方法可用，不需要改测试逻辑
6. **`_default_user_profile` 放在 `tools.py`**: 防止循环导入，它是 `_handle_submit_task` 的唯一调用方
