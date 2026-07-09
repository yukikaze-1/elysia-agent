# TTS 预处理解耦 + Prompt TTS 感知 设计

> 日期：2026-07-09 | 状态：待审批

## 1. 背景

TTS 模块当前有两个问题：

1. **预处理硬编码**：`core/tts_stream.py:91` 硬编码 `strip_all_markup()`，导致 MiniMax 原生支持的 `(laughs)`、`<#x#>` 标签在到达 backend 之前被删除
2. **Prompt 无 TTS 感知**：LLM 不知道目标 TTS 引擎支持什么标记，无法正确输出标记文本

## 2. 设计

### 2.1 协议层：`TTSBackend` 新增两个方法

```python
class TTSBackend(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """返回 provider 标识：'gpt-sovits' / 'minimax' / 'custom'。"""
        ...

    def get_preprocess(self):
        """返回本后端使用的预处理函数，默认 None（不预处理）。"""
        return None
```

各 backend 实现：

| Backend | `provider_name` | `get_preprocess()` |
|---------|----------------|-------------------|
| GPTSoVitsBackend | `"gpt-sovits"` | `strip_all_markup` |
| MiniMaxTTSBackend | `"minimax"` | `strip_for_minimax` |
| OpenAICompatTTSBackend | `"custom"` | `strip_all_markup` |

OpenAI 后端当前在 `synthesize()`/`synthesize_stream()` 中 inline 调用 `strip_all_markup()`，本次一并重构为 `self._preprocess` 模式，与其他 backend 一致。

### 2.2 TTSStreamManager：去硬编码

**文件：`core/tts_stream.py`**

- 删除 `from Module.tts.text_preprocess import strip_all_markup`
- 构造函数中从 backend 获取预处理函数：
  ```python
  self._preprocess = lambda text: text  # 默认
  if tts is not None:
      fn = tts.get_preprocess()
      if fn is not None:
          self._preprocess = fn
  ```
- `stream_sentence()` 中 `strip_all_markup(sentence)` → `self._preprocess(sentence)`
- `set_backend()` 热切换时同步更新 `self._preprocess`

### 2.3 Prompt 层：TTS 感知

**数据流：**

```
CognitiveLoop._think_loop()
  → build_messages(tts_provider=self._tts.provider_name)
    → PromptAssembler.build_system_prompt(tts_provider=...)
      → SystemPromptManager.set_context(tts_provider=...)
        → OutputFormatSection.set_context(tts_provider=...)
          → f2_output_format.j2 模板 {% if tts_provider == ... %}
```

**修改文件：**

1. `core/cognitive_loop.py` — `_think_loop()` 传入 `self._tts.provider_name`
2. `core/prompt_assembler.py` — `build_system_prompt()` / `build_messages()` 新增 `tts_provider: str = ""` 参数，透传至 `set_context()`
3. `Module/prompt/sections/output_format.py` — `set_context()` 接收 `tts_provider`，`render()` 传给模板
4. `Module/prompt/templates/f2_output_format.j2` — 新增条件块：

```jinja2
{% if tts_provider == "minimax" %}
## 语音合成标记
你的对话输出将用于语音合成。在对话文本中可以使用：
- 语气词：(laughs) (sighs) (crying) (gasps) (breath) (humming) 等
- 停顿控制：<#0.5#> 短停顿、<#1#> 中停顿、<#2#> 长停顿
- 发音标注：正确发音放在括号内，如 处理(chu3li3)
注意：仅在对话中使用，<inner_voice> 中无需添加。
{% elif tts_provider == "gpt-sovits" or tts_provider == "custom" %}
## 语音合成标记
你的对话输出将用于语音合成，请输出纯文本，不要使用任何特殊标记符号。
{% endif %}
```

### 2.4 TTS 关闭时的行为

TTS 未连接时 `CognitiveLoop._tts` 为 `None`，`tts_provider` 传空字符串 → 模板不命中任何条件 → 不输出标记指令。

## 3. 不修改的部分

- `Module/tts/text_preprocess.py` — 三个 strip 函数不变
- `Module/tts/registry.py` — TTSFactory 不变
- `config.yaml` / `AppConfig` — 不变
- `CharacterConfig` — 不变

## 4. 受影响文件清单

| 文件 | 改动 |
|------|------|
| `Module/tts/protocol.py` | 新增 `provider_name`（抽象属性）+ `get_preprocess()`（方法） |
| `Module/tts/gpt_sovits.py` | 实现 `provider_name` + `get_preprocess()` |
| `Module/tts/minimax_tts.py` | 实现 `provider_name` + `get_preprocess()` |
| `Module/tts/openai_tts.py` | 重构为 `self._preprocess` 模式 + 实现两个新方法 |
| `core/tts_stream.py` | 去硬编码 `strip_all_markup`，改用 `self._preprocess` |
| `core/cognitive_loop.py` | 传入 `tts_provider` 到 prompt |
| `core/prompt_assembler.py` | `build_system_prompt()` / `build_messages()` 新增参数 |
| `Module/prompt/sections/output_format.py` | `set_context()` 接收 `tts_provider` |
| `Module/prompt/templates/f2_output_format.j2` | 新增条件标记指令块 |

## 5. 测试

- 协议测试：验证 `provider_name` 为抽象属性
- Backend 测试：三个 backend 的 `provider_name` 和 `get_preprocess()` 返回值正确
- TTSStreamManager 测试：`set_backend()` 同步更新预处理函数；不同 backend 得到不同 strip 函数
- Prompt 组装测试：`tts_provider` 参数正确透传至模板渲染输出
- 模板渲染测试：`tts_provider="minimax"` 输出包含语气词说明

## 6. 验证

1. `python -m pytest Module/tts/ -v` 全部通过
2. `python -m pytest Module/prompt/ -v` 全部通过
3. `python -m pytest core/ -v -k "tts or prompt"` 相关测试通过
4. 启动 Agent，MiniMax 模式下 LLM 输出应包含 `(laughs)` 等标记，且不被 TTSStreamManager 删除
