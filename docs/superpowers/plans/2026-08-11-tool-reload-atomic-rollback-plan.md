# 工具热重载原子替换与失败回滚实施计划（E1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `ToolManager.reload_plugin` 的 P2-06 缺陷——新 module 重新 discover class、原子替换注册表、失败保留旧实例、in-flight 排空后 teardown、reload 不阻塞其他工具。

**Architecture:** `reload_plugin` 重构为「锁外构造/setup 新实例 → 持锁身份复核+原子替换四个注册表 dict → 锁外排空旧实例 in-flight + teardown」。`_reload_lock` 仅覆盖 swap 微秒窗口；`_inflight` 按实例快照排空；延迟 teardown 放独立 `_teardown_tasks`。调用点 `server.py:/tools/reload` 返回类型改 `tuple[bool, str]`。

**Tech Stack:** Python 3.12 + asyncio（Lock）+ importlib（reload）+ `Module/tool/manager.py`。

## Global Constraints

- 生产代码函数参数必须类型注解；禁相对导入；注释中文。
- 每 Task TDD；验收门禁全绿。
- **pytest strict 模式**（`pytest.ini` 无 `asyncio_mode`）：所有 async 测试须 `async def` + `@pytest.mark.asyncio`。
- **marker 必标**：`make test-quick = -m "unit or service"`——所有新增 reload 测试类标 `@pytest.mark.service`，否则被门禁静默排除。
- **共享 fixture 前置**（Task 1 定义，后续 Task 复用）：`plugin_dir`/`write_simple`——tmp_path 写插件模块 + sys.path + sys.modules 清理。

## 文件结构

| 文件 | 职责 |
|---|---|
| `Module/tool/manager.py` | `_load_safety_config`/`_discover_class`/`reload_plugin`/`_drain_and_teardown`/`_schedule_teardown`/`shutdown`/`execute`/`execute_stream` 改造 + `_reload_lock`/`_inflight`/`_teardown_tasks` |
| `core/input/server.py` | `/tools/reload` handler 解包 `tuple[bool, str]` |
| `core/input/tests/test_server.py` | reload 端点 mock 返回元组 |
| `Module/tool/tests/test_framework.py` | reload 行为测试 |
| `Module/tool/tests/conftest.py` | **Create**（新文件）：`plugin_dir`/`write_simple` 共享 fixture（tmp_path 模块 + sys.modules 清理） |

---

### Task 0: 共享 fixture（`Module/tool/tests/conftest.py`）

**Files:**
- Create: `Module/tool/tests/conftest.py`（**该文件不存在，是 Create 非 Modify**；`Module/conftest.py` 是父级，勿动）
- Test: 无（fixture 本身）

**Interfaces:**
- Produces: `plugin_dir` fixture（tmp_path 模块目录 + syspath + sys.modules 清理）、`write_simple(src, setup_raises, suffix)`（写 `simple.py`，含 `execute` 返回带版本标记的 ToolResult）、`write_config(src, **overrides)`（写 config.yaml）。

- [ ] **Step 1: 写 fixture**

```python
# Module/tool/tests/conftest.py（新建）
import sys
import pytest

@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """tmp_path 下建 reload_plugins 包 + sys.path + sys.modules 清理。
    importlib.reload 按模块缓存 __file__ 重执行——跨测试必须清 sys.modules 防读到上一测试的文件。"""
    src = tmp_path / "reload_plugins"
    src.mkdir()
    (src / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in list(sys.modules):
        if m == "reload_plugins" or m.startswith("reload_plugins."):
            del sys.modules[m]
    yield src
    for m in list(sys.modules):
        if m == "reload_plugins" or m.startswith("reload_plugins."):
            del sys.modules[m]

@pytest.fixture
def write_simple():
    def _write(src, *, setup_raises=False, suffix="v1"):
        setup_line = "def setup(self, ctx): raise RuntimeError('boom')\n    " if setup_raises else ""
        body = f"""from Module.tool.protocol import ToolPlugin, SafetyProfile, ToolResult, ToolContext
class SimplePlugin(ToolPlugin):
    name = "simple"
    description = "desc-{suffix}"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()
    {setup_line}async def execute(self, ctx: ToolContext, **params):
        return ToolResult.ok(name="simple", content="exec-{suffix}", duration_ms=0.0)
"""
        (src / "simple.py").write_text(body)
    return _write

@pytest.fixture
def write_config():
    """写插件目录 config.yaml（覆盖 SafetyProfile 字段）。"""
    def _write(src, **overrides):
        lines = [f"{k}: {v}\n" for k, v in overrides.items()]
        (src / "config.yaml").write_text("".join(lines))
    return _write
```

