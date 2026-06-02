# Phase 2: 测试缺口修复 — LLM / STT / TTS / Embedding 服务模块

> **Status: COMPLETE** — 2026-06-02, 4 tasks done, 135 tests passing.
> Commits: `25f8523` `070f394` `7d88a97` `501becc`

**Goal:** 补充 LLM、STT、TTS、Embedding 四个服务模块的 API 错误处理、流式路径、边界条件和缺失方法测试

**Architecture:** 每个模块追加测试到已有 `test_<module>.py` 文件；mock `httpx.AsyncClient` / `openai.AsyncOpenAI` 模拟 API 响应；使用 `respx` 或 `unittest.mock.patch` 注入 HTTP 错误

**Tech Stack:** Python 3.13, pytest, asyncio, unittest.mock, respx (optional)

**Spec:** [2026-06-02-test-audit-design.md](../specs/2026-06-02-test-audit-design.md) 第四至七节

---

## File Structure

```
Module/llm/test_llm.py           # 追加: stream_think, API errors, prefix edge cases
Module/stt/test_stt.py           # 追加: parse_emotion, parse_events, HTTP errors
Module/tts/test_tts.py           # 追加: emoji, full payload, HTTP errors
Module/embedding/test_embedding.py  # 追加: shutdown, singleton, HuggingFace from_config
```

---

### Task 1: Module/llm/test_llm.py 追加 — stream_think / API 错误 / prefix 边缘

**Files:**
- Modify: `Module/llm/test_llm.py` — 追加测试类到文件末尾
- Read first: `Module/llm/protocol.py` (LLMBackend ABC), `Module/llm/openai_compat.py` (OpenAICompatBackend), `Module/llm/registry.py` (LLMFactory)

**Test scenarios:**

#### A. stream_think() — 零覆盖
- `LLMBackend.stream_think()` protocol default raises NotImplementedError — 验证抽象基类行为
- `OpenAICompatBackend.stream_think()` 正常流式返回 — mock `_call_api(stream=True)` 返回多个含 `delta.content` 的 chunk，验证异步迭代器产出正确文本
- `stream_think()` 中 tool_calls 检测 — mock `_call_api` 返回含 `delta.tool_calls` 的 chunk，验证 `_stream_structured()` 分支的 tool_calls 累积逻辑
- `stream_think()` 中 `_call_api` 抛出 `openai.APIStatusError` — 验证被 `_extract_status()` 处理后传播

#### B. API 错误处理 — 零覆盖
- `chat()` 中 `_call_api` 返回 `choices[0].message.content = None` — 验证返回空字符串 `""`
- `chat_stream()` 中 delta 无 content — 验证跳过该 chunk 继续
- `_call_api` HTTP 4xx — 验证 `raise_for_status` 或 API 错误向上传播
- `_call_api` 网络超时 — 验证 `httpx.ReadTimeout` 传播
- `_call_api` 畸形 JSON 响应 — 验证异常传播

#### C. prefix continuation 边缘
- `chat_stream()` + prefix continuation — mock `_beta_client`，验证 prefix 消息通过 `extra_body` 传递
- 调用者同时传入 `extra_body` — 验证与 prefix 的 `extra_body` 正确合并 (line 132-136)
- 非 DeepSeek provider 的 prefix 行为 — 验证 `ValueError` 或 fallback

#### D. 其他
- `close()` 关闭 `_beta_client` — 设置 `_beta_client is not None`，验证 `close()` 同时关闭两个 client
- `model_id` 含多个 `/` — `"provider/model/variant"` 解析为 `provider="provider", model="model/variant"`
- `LLMFactory.create()` 注入 `get_caller_logger()` — 验证 backend 上的 logger 属性非 None

**运行:** `conda run -n Elysia python -m pytest Module/llm/test_llm.py -v`

---

### Task 2: Module/stt/test_stt.py 追加 — _parse_emotion / HTTP 错误 / 边缘路径

**Files:**
- Modify: `Module/stt/test_stt.py` — 追加测试类
- Read first: `Module/stt/sensevoice.py` (SenseVoiceBackend)

**Test scenarios:**

#### A. _parse_emotion / _parse_events — 零覆盖
- `_parse_emotion("hello [happy] world")` → `("hello world", "happy")`
- `_parse_emotion("hello world")` → `("hello world", "")` — 无情绪标签
- `_parse_emotion("")` → `("", "")` — 空字符串
- `_parse_events("hello [Laughter] world")` → `("hello world", ["Laughter"])`
- `_parse_events("hello")` → `("hello", [])`
- 验证 `_EMOTION_TAGS` / `_EVENT_TAGS` 字典和对应的正则模式

#### B. `<|nospeech|>` 丢弃路径
- API 返回 `{"result": {"text": "<|nospeech|>"}}` → 验证 `raw_text` 为空 → 日志 warn → 返回空字符串
- API 返回 `{"result": {"text": "hello <|nospeech|> world"}}` → 验证 `<|nospeech|>` 被过滤

