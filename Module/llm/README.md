# LLM Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一的大模型调用模块。提供 `LLMBackend` 接口，通过 `LLMFactory` 按 `provider/model` 标识创建后端实例，调用方不需要关心 API key、base URL 等细节。

## 文件结构

```
llm/
├── __init__.py          # 导出 LLMBackend, LLMFactory
├── __main__.py          # 真实 API 集成测试 (python -m Module.llm)
├── protocol.py          # LLMBackend ABC
├── registry.py          # 后端选择 + LLMFactory.create() / from_config()
├── openai_compat.py     # OpenAICompatBackend — OpenAI 兼容协议 (DeepSeek/OpenAI/MiMo/custom)
├── ollama.py            # OllamaBackend — Ollama **native** /api/chat（Batch 17 / AR-16）
├── config.py            # LLMConfig — dataclass 配置
├── tests/              # 单元测试

└── README.md
```

## 快速开始

```python
from Module.llm import LLMFactory

# 创建后端 — provider/model 格式
backend = LLMFactory.create("deepseek/deepseek-v4-pro")

# 非流式
reply = await backend.chat(
    [{"role": "user", "content": "今天天气怎么样？"}],
    temperature=1.3,
    max_tokens=200,
)

# 流式
async for chunk in backend.chat_stream(messages):
    print(chunk, end="")

await backend.close()
```

## 接口

```python
class LLMBackend(ABC):
    async def chat(self, messages: list[dict], **kwargs) -> str: ...
    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncGenerator[str, None]: ...
    async def stream_think(self, messages: list[dict], **kwargs) -> AsyncGenerator[dict, None]: ...
    async def test(self) -> dict: ...  # {"ok": True/False, ...}
    async def close(self) -> None: ...
```

- `messages` — OpenAI 格式 `[{"role": "user", "content": "..."}]`
- `kwargs` — 透传 temperature / max_tokens / extra_body 等参数，直接传给 API。`extra_body` 会与模块内部的 prefix 参数合并

### stream_think()

流式方法，返回结构化 dict（而非纯文本 token），每个 dict 包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | `str` | 本次产生的文本 token（可能为空字符串） |
| `tool_calls` | `list[dict] \| None` | 本次产出的工具调用增量（无工具调用时为 None） |
| `usage` | `dict \| None` | 流结束时返回的 token 用量统计（仅最后一个 chunk 有值） |

```python
async for chunk in backend.stream_think(messages):
    if chunk["content"]:
        print(chunk["content"], end="")
    if chunk["tool_calls"]:
        process_tools(chunk["tool_calls"])
    if chunk["usage"]:
        log_usage(chunk["usage"])
```

## 支持的后端

| Provider | 模型示例 | API Key 来源 |
|----------|---------|-------------|
| `deepseek` | `deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4-flash` | `DEEPSEEK_API_KEY` 环境变量 |
| `openai` | `openai/gpt-5` | `OPENAI_API_KEY` 环境变量 |
| `ollama` | `ollama/qwen3:latest` | 不需要（**native `/api/chat`**，见下节） |
| `mimo` | 多模态，与 OpenAI 协议兼容 | `MIMO_API_KEY` 环境变量 |
| `custom` | 用户自定义 OpenAI-compatible 端点 | 按端点配置 |
| `gemini` | 占位，未实现 | `GEMINI_API_KEY` |

可通过 `base_url` 参数覆盖默认 URL：

```python
backend = LLMFactory.create("openai/gpt-5", base_url="https://my-proxy.com/v1")
```

## Ollama：为什么它有自己的后端（Batch 17 / KB-13 / AR-16）

> **要接一台 Ollama 跑起来**（安装假设、拉模型、`llm.model` 怎么配、诊断命令、
> `.env` 里那一行为什么写 `/v1` 却不是 OpenAI 兼容协议）见
> [docs/operations.md §6](../../docs/operations.md)。本节只讲**为什么这样设计**。

`LLMFactory.create("ollama/<model>")` 返回 **`OllamaBackend`**（native `POST /api/chat`），
而不是通用 OpenAI 兼容后端。理由不是"每个 provider 都该有类"，而是**语义不可通约**：

Ollama 的 serving context 是**运行时分配**（`options.num_ctx`），而实测（Ollama 0.24.0 /
`qwen2.5:3b`，冷启动 A/B）**OpenAI 兼容端点 `/v1/chat/completions` 会静默忽略该字段**：