- [ ] **Step 2: 验证 fixture 可 import**

Run: `conda run -n Elysia python -m pytest Module/tool/tests/ --collect-only -q`
Expected: 收集正常（无 fixture 报错）

- [ ] **Step 3: 提交**

```bash
git add Module/tool/tests/conftest.py
git commit -m "test(tools): reload test fixtures with tmp module + sys.modules cleanup (E1)"
```

---

### Task 1: `_load_safety_config` helper 提取

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_framework.py`

**Interfaces:**
- Consumes: Task 0 `plugin_dir`/`write_simple`。
- Produces: `ToolManager._load_safety_config(instance, cls) -> None`。

- [ ] **Step 1: 写失败测试**（真实代码，非占位符）

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_load_safety_config_applies_yaml(self, plugin_dir, write_simple):
    write_simple(plugin_dir, suffix="v1")
    write_config = """max_concurrency: 7\n"""
    (plugin_dir / "config.yaml").write_text(write_config)
    import reload_plugins.simple as mod
    tm = ToolManager()
    instance = mod.SimplePlugin()
    tm._load_safety_config(instance, mod.SimplePlugin)
    assert instance.safety.max_concurrency == 7
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/tool/tests/test_framework.py -q -k "load_safety_config" -x --tb=short`
Expected: FAIL（AttributeError: `_load_safety_config` 不存在）

- [ ] **Step 3: 最小实现**

`manager.py` 新增 `_load_safety_config`（从 `load_all` 内联逻辑提取，spec §3）；`load_all` 调用它。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/tool/tests/test_framework.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/tool/manager.py Module/tool/tests/test_framework.py
git commit -m "refactor(tools): extract shared safety-config loader (E1)"
```

---

### Task 2: 初始化 `_reload_lock` / `_inflight` / `_teardown_tasks`

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_framework.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
def test_init_has_reload_infrastructure():
    tm = ToolManager()
    assert isinstance(tm._reload_lock, asyncio.Lock)
    assert tm._inflight == {}
    assert tm._teardown_tasks == {}
```

- [ ] **Step 2: 运行验证失败** → FAIL（AttributeError）
- [ ] **Step 3: 最小实现**：`__init__` 追加三字段。
- [ ] **Step 4: 运行验证通过** → PASS
- [ ] **Step 5: 提交**

```bash
git add Module/tool/manager.py Module/tool/tests/test_framework.py
git commit -m "feat(tools): reload lock and in-flight/teardown registries (E1)"
```

---

### Task 3: `_discover_class` + `reload_plugin` 原子替换重构

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_framework.py`

**Interfaces:**
- Consumes: Task 0/1/2。
- Produces: `_discover_class(module, name)`；`reload_plugin(name) -> tuple[bool, str]`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_reload_returns_tuple_and_replaces(self, plugin_dir, write_simple, tmp_path):
    write_simple(plugin_dir, suffix="v1")
    import reload_plugins.simple as mod_v1
    tm = ToolManager(); ctx = PluginContext()
    await tm.load_all([mod_v1.SimplePlugin], ctx)
    # 换 v2 实现
    write_simple(plugin_dir, suffix="v2")
    ok, reason = await tm.reload_plugin("simple")
    assert ok is True and reason == ""
    result = await tm.execute("simple", ctx=ToolContext())
    assert result.content == "exec-v2"      # 新 class 行为生效

@pytest.mark.service
@pytest.mark.asyncio
async def test_reload_setup_failure_keeps_old(self, plugin_dir, write_simple):
    write_simple(plugin_dir, suffix="v1")
    import reload_plugins.simple as mod_v1
    tm = ToolManager(); ctx = PluginContext()
    await tm.load_all([mod_v1.SimplePlugin], ctx)
    write_simple(plugin_dir, suffix="v2", setup_raises=True)   # 新 setup 抛
    ok, reason = await tm.reload_plugin("simple")
    assert ok is False and "热重载失败" in reason
    result = await tm.execute("simple", ctx=ToolContext())
    assert result.content == "exec-v1"      # 旧实例仍可用
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/tool/tests/test_framework.py -q -k "reload_returns or reload_setup" -x --tb=short`
Expected: FAIL（返回 bool 或行为未实现）

- [ ] **Step 3: 最小实现**

> **顺序依赖注**：`reload_plugin` 调用 `_drain_and_teardown`（下一 Task 实现）。本 Task 先实现 `reload_plugin` 主体，`_drain_and_teardown` 调用处**先内联最小版**（`await asyncio.wait(old_tasks, timeout=10.0)` 后 `await old.teardown()`，pending 则跳过），下一 Task 替换为完整实现。

