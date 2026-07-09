# TTS 预处理解耦 + Prompt TTS 感知 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 解除 `core/tts_stream.py` 的硬编码 `strip_all_markup`，让 TTS backend 提供预处理函数；同时让 Prompt 系统感知当前 TTS backend，为 LLM 生成正确的标记格式指令。

**Architecture:** 三层联动 — 协议层新增 `provider_name` + `get_preprocess()`；TTSStreamManager 从 backend 获取预处理函数替代硬编码；Prompt 层将 `tts_provider` 从 CognitiveLoop 透传至模板。

**Tech Stack:** Python 3.13, asyncio, Jinja2, pytest

## Global Constraints

- Python 3.13+
- `provider_name` 返回 `"gpt-sovits"` / `"minimax"` / `"custom"`（与 `_BACKENDS` 注册表 key 一致）
- `get_preprocess()` 默认返回 `None`（非抽象，向后兼容）
- `tts_provider` 空字符串表示 TTS 未连接 → 模板不输出标记指令
- `tts_provider = self._tts.provider_name if self._tts else ""`
- 所有现有测试不能出现回归
- 每次 task 结束时 commit

---

### Task 1: 协议层 — `TTSBackend` 新增 `provider_name` + `get_preprocess()`

**Files:**
- Modify: `Module/tts/protocol.py`

**Produces:** `TTSBackend.provider_name`（抽象 property）+ `TTSBackend.get_preprocess()`（非抽象方法，默认 `None`）

- [ ] **Step 1: 修改 protocol.py**

在 `TTSBackend` 类中，`close()` 方法之前添加：

```python
@property
@abstractmethod
def provider_name(self) -> str:
    """返回 provider 标识：'gpt-sovits' / 'minimax' / 'custom'。"""
    ...

def get_preprocess(self):
    """返回本后端使用的预处理函数，默认 None（不预处理）。"""
    return None
```

- [ ] **Step 2: 验证导入**

Run: `python -c "from Module.tts.protocol import TTSBackend; print('OK')"`
Expected: OK

- [ ] **Step 3: 运行现有测试确认无回归**

Run: `python -m pytest Module/tts/test_tts.py -v`
Expected: 某些 backend 测试会因为未实现抽象属性 `provider_name` 而失败（预期行为）

- [ ] **Step 4: Commit**

```bash
git add Module/tts/protocol.py
git commit -m "feat: add provider_name + get_preprocess() to TTSBackend protocol"
```

---

### Task 2: Backend 实现 — 三个 backend 实现新协议方法

**Files:**
- Modify: `Module/tts/gpt_sovits.py`
- Modify: `Module/tts/minimax_tts.py`
- Modify: `Module/tts/openai_tts.py`
- Modify: `Module/tts/test_tts.py`（测试）

**Produces:** 三个 backend 实现 `provider_name` + `get_preprocess()`；OpenAI backend 重构为 `self._preprocess` 模式

- [ ] **Step 1: 在 test_tts.py 添加协议测试**

在 `TestTTSBackendProtocol` 类末尾添加：

```python
def test_provider_name_is_abstract(self):
    """provider_name 是抽象属性，未实现时不能实例化。"""
    with pytest.raises(TypeError):

        class NoProviderName(TTSBackend):
            async def synthesize(self, text, *, character, emotion=None) -> bytes:
                return b""

            async def synthesize_stream(self, text, *, character, emotion=None):
                yield b""

            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}

            async def close(self) -> None:
                pass

            def get_preprocess(self):
                return None

        NoProviderName()  # type: ignore[abstract]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest Module/tts/test_tts.py::TestTTSBackendProtocol::test_provider_name_is_abstract -v`
Expected: FAIL（新测试）

- [ ] **Step 3: GPT-SoVits backend** — 在 `GPTSoVitsBackend` 类中，`close()` 之前添加：

```python
@property
def provider_name(self) -> str:
    return "gpt-sovits"

def get_preprocess(self):
    return self._preprocess
```

- [ ] **Step 4: MiniMax backend** — 在 `MiniMaxTTSBackend` 类中，`close()` 之前添加：

```python
@property
def provider_name(self) -> str:
    return "minimax"

def get_preprocess(self):
    return self._preprocess
```

- [ ] **Step 5: OpenAI backend — 重构为 `self._preprocess` 模式**

在 `OpenAICompatTTSBackend.__init__()` 中，`self._client = ...` 之前添加：

```python
self._preprocess = strip_all_markup
```

将 `synthesize()` 中第 57 行和 `synthesize_stream()` 中第 67 行：

```python
text = strip_all_markup(text)
```

替换为：

```python
text = self._preprocess(text)
```

在类中，`close()` 之前添加：

```python
@property
def provider_name(self) -> str:
    return "custom"

def get_preprocess(self):
    return self._preprocess
```

- [ ] **Step 6: 添加 backend 测试**

在 `test_tts.py` 末尾添加：