#### C. HTTP 错误处理
- `httpx.RequestError` → 验证 `transcribe()` 包装为 `RuntimeError`
- HTTP 4xx → `response.raise_for_status()` 异常传播
- 畸形 JSON → `response.json()` 抛出 `json.JSONDecodeError` → 传播

#### D. 其他边缘路径
- `data.get("result")` 是 dict 但无 `"text"` key → 验证 warning log → 返回 `""`
- `data.get("result")` 是 falsy → 验证 warning log
- `raw_text` 被设置但 emotion/events 未更新 → 验证 `self.last_emotion` / `self.last_events` 保持旧值
- kwargs 透传到 `transcribe()` / `transcribe_stream()`

**运行:** `conda run -n Elysia python -m pytest Module/stt/test_stt.py -v`

---

### Task 3: Module/tts/test_tts.py 追加 — emoji / payload / HTTP 错误

**Files:**
- Modify: `Module/tts/test_tts.py` — 追加测试类
- Modify: `Module/tts/test_config.py` — 可能需要追加 (已知失败)
- Read first: `Module/tts/text_preprocess.py`, `Module/tts/gpt_sovits.py`

**Test scenarios:**

#### A. Emoji 脱敏 — `text_preprocess.py:4-14`
- `strip_all_markup("hello 😀 world")` → `"hello  world"` (emoji 被移除)
- `strip_action_only("hello 😀 world")` → `"hello  world"`
- 测试多种 emoji：笑脸、手势、国旗、肤色修饰符
- `_EMOJI_RE` 正则单独测试

#### B. 纯 markup 文本
- `strip_all_markup("（语气）")` → `""` (全 markup，结果空)
- `strip_all_markup("（语气）hello（动作）")` → `"hello"`

#### C. GPTSoVitsBackend payload 验证
- 构造 backend 并调用 `_call_api("test text", character="default", emotion="happy")`
- mock `httpx.AsyncClient.post`，捕获发送的 JSON payload
- 验证 payload 包含：`text`, `text_language="zh"`, `character`, `emotion`, `top_k`, `top_p`, `temperature`, `batch_size`, `speed_factor`, `stream="yes"`
- 验证 `text = self._preprocess(text)` 在 API 调用前被应用

#### D. HTTP 错误处理
- `httpx.RequestError` → 验证 `synthesize()` 包装为 `RuntimeError`
- `resp.raise_for_status()` HTTP 4xx → 异常传播
- Queue 取消路径 — consumer 提前 `break` 退出迭代循环 → task 被取消 → `asyncio.CancelledError` 被正确处理

#### E. 其他边缘
- `character` 不在 `_voices` 中 → `voice_cfg = self._voices[character]` 抛出 `KeyError`
- `emotion` 参数被接受（测试已有，确认覆盖）

**运行:** `conda run -n Elysia python -m pytest Module/tts/test_tts.py Module/tts/test_config.py -v`

---

### Task 4: Module/embedding/test_embedding.py 追加 — shutdown / 单例 / HuggingFace from_config

**Files:**
- Modify: `Module/embedding/test_embedding.py` — 追加测试类
- Read first: `Module/embedding/registry.py` (EmbeddingFactory), `Module/embedding/huggingface.py`

**Test scenarios:**

#### A. EmbeddingFactory.shutdown() — 零覆盖
- 调用 `EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")` 创建 cached instance
- 调用 `EmbeddingFactory.shutdown()` → 验证：
  - 每个 cached instance 的 `close()` 被调用
  - `_instances` dict 被清空
  - shutdown 后再 `create()` 返回新的 instance
- shutdown 中某个 instance 的 `close()` 抛异常 → `except Exception: pass` → 其他 instance 仍被关闭 → `_instances` 被清空

#### B. 单例缓存行为 — 零覆盖
- 同一个 `model_id` 调用 `create()` 两次 → 返回**同一个对象** (`assert result1 is result2`)
- 不同 `model_id` 调用 `create()` → 返回**不同对象**
- 验证 `_instances` dict 的 key 和 value

#### C. HuggingFaceBackend._find_local_model() — 零覆盖
- 在 `tmp_path` 创建假的 huggingface cache 目录结构
- 验证找到本地模型路径
- 未找到时返回 `None`

#### D. HuggingFace from_config — 零覆盖
- 模仿 OpenAI `from_config` 的测试模式
- 用 `EmbeddingConfig(model="huggingface/BAAI/bge-large-zh-v1.5", ...)` 调用 `HuggingFaceBackend.from_config(cfg)`
- 验证返回的 backend 有正确的 model_id、dimension

#### E. API 错误 / 空输入
- mock `openai.AsyncOpenAI` 的 embeddings.create 抛异常 → `embed_documents()` 传播
- `embed_documents([])` → 空数组输入行为（应返回空列表或抛异常）
- `embed_query("")` → 空字符串行为

**运行:** `conda run -n Elysia python -m pytest Module/embedding/test_embedding.py Module/embedding/test_config.py -v`

---

## 验证

```bash
conda run -n Elysia python -m pytest Module/llm/test_llm.py Module/stt/test_stt.py Module/tts/test_tts.py Module/embedding/test_embedding.py -v
```
