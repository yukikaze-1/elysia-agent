# 测试指南

## 分层体系

测试统计由 `scripts/print_test_stats.py` 生成（F5：不手写易漂移数字）——`conda run -n Elysia python scripts/print_test_stats.py`。当前（2026-09-10）全量收集 4287：

| 层 | marker | 标准 | 测试数 |
|---|--------|------|--------|
| 单元 | `unit` | 纯函数/类，无 I/O，无 Agent | 2313 |
| 服务 | `service` | 单模块 + mock，不启动 Agent | 1911 |
| 集成 | `integration` | 需真实数据库或外部 API | 39 |
| 慢速 | `slow` | 完整 Agent 启动/端到端/单测 >1s | 53 |
| 外部 | `external` | 需真实 LLM/DB 凭据（F1，独立 opt-in） | 36 |

> **口径说明（F1 起）**：`external` 独立于四层——真实 LLM E2E（WorkflowEngine 集成）不进合并门禁；合并门禁 `-m "not external"` 收集 4251。2026-09-10 实测 `make test-quick`（`-m "unit or service"`）4210 passed / 2 skipped；`make test-normal`（`-m "not slow and not external"`）4213 passed / 2 skipped。各层相加与全量的差异（多 marker 重复计入）以脚本输出为准。
>
> **标记纪律（2026-09-10 复审）**：全仓**不得有无 marker 的测试**——无 marker 会被 `make test-quick` 静默跳过，PR 门禁对它不设防（曾积累 188 条）。同时**不得出现 `(slow|integration|external) and (unit|service)`** 的叠加——pytest 的 marker 只增不减，模块级 `pytestmark` 会覆盖类级 `slow`，把真 I/O 用例塞进快门禁（曾 10 条）。两条不变量可自查：
>
> ```bash
> pytest -q --collect-only -m "not (unit or service or integration or slow or external)" | grep -c "::"   # 期望 0
> pytest -q --collect-only -m "(slow or integration or external) and (unit or service)" | grep -c "::"   # 期望 0
> ```
>
> 另：`pytest.ini` 设 `timeout = 300`（`pytest-timeout`，`thread` 方式）——无界 `await` 回归成挂死时不再无限期挂住 CI。

### 外部测试门禁（E4，`RUN_EXTERNAL_TESTS=1`）

`external` 测试（真实 LLM/DB E2E）**双保险门禁**：

1. 合并门禁 `-m "unit or service"` / `-m "not slow and not external"` 已排除 `external` marker。
2. `Module/conftest.py` 的 collection hook：未设 `RUN_EXTERNAL_TESTS=1` 时，所有 `external` 测试整体 `skip`（用 `item.add_marker(skipif)`——裸 `pytest.skip()` 在 collection 阶段会中断收集）。

即「环境恰有 `DEEPSEEK_API_KEY` + 显式 `-m external` 但忘设 env」也不会误跑真实 LLM。显式 opt-in：

```bash
make test-external
# 或: conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short
```

真实 LLM 测试必须同时标 `integration + external`。workflow 确定性测试基建在
`Module/workflow_engine/tests/helpers/fake_llm.py`：`FakeLLM`（对齐 `chat.completions.create`
的 `tool_calls`/`content`/`usage` 访问面 + `gate`/`calls`/`prompts`）、`with_deadline`（gated 测试
挂死防线，`asyncio.wait_for` 封装）、`CountingLLM`（外部成本计数包装，委托透传 `messages`）。

## 日常命令

```bash
conda activate Elysia

# 日常开发 — 秒级验证（~60s）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow and not external" -q --tb=short

# 全量 — 推之前（~20min）
make test-full
# 或: pytest -q

# 导入风格门禁（秒级）
make lint
# 或: ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__
```

## Import 门禁（ruff TID252）

强制绝对导入：`from core.xxx import y` / `from Module.xxx import y`，禁止相对导入（`from .module` / `from ..module`）。`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免。配置见根目录 `ruff.toml`；提交前用 `make lint` 验证。

```bash
# 自动修复相对导入 → 绝对（一次性迁移用）
ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__ --fix --unsafe-fixes

# 查非豁免文件残留相对导入（应输出为空）
grep -rEn "^from \." core/ Module/ --include="*.py" \
  | grep -v __pycache__ | grep -vE "__init__\.py|__main__\.py|/tests/"
```

## 按模块运行

```bash
# 按 marker 过滤
pytest -m unit -q                          # 只跑纯单元测试
pytest -m "unit or service" -q             # 快速验证
pytest -m "not slow" -q                    # 跑除了慢速之外的所有
pytest -m slow -q                          # 只跑慢速测试