```python
# ============================================================
# TTSBackend provider_name + get_preprocess
# ============================================================

class TestTTSBackendProviderName:
    def test_gpt_sovits_provider_name(self, backend):
        assert backend.provider_name == "gpt-sovits"

    def test_gpt_sovits_get_preprocess(self, backend):
        from .text_preprocess import strip_all_markup
        assert backend.get_preprocess() is strip_all_markup

    def test_minimax_provider_name(self):
        from .minimax_tts import MiniMaxTTSBackend
        backend = MiniMaxTTSBackend(api_key="sk-test", voice_id="test-voice")
        assert backend.provider_name == "minimax"

    def test_minimax_get_preprocess(self):
        from .minimax_tts import MiniMaxTTSBackend
        from .text_preprocess import strip_for_minimax
        backend = MiniMaxTTSBackend(api_key="sk-test", voice_id="test-voice")
        assert backend.get_preprocess() is strip_for_minimax

    def test_openai_provider_name(self):
        from .openai_tts import OpenAICompatTTSBackend
        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        assert backend.provider_name == "custom"

    def test_openai_get_preprocess(self):
        from .openai_tts import OpenAICompatTTSBackend
        from .text_preprocess import strip_all_markup
        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        assert backend.get_preprocess() is strip_all_markup
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest Module/tts/test_tts.py -v`
Expected: 所有测试 PASS（含新增的协议 + backend provider_name 测试）

- [ ] **Step 8: Commit**

```bash
git add Module/tts/gpt_sovits.py Module/tts/minimax_tts.py Module/tts/openai_tts.py Module/tts/test_tts.py
git commit -m "feat: implement provider_name + get_preprocess() in all TTS backends"
```

---

### Task 3: TTSStreamManager — 去硬编码 `strip_all_markup`

**Files:**
- Modify: `core/tts_stream.py`
- Modify: `core/tests/` 相关测试文件（如有）

**Produces:** `TTSStreamManager` 使用 backend 提供的预处理函数，热切换时同步更新

- [ ] **Step 1: 修改构造函数**

在 `TTSStreamManager.__init__()` 中：
- 删除第 23 行：`from Module.tts.text_preprocess import strip_all_markup`
- 在第 64 行 `self._char_name = character_name` 之后添加：

```python
self._preprocess = lambda text: text  # 默认：不预处理
if tts is not None:
    fn = tts.get_preprocess()
    if fn is not None:
        self._preprocess = fn
```

- [ ] **Step 2: 修改 `stream_sentence()`**

第 91 行，将：

```python
_clean = strip_all_markup(sentence)
```

替换为：

```python
_clean = self._preprocess(sentence)
```

同时更新注释（第 90 行）：

```python
# 跳过空句 — 预处理后为空会导致 TTS API 返回 400
```

- [ ] **Step 3: 修改 `set_backend()`** — 在 `self._tts = tts` 后添加：

```python
if tts is not None:
    fn = tts.get_preprocess()
    self._preprocess = fn if fn is not None else lambda text: text
else:
    self._preprocess = lambda text: text
```

完成后 `set_backend()` 应为：

```python
def set_backend(self, tts: TTSBackend | None) -> None:
    """热切换 TTS 后端。"""
    self._tts = tts
    if tts is not None:
        fn = tts.get_preprocess()
        self._preprocess = fn if fn is not None else lambda text: text
    else:
        self._preprocess = lambda text: text
```

- [ ] **Step 4: 验证导入**

Run: `python -c "from core.tts_stream import TTSStreamManager; print('OK')"`
Expected: OK

- [ ] **Step 5: 运行 TTS 测试确认无回归**

Run: `python -m pytest Module/tts/test_tts.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add core/tts_stream.py
git commit -m "refactor: replace hardcoded strip_all_markup with backend-provided preprocess function"
```

---

### Task 4: Prompt 层 — OutputFormatSection + 模板

**Files:**
- Modify: `Module/prompt/sections/output_format.py`
- Modify: `Module/prompt/templates/f2_output_format.j2`

**Produces:** `OutputFormatSection` 接收 `tts_provider`，模板根据 provider 输出不同标记指令

- [ ] **Step 1: 修改 `OutputFormatSection`**

```python
"""F2: 输出格式。"""
from Module.prompt.protocol import Jinja2Section


class OutputFormatSection(Jinja2Section):
    name = "OutputFormat"
    version = "1.0.0"
    description = "输出格式：inner_voice 双轨协议 + TTS 标记指令"

    def set_context(self, **kwargs) -> None:
        self._tts_provider = kwargs.get("tts_provider", "")

    async def render(self) -> str:
        return self._render_template(tts_provider=self._tts_provider)
```

- [ ] **Step 2: 修改 `f2_output_format.j2`**

在文件末尾（`</|code|>` 行之后）追加：