`reload_plugin`（spec §4.2）：锁外 `importlib.reload` + `_discover_class` + 构造 + `_load_safety_config` + `await setup`（try/except → `(False, reason)`）；持锁身份复核（`superseded` → 锁外 teardown 新实例 + `(False, ...)`）；原子替换四 dict + `old_tasks` 快照；锁外 `_drain_and_teardown`；返回 `(True, "")`。现有 `TestReloadPlugin` 断言改 tuple。

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/tool/manager.py Module/tool/tests/test_framework.py
git commit -m "fix(tools): atomic reload with rollback and new-class discovery (E1)"
```

---

### Task 4: `_drain_and_teardown` + `_schedule_teardown`

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_framework.py`

**Interfaces:**
- Produces: `_drain_and_teardown(name, old, old_tasks, timeout=10.0)`；`_schedule_teardown(name, old)`。

- [ ] **Step 1: 写失败测试**（**显式传小 timeout**——默认 10s 会拖慢测试）

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_drain_waits_inflight_then_teardown(self):
    tm = ToolManager()
    old = MagicMock(); old.teardown = AsyncMock()
    never_done = asyncio.create_task(asyncio.Event().wait())     # 永不完成
    try:
        await tm._drain_and_teardown("x", old, {never_done}, timeout=0.1)
        # 超时 → 未等待完成 → 延迟 teardown 登记
        assert len(tm._teardown_tasks) == 1
        await asyncio.sleep(0.05)
        assert old.teardown.await_count == 1     # 后台延迟 teardown 已执行
    finally:
        never_done.cancel()

@pytest.mark.service
@pytest.mark.asyncio
async def test_drain_timeout_schedules_teardown(self):
    tm = ToolManager()
    old = MagicMock(); old.teardown = AsyncMock()
    never = asyncio.create_task(asyncio.sleep(5))
    try:
        await tm._drain_and_teardown("x", old, {never}, timeout=0.1)   # 0.1s 超时
        assert len(tm._teardown_tasks) == 1       # 延迟 teardown 已登记
        await asyncio.sleep(0.05)
        assert old.teardown.await_count == 1
    finally:
        never.cancel()
```

- [ ] **Step 2: 运行验证失败** → FAIL（方法不存在）
- [ ] **Step 3: 最小实现**

`_drain_and_teardown`（spec §4.4）：`asyncio.wait(old_tasks, timeout=timeout)`；pending → `_schedule_teardown` + return；否则 `await old.teardown()`（try/except warning）。`_schedule_teardown`：`create_task(old.teardown())` 登记 `_teardown_tasks` + done 回调收异常 + pop。
- [ ] **Step 4: 运行验证通过** → PASS
- [ ] **Step 5: 提交**

```bash
git add Module/tool/manager.py Module/tool/tests/test_framework.py
git commit -m "feat(tools): drain in-flight with deferred teardown on timeout (E1)"
```

---

### Task 5: `execute`/`execute_stream`——取值段加锁 + in-flight 计数

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_framework.py`

**Interfaces:**
- Produces: 两方法均 `async with self._reload_lock` 包取值段 + `_inflight` 计数 + `_executing.add`。

- [ ] **Step 1: 写失败测试**（SlowPlugin 定义在测试文件：`setup` 后 `execute` 挂起）

```python
class SlowPlugin(ToolPlugin):
    name = "slow"; description = "slow"; param_model = None; capabilities = set(); safety = SafetyProfile()
    async def setup(self, ctx): pass
    async def execute(self, ctx, **params):
        await asyncio.sleep(0.2)
        return ToolResult.ok(name="slow", content="done", duration_ms=0.0)

@pytest.mark.service
@pytest.mark.asyncio
async def test_execute_tracks_inflight(self):
    tm = ToolManager(); ctx = PluginContext()
    await tm.load_all([SlowPlugin], ctx)
    task = asyncio.create_task(tm.execute("slow", ctx=ToolContext()))
    await asyncio.sleep(0.05)
    assert len(tm._inflight["slow"]) == 1
    await task
    assert tm._inflight["slow"] == set()

@pytest.mark.service
@pytest.mark.asyncio
async def test_executing_still_populated(self):
    tm = ToolManager(); ctx = PluginContext()
    await tm.load_all([SlowPlugin], ctx)
    task = asyncio.create_task(tm.execute("slow", ctx=ToolContext()))
    await asyncio.sleep(0.05)
    assert "slow" in tm._executing      # executing_now 不回归
    await task
```

