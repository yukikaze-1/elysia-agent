# LLM Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一的大模型调用模块。提供 `LLMBackend` 接口，通过 `LLMFactory` 按 `provider/model` 标识创建后端实例，调用方不需要关心 API key、base URL 等细节。

## 文件结构

```
llm/
├── __init__.py          # 导出 LLMBackend, LLMFactory
├── __main__.py          # 真实 API 集成测试 (python -m Module.llm)
├── protocol.py          # LLMBackend ABC
├── registry.py          # 后端注册表 + LLMFactory.create() / from_config()
├── openai_compat.py     # OpenAICompatBackend — OpenAI 兼容协议 (DeepSeek/OpenAI/Ollama)
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
```

- `messages` — OpenAI 格式 `[{"role": "user", "content": "..."}]`
- `kwargs` — 透传 temperature / max_tokens / extra_body 等参数，直接传给 API。`extra_body` 会与模块内部的 prefix 参数合并

## 支持的后端

| Provider | 模型示例 | API Key 来源 |
|----------|---------|-------------|
| `deepseek` | `deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4-flash` | `DEEPSEEK_API_KEY` 环境变量 |
| `openai` | `openai/gpt-5` | `OPENAI_API_KEY` 环境变量 |
| `ollama` | `ollama/qwen3:latest` | 不需要 |
| `gemini` | 占位，未实现 | `GEMINI_API_KEY` |

可通过 `base_url` 参数覆盖默认 URL：

```python
backend = LLMFactory.create("openai/gpt-5", base_url="https://my-proxy.com/v1")
```

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

通过 `extra_body={"thinking": {"type": "disabled"}}` 关闭思考模式，大幅降低 TTFT：

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

不传则默认使用无 handler 的静默 logger（不输出任何内容），调用方可通过 `Module/logger/LoggerFactory` 注入。

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
| `base_url` | `https://api.deepseek.com` | `DEEPSEEK_API_BASE` | LLM API 端点 |
| `api_key` | `""` | `DEEPSEEK_API_KEY` | API 密钥（secret，仅从 env 读取） |

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