| 请求 | `GET /api/ps context_length` |
|---|---|
| `/v1/chat/completions`（无 `options`） | 4096 |
| `/v1/chat/completions` + `options.num_ctx=8192` | **4096（被忽略）** |
| native `/api/chat` + `options.num_ctx=8192` | **8192** |

于是"声明 8192 但实际只服务 4096"会**静默**发生（超窗是 HTTP 200 + 截断，不报错），
而 `providers.yaml::llm.ollama.context_window` 就是 Elysia 的 prompt 预算权威 ⇒
预算装得下 ≠ provider 真的服务这么多（KB-13）。修法是把 runtime context 的**请求**
与预算权威绑在同一个值上：

```text
providers.yaml::llm.ollama.context_window = N（>0）
      ↓ LLMFactory.create() 解析（预算侧读的是同一条目：LLMFactory.context_window）
OllamaBackend(context_window=N)
      ↓ request translation（后端内部，上层不可见）
POST /api/chat { "options": { "num_ctx": N } }
```

三条硬边界（有测试与不变量钉住，见 `docs/architecture/invariants.md` §3.4 OL1–OL6）：

1. **只有显式声明才请求分配**。未声明/`≤0` ⇒ **不发** `num_ctx`（由 daemon 默认管辖，
   诊断面如实报 `requested=None` = "runtime context not controlled by Elysia"）。
   通用预算回退（`budget.DEFAULT_CONTEXT_WINDOW = 32768`）是**上层预算兼容行为**，
   **绝不**当分配指令发出——那是真实显存分配（实测 32768 已需 ~4.3GB VRAM），
   而 `num_ctx=0/-1` 在真实 daemon 上会得到一个 **4 token** 的畸形 runner。
2. **`/api/ps` 不在热路径**。正常路径保持 **1 user generation → 1 provider request**；
   `backend.probe_runtime_context()` 是**显式**诊断调用（configured / requested /
   observed 三分真值），失败不抛且不影响 generation。
3. **`raw_client` 保持 OpenAI 兼容侧车**（懒创建、指向 `<root>/v1`）：它的消费方是
   `WorkflowEngine` Planner/Executor（`chat.completions.create(...)`，OpenAI SDK 形状），
   属本批范围外 ⇒ **工作流路径仍不受 Elysia runtime context 控制**（已知边界）。

其它行为差异（都与 native 的真实语义对齐）：`max_tokens` → `options.num_predict`、
`temperature` → `options.temperature`；`tools` 原样透传，native 的
`message.tool_calls`（`arguments` 是**对象**）被归一成通用 delta 契约（JSON 字符串 +
`index`）；`tool_choice` / `stream_options` / `response_format` / `extra_body` 无 native
对应语义，**丢弃并警告一次**；`prefix: True` **显式拒绝**（静默丢字段会把"续写"变成
"重新回答"，两者都返回 200）。

## Prefix Continuation

DeepSeek 支持前缀续写以强制 JSON 输出。调用方在 messages 末尾添加带 `prefix: True` 的 assistant 消息：

```python
messages = [
    {"role": "system", "content": "请以 JSON 回复"},
    {"role": "assistant", "content": "{\n", "prefix": True},
]
```

模块检测到 `prefix: True` 后：
- 剥离 `prefix` 字段
- 通过 `extra_body={"prefix": True}` 启用前缀续写
- 自动切换至 `https://api.deepseek.com/beta`（`beta_url`）
- 不支持的 provider 会抛出 `ValueError`

DeepSeek 的普通对话和 prefix 对话使用不同的端点，模块内部自动选择：
- 无 prefix → `https://api.deepseek.com`
- 有 prefix → `https://api.deepseek.com/beta`

## 关闭思考模式 (DeepSeek V4 Pro)

DeepSeek V4 Pro 默认开启思考模式（thinking），会导致首 token 延迟显著增加（3-4s）。适合需要深度推理的场景，但对简单对话、结构化输出的场景不必要。

通过 `extra_body={"thinking": {"type": "disabled"}}` 关闭思考模式，大幅降低 TTFT。