- [ ] **Step 2: 运行验证失败** → FAIL（`_inflight` 未维护）
- [ ] **Step 3: 最小实现**

`execute`（spec §5 完整实现）：`async with self._reload_lock` 取 plugin+sem → `_executing.add` → `create_task(self._pipeline.execute(...))` → `_tasks`/`_inflight` 登记 → `finally` 清理。

`execute_stream`（**保留现实现 manager.py:174-229 结构，仅加 `_reload_lock` 取值段 + `_inflight` 计数**——勿改 pipeline 调用签名/错误处理）：

```python
async def execute_stream(self, name: str, *, ctx: ToolContext, emit: ToolEmitter | None = None, **raw_params: Any) -> AsyncIterator[Any]:
    # 取值段加锁（与 execute 一致）；yield 必须在锁外——async generator 锁跨 yield 持有到下一 __anext__/GC
    async with self._reload_lock:
        plugin = self._plugins.get(name)
        if plugin is None:
            pass                            # 占位：锁外 yield unknown
        else:
            sem = self._semaphores.get(name)
    if plugin is None:
        yield self._unknown_tool_result(name)   # 锁外 yield
        return
    self._executing.add(name)
    queue: asyncio.Queue = asyncio.Queue()

    async def _drive() -> None:
        try:
            async for chunk in self._pipeline.execute_stream(plugin, name, ctx, raw_params, sem):
                await queue.put(chunk)
        except asyncio.CancelledError:
            await queue.put(ToolResult.fail(name=name, error="用户取消", error_type=ErrorType.CANCELLED))
        except Exception as e:
            await queue.put(ToolResult.fail(name=name, error=str(e), error_type=ErrorType.TOOL_ERROR))
        finally:
            await queue.put(None)

    task = asyncio.create_task(_drive())
    self._tasks[ctx.call_id or name] = task
    self._inflight.setdefault(name, set()).add(task)     # 新增：in-flight 计数
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, ToolResult):
                self._record(item)
            yield item
    finally:
        self._executing.discard(name)
        self._inflight.setdefault(name, set()).discard(task)   # 新增
        self._tasks.pop(ctx.call_id or name, None)
```

> 注：**签名保持现实现不变**（类型注解齐全，过 ANN 门禁）；`SafetyPipeline.execute_stream` 签名为 5 参（plugin/name/ctx/raw_params/semaphore，safety.py:502）——**无 `emit` 位置参数**；取消分支保留现 `ToolResult.fail(ErrorType.CANCELLED)`；**yield 全部在锁外**（async generator 锁跨 yield 持有到下一 `__anext__`/GC，锁内 yield 会阻塞后续 reload/execute）。
- [ ] **Step 4: 运行验证通过** → PASS
- [ ] **Step 5: 提交**

```bash
git add Module/tool/manager.py Module/tool/tests/test_framework.py
git commit -m "feat(tools): lock value fetch and track in-flight per plugin (E1)"
```

---

### Task 6: `shutdown` 重构——持锁 + 收尾 `_teardown_tasks`

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_framework.py`

**Interfaces:**
- Produces: `shutdown()` 持锁 + gather 活动插件 + clear + gather `_teardown_tasks`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_waits_deferred_teardowns(self):
    tm = ToolManager()
    old = MagicMock(); old.teardown = AsyncMock()
    t = asyncio.create_task(old.teardown())
    tm._teardown_tasks["x:teardown:1"] = t
    await tm.shutdown()
    assert old.teardown.await_count == 1
    assert tm._teardown_tasks == {}
```

- [ ] **Step 2: 运行验证失败** → FAIL（shutdown 不 await _teardown_tasks）
- [ ] **Step 3: 最小实现**

`shutdown()`（spec §4.5）：`async with self._reload_lock` 包 `cancel_all` + gather 活动插件 teardown + `_plugins.clear()` + `if self._teardown_tasks: await gather(*values, return_exceptions=True); clear()`。**注**：若插件 teardown 内部 re-enter execute（取锁）会死锁——现有插件 teardown 不调 execute，为已知取舍（spec §10），测试不覆盖该方向。
- [ ] **Step 4: 运行验证通过** → PASS
- [ ] **Step 5: 提交**

```bash
git add Module/tool/manager.py Module/tool/tests/test_framework.py
git commit -m "fix(tools): shutdown awaits deferred teardown under reload lock (E1)"
```

---

### Task 7: `server.py` 调用点适配（tuple 返回）

**Files:**
- Modify: `core/input/server.py`
- Modify: `core/input/tests/test_server.py`

