# _common — 共享工具层

> 文档导航：[项目文档](../../README.md)

Module/ 内部共享的零依赖工具函数，供各子模块的 registry、config、test、`__main__` 复用。

## 文件

| 文件 | 用途 |
|------|------|
| `env.py` | `dotenv` 统一入口——`ensure_dotenv()` 查找项目根 `.env` 并加载。所有 `__main__.py` 应通过此函数加载环境变量 |
| `registry_helpers.py` | Registry 共享工具：`resolve_api_key()` / `resolve_base_url()`（从 env var / YAML / kwargs 解析）、`inject_logger()`（按调用栈注入 logger） |
| `test_helpers.py` | `test()` 工厂——消除 5 个 backend（LLM/STT/TTS/Embedding/DocumentDB）中逐字相同的连通性测试实现 |
| `config_test_helpers.py` | Config 测试工厂——`make_config_test_classes()` 参数化生成 `TestDefaults` + `TestFromConfig` 两个 test class |
| `main_runner.py` | 统一 `__main__` 入口——`ensure_dotenv()` + `asyncio.run()`，一行启动集成烟雾测试 |

## 使用

```python
from Module._common.env import ensure_dotenv
from Module._common.main_runner import run_main

ensure_dotenv()
# ... setup ...
run_main(main_coro)
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [Module/llm/README.md](../llm/README.md) | LLM 模块——registry_helpers 的主要消费者 |
| [Module/config/README.md](../config/README.md) | 配置系统——config_test_helpers 的应用方 |
