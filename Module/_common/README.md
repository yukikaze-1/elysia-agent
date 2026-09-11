# _common — 共享工具层

> 文档导航：[项目文档](../../README.md)

Module/ 内部共享的零依赖工具函数，供各子模块的 registry、config、test、`__main__` 复用。

## 文件

| 文件 | 用途 |
|------|------|
| `env.py` | `dotenv` 统一入口——`ensure_dotenv()` 查找项目根 `.env` 并加载。所有 `__main__.py` 应通过此函数加载环境变量 |
| `registry_helpers.py` | Registry 共享工具：`resolve_api_key()` / `resolve_base_url()`（从 env var / YAML / kwargs 解析）、`inject_logger()`（按调用栈注入 logger） |
| `test_helpers.py` | `make_test_method()` 工厂——消除 5 个 backend（LLM/STT/TTS/Embedding/DocumentDB）中逐字相同的连通性测试实现 |
| `config_test_helpers.py` | Config 测试工厂——`make_config_test_classes()` 参数化生成 `TestDefaults` + `TestFromConfig` 两个 test class |
| `main_runner.py` | 统一 `__main__` 入口——`ensure_dotenv()` + `asyncio.run()`，一行启动集成烟雾测试 |
| `error_type.py` | `ErrorType` 工具错误分类枚举（纯 enum，零依赖）——Module.tool 与 core.schema 共用，`core.schema.error_type` 为此处的重导出壳 |
| `ssrf.py` | SSRFGuard——`validate_endpoint()`（自定义 Endpoint 静态校验）+ `build_guarded_http_client()`（出站守卫传输层 + 跨源剥头）。2026-08-14 从 `core/input/ssrf.py` 下沉（消除 Module→core 唯一反向依赖），被 core 侧 6 处消费方与 `Module/llm/registry.py` 共用 |
| `task_scope.py` | `AsyncTaskScope`（按 owner 实例化的后台协程集合：`spawn` 强引用 + done callback 统一记未处理异常 + `cancel_all`/`drain`/`aclose`）与 `cancel_and_drain()`（单任务有序收尾）。ARCH-INC-P2-03——全仓后台协程的所有权/异常观察/退出收尾收口点 |

## 后台协程所有权（`task_scope.py`）

新增后台协程一律走 `AsyncTaskScope`，不要裸用 `asyncio.create_task`——`make check-arch`
的 R6 会拦下未登记的裸创建点。

```python
from Module._common.task_scope import AsyncTaskScope, cancel_and_drain

class VoiceSession:
    def __init__(self, session_id: str) -> None:
        self._tasks = AsyncTaskScope(f"voice:{session_id}")

    def _open(self) -> None:
        self._task = self._tasks.spawn(self._loop(), name="voice-ack")

    async def close(self) -> None:
        await self._tasks.aclose(timeout=3.0)   # 取消 + 落定 + 拒绝再 spawn
```

关键语义：

- **`spawn` vs `try_spawn`**：前者要求有运行中的事件循环（否则抛 `RuntimeError` 并关闭
  协程对象，不留 "never awaited" 告警）；后者用于**可能被同步调用**的路径——无 loop 时
  记 warning 返回 `None`，但 owner 已关闭时仍抛 `TaskScopeClosedError`。
- **`drain(timeout)` 是总预算**，不是逐任务超时：N 个任务共享一份预算，靠后的拿到剩余时间。
- **`aclose()` vs `cancel_all()+drain()`**：终态 owner（VoiceSession、ToolPlugin）用前者
  （之后 spawn 响亮失败）；**可重启** owner（Agent 同一实例 `stop()`→`start()`）必须用后者，
  否则重启后一律 `TaskScopeClosedError`。
- **关闭顺序**：先 drain 后台任务，再关它们依赖的资源（repo / HTTP 客户端）。

一个 owner 一个 scope，不要建全局 Supervisor——生命周期归属清楚才是这套东西存在的理由。
详见 `docs/review/2026-08-30-后端架构分层与模块化增量评估.md` §ARCH-INC-P2-03。

## 使用

```python
from Module._common.main_runner import run_module_main


async def main():
    # ... setup ...
    ...


if __name__ == "__main__":
    run_module_main(main)   # 加载 .env + asyncio.run()
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [Module/llm/README.md](../llm/README.md) | LLM 模块——registry_helpers 的主要消费者 |
| [Module/config/README.md](../config/README.md) | 配置系统——config_test_helpers 的应用方 |