> **provider 能力门控（S2-H3）**：`thinking` 是 **DeepSeek 风格**的顶层参数，
> 不是 OpenAI 规范的一部分。后端按 `Module/config/providers.yaml` 的
> `llm.<provider>.supports_thinking` 判定：**未声明（或 false）的 provider 上该键会被
> 丢弃**并打一次 warning；调用方其它 `extra_body` 键原样保留。
> 原因是严格 OpenAI 风格 API 对未知顶层参数返回 `400`，会让该 provider 上
> **100% 回合失败**，而日志只显示 `GENERATION_PROVIDER_UNAVAILABLE`——看不出是请求体
> 带了外字段。未声明 = "不表态"（交给 provider 默认），**不是**"显式关闭"；
> 若某 provider 确实支持，在 providers.yaml 对应条目加 `supports_thinking: true`。

```python
# 关闭思考 — 快速响应
reply = await backend.chat(
    [{"role": "user", "content": "用一句话介绍你自己"}],
    extra_body={"thinking": {"type": "disabled"}},
)

async for chunk in backend.chat_stream(
    messages,
    extra_body={"thinking": {"type": "disabled"}},
):
    print(chunk, end="")
```

实测对比（`deepseek-v4-pro`）：

| 模式 | chat() 总耗时 | chat_stream() TTFT |
|------|:----------:|:---------------:|
| 思考开启 (默认) | ~12.7s | ~4.4s |
| 思考关闭 | ~2.1s | ~0.9s |

## Logger

每个 backend `__init__` 接受可选的 `logger` 参数：

```python
backend = LLMFactory.create("deepseek/deepseek-v4-pro", logger=my_logger)
```

不传则默认使用无 handler 的静默 logger（不输出任何内容）；需要输出的调用方自行传入 `logging.getLogger(...)` 的实例。应用侧的日志落盘与分流由 `core/utils/log_setup.py` 统一配置，库层不设置 handler。

## Config

`LLMConfig` dataclass 支持通过 YAML + 环境变量加载配置，与 `Module/config/` 配合：

```python
from dataclasses import dataclass
from Module.config import ConfigLoader
from Module.llm import LLMFactory
from Module.llm.config import LLMConfig

@dataclass
class MyLLMConfig(LLMConfig):
    pass

cfg = ConfigLoader(LLMConfig, yaml_path="config.yaml", yaml_key="LLM").load()
backend = LLMFactory.from_config("deepseek/deepseek-v4-pro", cfg)
# 覆盖个别参数
backend = LLMFactory.from_config("openai/gpt-5", cfg, base_url="https://proxy/v1")
```

| 字段 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `base_url` | `https://api.deepseek.com` | `LLM_API_BASE` | LLM API 端点。⚠️ **只有 `LLMFactory.from_config` 这条（测试/嵌入式）路径读它**：生产装配是 `LLMFactory.create("<provider>/<model>")`，端点来自 `Module/config/providers.yaml` 的 `url_key`（如 `DEEPSEEK_URL`）⇒ `LLM_API_BASE` 在生产**无调用者**（见 [docs/operations.md](../../docs/operations.md) §3.3） |
| `api_key` | `""` | `LLM_API_KEY` | API 密钥（secret，仅从 env 读取）。同上：生产走 `providers.yaml` 的 `env_key`（如 `DEEPSEEK_API_KEY`），`LLM_API_KEY` **无生产调用者** |

## 添加新后端

1. 在 `llm/` 下新建 `your_backend.py`
2. 继承 `LLMBackend`，实现 `chat` 和 `chat_stream`
3. 在 `registry.py` 的 `_BACKENDS` 中注册

```python
from .protocol import LLMBackend

class YourBackend(LLMBackend):
    def __init__(self, model, *, api_key, base_url, supports_prefix, logger=None):
        ...

    async def chat(self, messages, **kwargs) -> str:
        ...

    async def chat_stream(self, messages, **kwargs):
        ...
```

## 测试

```bash
# 单元测试（mock，不需要 API key）
python -m pytest Module/llm/tests/ -v

# 真实 Ollama 的 runtime context 旁证（**opt-in**：需要本地 daemon，会卸载测试模型；
# 不进 quick/normal/full，CI 不依赖 Ollama）
RUN_EXTERNAL_TESTS=1 python -m pytest Module/llm/tests/test_ollama_real.py -q -s

# 真实 API 集成测试（需要 .env 中的 DEEPSEEK_API_KEY）
python -m Module.llm

# 统一延迟基准测试（含 LLM/TTS/STT 流式和非流式）
python -m Module.bench
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/config/README.md](../config/README.md) | Config 基础设施（LLM 配置依赖） |
| [Module/output/README.md](../output/README.md) | 输出通道（LLM 流式输出目标） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 数据流 |