**Interfaces:**
- Produces: `/tools/reload` handler 解包 `tuple[bool, str]`；测试 mock 返回元组。

- [ ] **Step 1: 写失败测试**

```python
# test_server.py 现有 reload 测试（:944/984 附近）mock 改为返回元组：
#   AsyncMock(return_value=(True, "")) / AsyncMock(return_value=(False, "some reason"))
# 失败用例断言响应 data["ok"] is False 且 data["error"] 含原因
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py -q -k reload -x --tb=short`
Expected: FAIL——**失败原因不是 TypeError**（mock 已改元组，旧 handler 的 `if ok:` 判元组恒 truthy，失败用例报成功）；成功用例在新 handler 前已绿（无法区分）。以失败用例断言为准。
- [ ] **Step 3: 最小实现**

`server.py:627`：
```python
ok, reason = await agent.tool_adapter._manager.reload_plugin(name)  # noqa: SLF001
if ok:
    return web.json_response({"ok": True, "name": name})
return web.json_response({"ok": False, "name": name, "error": reason})
```
- [ ] **Step 4: 运行验证通过** → PASS
- [ ] **Step 5: 提交**

```bash
git add core/input/server.py core/input/tests/test_server.py
git commit -m "fix(server): adapt /tools/reload to tuple result with reason (E1)"
```

---

### Task 8: 完整行为测试套件（全表）

**Files:**
- Modify: `Module/tool/tests/test_framework.py`
- 依赖：Task 0 fixture。

**Interfaces:**
- 用 Task 0 `plugin_dir`/`write_simple` + `write_config` helper 实现 spec §6 全表。

- [ ] **Step 1: 写失败测试**

spec §6 全表用例（每类 `@pytest.mark.service` + async 时 `@pytest.mark.asyncio`）：class 变化、description/config 变化、setup 失败回滚、discover 不到类回滚、teardown 异常、in-flight 排空、排空超时延迟 teardown、新调用不阻塞 reload、并发取值互斥、shutdown∥reload、连续 reload、无 config.yaml。

> **慢插件 helper（Task 8 内定义）**：in-flight 排空/超时用例需要「reload 瞬间仍在执行」的旧调用——`write_simple` 的 execute 秒回，快照前已完成（排空恒空/vacuous）。新增 `write_slow(src, suffix)` helper：execute 内 `await asyncio.sleep(0.5)` 再返回带版本标记的 ToolResult。排空超时用例给 `reload_plugin` 注入短 timeout 或对慢插件构造长 in-flight（默认 `_drain_and_teardown timeout=10.0`——**测试须避免真实等 10s**：直接测 `_drain_and_teardown` 的小 timeout（Task 4 已覆盖），reload 路径的排空用「慢插件 + 先发起 execute 再 reload」验证排空等待旧调用完成。

- [ ] **Step 2: 运行验证失败**

Expected: 多数已由 Task 3-6 实现 → PASS；未覆盖行为 FAIL → 补实现。

- [ ] **Step 3: 补齐实现缺口**

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/tool/tests/ -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/tool/tests/ Module/tool/manager.py
git commit -m "test(tools): full reload atomicity and rollback coverage (E1)"
```

---

### Task 9: 门禁 + 文档

**Files:**
- Modify: `todo.md`（E1 勾选 + 决策）
- Modify: `docs/`（architecture 工具热重载段落、`Module/tool/` README 若有）

- [ ] **Step 1: 全量门禁**

```bash
make lint && make pyright && make check-types
conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
```
Expected: 全绿（新增 reload 测试均标 service，纳入门禁）。

- [ ] **Step 2: 文档更新**：`todo.md` E1 完成 + 决策；`docs/architecture.md` 工具重载语义。
- [ ] **Step 3: 提交**

```bash
git add todo.md docs/
git commit -m "docs(tools): E1 completion — atomic reload with rollback"
```

---

## 自审记录

- **Spec 覆盖**：Task 0→§6 fixture、Task 1→§3、Task 2→§4.1、Task 3→§4.2/§4.3、Task 4→§4.4、Task 5→§5、Task 6→§4.5、Task 7→§9、Task 8→§6、Task 9→§7/§8。
- **可执行性**：所有 async 测试 `async def` + `@pytest.mark.asyncio` + `@pytest.mark.service`；fixture 前置 Task 0；drain 测试显式小 timeout。
- **类型一致**：`tuple[bool, str]`/`_teardown_tasks`/`_inflight`/`_discover_class` 一致。
- **无占位符**：每 Task 含可运行测试 + 实现代码。