```jinja2

{% if tts_provider == "minimax" %}
## 语音合成标记
你的对话输出将直接用于语音合成。在对话文本（非 <inner_voice> 内部）中可以使用以下标记来控制语音表现：
- 语气词：(laughs) (sighs) (crying) (gasps) (breath) (humming) (sniffs) (sneezes) (coughs) (groans) (pant) (chuckle) (clears throat) (applause)
- 停顿控制：<#0.5#> 短停顿、<#1#> 中停顿、<#2#> 长停顿（秒数 0.01~99.99）
- 发音标注：将拼音或音标放在括号内，如 处理(chu3li3)
这些标记仅用于公开对话文本，不要在 <inner_voice> 内部使用。
{% elif tts_provider == "gpt-sovits" or tts_provider == "custom" %}
## 语音合成标记
你的对话输出将直接用于语音合成。请输出纯文本，不要使用任何特殊标记符号（如 []、()、<> 等）。
{% endif %}
```

- [ ] **Step 3: 验证模板渲染**

Run:
```python
python -c "
from Module.prompt.sections.output_format import OutputFormatSection
import asyncio

async def test():
    s = OutputFormatSection('Module/prompt/templates/f2_output_format.j2')
    
    # TTS 未连接
    s.set_context(tts_provider='')
    r = await s.render()
    assert '语音合成标记' not in r, 'empty provider should not output TTS instructions'
    
    # MiniMax
    s.set_context(tts_provider='minimax')
    r = await s.render()
    assert '(laughs)' in r, 'minimax should include tone tags'
    assert '<#0.5#>' in r, 'minimax should include pause control'
    
    # GPT-SoVits
    s.set_context(tts_provider='gpt-sovits')
    r = await s.render()
    assert '纯文本' in r, 'gpt-sovits should say plain text'
    assert '(laughs)' not in r, 'gpt-sovits should NOT include tone tags'
    
    print('OK')

asyncio.run(test())
"
```
Expected: OK

- [ ] **Step 4: 运行 prompt 测试确认无回归**

Run: `python -m pytest Module/prompt/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add Module/prompt/sections/output_format.py Module/prompt/templates/f2_output_format.j2
git commit -m "feat: add TTS-aware markup instructions to OutputFormatSection and f2 template"
```

---

### Task 5: PromptAssembler — 透传 `tts_provider`

**Files:**
- Modify: `core/prompt_assembler.py`

**Produces:** `build_system_prompt()` 和 `build_messages()` 接受 `tts_provider` 参数并透传

- [ ] **Step 1: 修改 `build_system_prompt()` 签名**

```python
async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                               stt_emotion: str = "", stt_events=None,
                               tts_provider: str = "") -> str:
```

- [ ] **Step 2: 在 `set_context()` 调用中添加 `tts_provider`**

```python
self._manager.set_context(
    user_message=user_message,
    user_name=user_name,
    user_pronoun=self._character.user_pronoun,
    stt_emotion=stt_emotion,
    stt_events=stt_events or [],
    tts_provider=tts_provider,
)
```

- [ ] **Step 3: 修改 `build_messages()` 签名**

```python
async def build_messages(self, user_message: str = "",
                          user_name: str = "",
                          stt_emotion: str = "",
                          stt_events=None,
                          content=None,
                          supports_multimodal: bool = False,
                          tts_provider: str = "") -> list[dict]:
```

- [ ] **Step 4: 在 `build_messages()` 内部调用中传入 `tts_provider`**

```python
system = await self.build_system_prompt(
    user_message=text, user_name=user_name,
    stt_emotion=stt_emotion, stt_events=stt_events,
    tts_provider=tts_provider,
)
```

- [ ] **Step 5: 验证导入**

Run: `python -c "from core.prompt_assembler import PromptAssembler; print('OK')"`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add core/prompt_assembler.py
git commit -m "feat: thread tts_provider through PromptAssembler to system prompt"
```

---

### Task 6: CognitiveLoop — 传入 `tts_provider`

**Files:**
- Modify: `core/cognitive_loop.py`

**Produces:** `_think_loop()` 传入 `tts_provider` 到 `build_messages()`

- [ ] **Step 1: 在第 269 行 `build_messages()` 调用处，添加 `tts_provider` 参数**

在 `messages = await self._prompt.build_messages(` 调用中，`supports_multimodal=supports_mm,` 之后添加：

```python
tts_provider = self._tts.provider_name if self._tts else ""
messages = await self._prompt.build_messages(
    user_message=user_message, user_name=user_name,
    stt_emotion=stt_emotion, stt_events=stt_events,
    content=content,
    supports_multimodal=supports_mm,
    tts_provider=tts_provider,
)
```

- [ ] **Step 2: 验证导入**

Run: `python -c "from core.cognitive_loop import CognitiveLoop; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "feat: pass tts_provider from CognitiveLoop to prompt system"
```

---

### Task 7: 最终验证

- [ ] **Step 1: 运行全部 TTS 测试**

Run: `python -m pytest Module/tts/ -v`
Expected: ALL PASS

- [ ] **Step 2: 运行 prompt 测试**

Run: `python -m pytest Module/prompt/ -v`
Expected: ALL PASS

- [ ] **Step 3: 运行 prompt 集成测试**

Run: `python -m pytest Module/prompt/tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: 全量相关测试**

Run: `python -m pytest Module/tts/ Module/prompt/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit（如有修改）**

```bash
git add -A
git commit -m "test: final verification — all TTS + prompt tests pass"
```