# 按目录运行
pytest Module/llm/ -v                      # LLM 模块
pytest Module/tts/ -v                      # TTS 模块
pytest Module/database/ -v                 # 数据库模块
pytest core/tests/ -v                      # Core 测试
pytest core/agent/tests/ -v                # Agent 生命周期
pytest core/config/tests/ -v               # 配置 dataclass
pytest core/monitoring/tests/ -v           # Token 监控
pytest core/task/tests/ -v                 # Task 调度
pytest core/tools/ -v                      # 工具插件
pytest Module/workflow_engine/tests/unit/ -v   # WorkflowEngine 单元
pytest Module/workflow_engine/tests/integration/ -v  # WorkflowEngine 集成
pytest frontend_contract/tests/ -v         # 前后端契约（建模 + TS 生成器）

# 按文件运行
pytest Module/llm/tests/ -v
pytest Module/config/tests/ -v
pytest core/tests/test_phase3.py -v
```

## 测试目录索引

各目录的**测试数量不在此手写**——手写数字必然漂移（本节此前即因之全面过期）。取权威数字用：

```bash
# 全仓分层统计（各 marker 分布 + 合并门禁范围）
conda run -n Elysia python scripts/print_test_stats.py

# 单个目录的收集数
conda run -n Elysia python -m pytest <目录> -q --collect-only | tail -1

# 单个文件里每个测试实际带的 marker
conda run -n Elysia python -m pytest <文件> --collect-only -q -m "unit or service"
```

| 层 | 测试目录 |
|----|----------|
| `Module/` | `_common/tests/`、`config/tests/`、`database/documentdb/tests/`、`database/vectordb/tests/`、`embedding/tests/`、`llm/tests/`、`logger/tests/`、`output/tests/`、`stt/tests/`、`system_prompt/tests/`（含 `unit/`、`integration/`）、`tool/tests/`、`tts/tests/`、`workflow_contract/tests/`、`workflow_engine/tests/unit/`、`workflow_engine/tests/integration/` |
| `core/` | `tests/`、`status/tests/`、`agent/tests/`、`config/tests/`、`monitoring/tests/`、`task/tests/`、`text/tests/`、`debug/tests/`、`cognitive/tests/`、`desktop/tests/`、`input/tests/`、`memory/tests/`、`personality/tests/`、`prompt/tests/`、`schema/tests/`、`schema/adapters/tests/`、`storage/tests/`、`tools/tests/`、`adapter/tests/` |
| `frontend_contract/` | `tests/` |
| 仓库根 | `scripts/tests/`、`tests/` |

`frontend_contract/` 为前后端契约单源包（Pydantic 模型 + TS 生成器，详见
[frontend_contract/README.md](../frontend_contract/README.md)），测试为纯函数级（无 I/O、无 Agent）→
marker=unit。`make gen-types` 重新生成 `frontend/src/types/contract.generated.ts`，
`make check-types` 漂移门禁。

## Marker 标准

### unit

- 只依赖 Python 标准库和被测模块
- 无 `asyncio`、无 mock 外部服务
- 不访问文件系统（tempfile 除外）、网络、数据库
- 不创建 Agent 实例

```python
# 模块级标记
pytestmark = pytest.mark.unit

# 或类级标记
@pytest.mark.unit
class TestMyFunction:
    ...
```

### service

- 单模块测试，mock 外部依赖（用 `unittest.mock`、`FakeLLMBackend` 等）
- 可能使用 `asyncio`，但不启动完整 Agent
- 可以访问临时文件、mock 的数据库连接
- 创建 `Agent` 但不调用 `agent.start()` → service

```python
pytestmark = pytest.mark.service
```

### integration

- 需要真实 MongoDB/Milvus 连接
- 需要真实外部 API（LLM/TTS/STT）
- 不 mock 关键依赖

```python
pytestmark = pytest.mark.integration
```

### slow

- 创建 `Agent(config=AppConfig())` 并调用 `agent.start()`
- 完整管线端到端测试
- 单测耗时 >1s
- 大量 `asyncio.sleep()` 累积

```python
@pytest.mark.slow
class TestAgentLifecycle:
    ...
```

## 已知问题

- **`test_touch_resets_timer`** — ✅ 已修复：`IdleMonitor` 计时已改用 `time.monotonic()`（测试内保留 `time.sleep(0.001)` 仅作时间推进）。
- **`test_resume_idempotent_on_running`** — WorkflowEngine 历史 race（task 过快完成导致 `resume()` 返回不符合预期），保留为集成测试观测项。
- **`test_ok_returns_latency`** — ✅ 已修复：`ok_check(self)` 多余参数已移除（`Module/_common/tests/test_test_helpers.py`）。

## 添加新测试

1. 确定层级：对照上述标准，选择 `unit` / `service` / `integration` / `slow`
2. 加 marker：class 或 module 级别
3. 文件名：`test_<功能>.py`，放在对应模块的 `tests/` 目录下
4. 验证：`pytest -m <marker> 新文件路径 -v`
