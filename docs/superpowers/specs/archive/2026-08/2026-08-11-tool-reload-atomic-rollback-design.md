# 工具热重载原子替换与失败回滚设计（E1）

> **状态**：设计定稿（2026-08-11）
> **对应**：P2-06（`todo.md` E1）、`docs/superpowers/plans/archive/2026-08/2026-08-10-full-project-remediation-plan.md` Task E1
> **范围**：`Module/tool/manager.py`（`reload_plugin` / `load_all` / `execute` / `execute_stream` / `shutdown`）+ `Module/tool/tests/test_framework.py` + `core/input/server.py` + `core/input/tests/test_server.py`

---

## 1. 问题（P2-06）

`ToolManager.reload_plugin`（`Module/tool/manager.py:484-546`）存在缺陷：

1. **失败无回滚**：先 `await old.teardown()` 并 `self._plugins.pop(name)`（:501-506）——若新实例 setup 失败，**旧工具永久消失**。
2. **读旧 class**：`cls = self._plugin_classes.get(name)`（:511）——`importlib.reload(module)` 后该字典仍是旧 class 引用；即使模块暴露新 class，注册的仍是旧实现。:513-522 的 fallback 扫描只在 `_plugin_classes.get(name)` 为空时才执行，正常路径读不到新 class。
3. **非原子**：reload 逐步赋值 `_plugins`/`_semaphores`/`_plugin_classes`/`_plugin_modules`，与并发 `execute` 之间无互斥——调用方可能观察到混合状态。
4. **无 in-flight 排空**：reload 路径对旧实例先 teardown（:501）再 reload——替换瞬间已在执行的旧实例调用未等待即被 teardown（缺陷根因：**先 teardown 后 reload**，与「替换后排空再 teardown」的正确顺序相反）。

## 2. 决策

**原子替换 + 失败回滚**（计划推荐）：

- reload 后从**新 module 重新 discover class**（不再读旧 `_plugin_classes`）。
- 正常 load / reload **共用同一 config.yaml 解析、SafetyProfile 覆盖与 setup 流程**。
- 先在**隔离位置**构造/setup 新实例；成功后**原子替换**注册表；再等旧实例 in-flight 调用排空并 teardown。
- 新 setup 失败时**保留旧实例、旧 schema 和可用状态**；返回明确失败原因。
- reload 与新 execute 互斥/版本化，调用只能看到**完整旧版或完整新版**。

## 3. 共用 config 加载 helper

`load_all` 内联的 config.yaml → SafetyProfile 覆盖逻辑提取为私有方法，load 与 reload 共用：

```python
def _load_safety_config(self, instance: ToolPlugin, cls: type[ToolPlugin]) -> None:
    """从插件目录加载 config.yaml 覆盖 SafetyProfile 字段（load/reload 共用）。"""
    module_file = inspect.getfile(cls)
    plugin_dir = Path(module_file).parent
    config_file = plugin_dir / "config.yaml"
    if not config_file.is_file():
        return
    try:
        raw = yaml.safe_load(config_file.read_text("utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if hasattr(instance.safety, key):
                    setattr(instance.safety, key, value)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("工具 '%s' config.yaml 加载失败: %s", cls.__name__, e)
```

`load_all` 改为调用该 helper（行为不变，去重）。

## 4. 并发控制与原子替换

### 4.1 数据结构（`ToolManager.__init__` 新增）

```python
self._reload_lock: asyncio.Lock = asyncio.Lock()          # 全局互斥：仅 swap 与 shutdown 期间持锁
self._inflight: dict[str, set[asyncio.Task]] = {}          # plugin name → 进行中 execute/execute_stream 任务
self._teardown_tasks: dict[str, asyncio.Task] = {}         # 延迟 teardown 任务（独立于 _tasks，不被 cancel_all 取消）
```

- **互斥范围**：`_reload_lock` **只在原子替换的瞬时窗口持锁**（§4.2 步骤 3，微秒级），**不覆盖排空/teardown**——其他工具（乃至同工具的后续新调用）不被阻塞最多 10s。
  - 正确性依据：swap（四个 dict 赋值）与 execute 取值段（`_plugins.get` + `_semaphores.get`）都是**无 await 的同步块**——asyncio 协作式调度下不会在块内切换，读/写天然不交错。锁是显式的互斥声明（计划要求「加锁/版本化」），不是正确性的唯一保证。
- **in-flight 按实例快照**：`_inflight[name]` 记录进行中任务；swap 时**快照旧任务** `old_tasks = set(self._inflight.get(name, ()))`，排空只等 `old_tasks`。替换后新调用加入 `_inflight[name]` 但**不在快照内**——排空不受新调用干扰，必然收敛（修复「同名计数永不归零」）。

### 4.2 reload 流程

```python
async def reload_plugin(self, name: str) -> tuple[bool, str]:
    old = self._plugins.get(name)
    module_path = self._plugin_modules.get(name)
    if old is None or module_path is None:
        return False, f"工具 '{name}' 不存在或无法定位模块"

    # 1. 隔离构造 + 共用 config + setup（锁外——慢 I/O 不阻塞其他工具；计划「隔离构造」）
    try:
        module = importlib.import_module(module_path)
        module = importlib.reload(module)

        # 2. 从新 module 重新 discover class（不读 _plugin_classes）
        new_cls = self._discover_class(module, name)
        if new_cls is None:
            return False, f"模块 '{module_path}' 中未找到名为 '{name}' 的插件类"

        new_instance = new_cls()
        self._load_safety_config(new_instance, new_cls)
        if self._plugin_context is None:
            return False, "PluginContext 未初始化，请先调用 load_all()"
        await new_instance.setup(self._plugin_context)
    except Exception as e:
        logger.error("工具 '%s' 热重载失败: %s", name, e)
        return False, f"热重载失败: {e}"

    # 3. 持锁做身份复核 + 原子替换（微秒窗口；setup 期间并发 reload/shutdown 可能已改动注册表）
    superseded = False
    async with self._reload_lock:
        if self._plugins.get(name) is not old:
            superseded = True                        # 被并发替换/清除——不覆盖他人结果
        else:
            new_sem = None
            if new_instance.safety and new_instance.safety.max_concurrency > 0:
                new_sem = asyncio.Semaphore(new_instance.safety.max_concurrency)
            old_tasks = set(self._inflight.get(name, ()))   # 快照：排空目标 = 替换前已在执行的任务
            self._plugins[name] = new_instance
            self._plugin_classes[name] = new_cls
            self._plugin_modules[name] = module_path
            if new_sem is not None:
                self._semaphores[name] = new_sem
            else:
                self._semaphores.pop(name, None)

    if superseded:
        # 弃用新实例——锁外 teardown（慢 I/O 不进微秒锁窗口）+ try/except（异常不逸出端点）
        try:
            await new_instance.teardown()
        except Exception as e:
            logger.warning("工具 '%s' 并发覆盖后新实例 teardown 异常: %s", name, e)
        return False, f"工具 '{name}' 在 reload 期间被并发修改"

    # 4. 排空旧实例 in-flight + teardown（锁已释放——新调用立即走新实例，不受阻塞）
    await self._drain_and_teardown(name, old, old_tasks)
    logger.info("工具 '%s' 已热重载", name)
    return True, ""
```

- **构造/setup 在锁外、swap 在锁内**：`importlib.reload` + 构造 + `await setup()` 在锁外执行——慢 I/O（网络初始化等）不阻塞其他工具，setup 内 re-enter `execute()` 也不死锁。锁只包步骤 3（身份复核 + 原子替换，微秒）。排空/teardown 在锁外执行——新调用（含同名）立即走新实例，`_inflight` 不再计入快照，排空必然收敛。
- **身份复核防并发覆盖**：setup 期间并发 reload/shutdown 可能已替换/清除 `_plugins[name]`——swap 前持锁复核 `self._plugins.get(name) is old`，不等则 teardown 新实例并返回失败（不覆盖他人结果）。
- **失败回滚**：步骤 1/2 任何异常（discover 不到类、config 解析错、setup 抛）都走 except，注册表四个字典**零改动**。

### 4.3 `_discover_class`

```python
@staticmethod
def _discover_class(module, name: str) -> type[ToolPlugin] | None:
    for attr in dir(module):
        obj = getattr(module, attr)
        if (isinstance(obj, type)
                and issubclass(obj, ToolPlugin)
                and obj is not ToolPlugin
                and getattr(obj, "name", "") == name):
            return obj
    return None
```

（现 :513-522 fallback 扫描提取复用；**删除**「读 `_plugin_classes.get(name)`」主路径。）

### 4.4 in-flight 排空与延迟 teardown

```python
async def _drain_and_teardown(
    self, name: str, old: ToolPlugin, old_tasks: set[asyncio.Task], timeout: float = 10.0,
) -> None:
    """等旧实例 in-flight 归零后 teardown。超时转后台延迟 teardown，不阻塞 reload。"""
    if old_tasks:
        done, pending = await asyncio.wait(old_tasks, timeout=timeout)
        if pending:
            logger.warning(
                "工具 '%s' 热重载：%d 个 in-flight 未在 %.0fs 内排空，延迟 teardown",
                name, len(pending), timeout,
            )
            self._schedule_teardown(name, old)
            return
    try:
        await old.teardown()
    except Exception as e:
        logger.warning("旧插件 '%s' teardown() 异常: %s", name, e)

def _schedule_teardown(self, name: str, old: ToolPlugin) -> None:
    """后台延迟 teardown——登记到 _teardown_tasks + done 回调收异常，防『Task exception was never retrieved』。"""
    task = asyncio.create_task(old.teardown())
    self._teardown_tasks[f"{name}:teardown:{id(old)}"] = task   # 独立登记——不被 cancel_all 取消

    def _on_done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("延迟 teardown 工具 '%s' 异常: %s", name, e)
        finally:
            self._teardown_tasks.pop(f"{name}:teardown:{id(old)}", None)

    task.add_done_callback(_on_done)
```

- 替换先于排空：新调用立即走新实例；排空只等 `old_tasks` 快照（替换瞬间已在执行的旧实例调用）。
- **`_teardown_tasks` 独立登记**：延迟 teardown 任务放独立 dict（非 `_tasks`）——`cancel_all()`（ToolExecutor 公开协议）只取消 `_tasks`，不误杀延迟 teardown 导致旧实例资源泄漏（http_client/DB 连接）。`shutdown()` 显式 `await asyncio.gather(*self._teardown_tasks.values(), return_exceptions=True)` 收尾。

### 4.5 shutdown / cancel_all 并发

```python
async def shutdown(self) -> None:
    async with self._reload_lock:        # 与 reload 互斥，防 double-teardown / 半替换状态
        await self.cancel_all()
        teardowns = [p.teardown() for p in self._plugins.values()]
        results = await asyncio.gather(*teardowns, return_exceptions=True)
        for name, result in zip(self._plugins.keys(), results):
            if isinstance(result, Exception):
                logger.warning("工具 '%s' teardown() 异常: %s", name, result)
        self._plugins.clear()
        # 收尾延迟 teardown 任务（reload 排空超时登记）——不被 cancel_all 取消，此处显式等待
        if self._teardown_tasks:
            await asyncio.gather(*self._teardown_tasks.values(), return_exceptions=True)
            self._teardown_tasks.clear()
    logger.info("ToolManager 已关闭")
```

- `shutdown` 持 `_reload_lock`：reload 进行中时 shutdown 等待 swap 完成（微秒级）或排空（锁外）；shutdown 结束后 reload 的延迟 teardown 任务在 `_teardown_tasks`（非 `_tasks`、不被 `cancel_all` 取消），shutdown 在锁内显式 `gather` 等待其完成——防事件循环关闭时旧实例 http_client/DB 清理中断。

## 5. execute / execute_stream 改造

```python
async def execute(self, name, *, ctx, **raw_params):
    async with self._reload_lock:                    # 与 swap 互斥（微秒级，无 await）
        plugin = self._plugins.get(name)
        if plugin is None:
            return self._unknown_tool_result(name)
        sem = self._semaphores.get(name)
    self._executing.add(name)                        # 保留——debug executing_now 依赖
    task = asyncio.create_task(self._pipeline.execute(plugin, name, ctx, raw_params, sem))
    self._tasks[ctx.call_id or name] = task
    self._inflight.setdefault(name, set()).add(task) # 供 reload 排空快照
    try:
        result = await task
        self._record(result)
        return result
    finally:
        self._executing.discard(name)
        self._inflight.setdefault(name, set()).discard(task)
        self._tasks.pop(ctx.call_id or name, None)
```

`execute_stream` 同样处理（取值段 `_reload_lock` + `_executing.add` + `_inflight` 计数）。

## 6. 测试计划（TDD，`Module/tool/tests/test_framework.py`）

**测试驱动机制（真实 `importlib.reload` 语义）**：fixture 在 `tmp_path` 写插件模块文件，加入 `sys.path` 后按名 import（`reload_plugin` 用 `importlib.import_module(module_path)`，模块须可按点分名 import）。**改 class / 抛 setup 异常 / 改 config.yaml 都通过改写磁盘文件 + `reload_plugin` 触发真实 reload**（monkeypatch 模块属性在 `importlib.reload` 时会被磁盘重执行抹掉，不可用）。**必须清理 `sys.modules`**：`importlib.reload` 按模块缓存里的 `__file__`（上一测试的 tmp_path）重执行——跨测试复用 `reload_plugins.simple` 名时，若不清 `sys.modules`，当前测试改写的新文件不会被读到（读到陈旧实现）。

```python
@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    src = tmp_path / "reload_plugins"
    src.mkdir()
    (src / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))      # importlib.import_module("reload_plugins.simple") 可解析
    # setup 前弹掉旧缓存——防跨测试读到上一测试的 __file__/实现
    for m in list(sys.modules):
        if m == "reload_plugins" or m.startswith("reload_plugins."):
            del sys.modules[m]
    yield src
    for m in list(sys.modules):                     # teardown 清缓存——下一个测试从自己 tmp_path 重新 import
        if m == "reload_plugins" or m.startswith("reload_plugins."):
            del sys.modules[m]

def write_simple(src, *, setup_raises=False, suffix="v1"):
    body = f'''from Module.tool.protocol import ToolPlugin, SafetyProfile, ToolResult, ToolContext
class SimplePlugin(ToolPlugin):
    name = "simple"
    description = "desc-{suffix}"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()
    {"def setup(self, ctx): raise RuntimeError('boom')" if setup_raises else ""}
    async def execute(self, ctx: ToolContext, **params):
        # execute 返回带版本标记的 ToolResult——reload 后用 execute 结果区分 v1/v2
        return ToolResult.ok(name="simple", content="exec-{suffix}", duration_ms=0.0)
'''
    (src / "simple.py").write_text(body)
```

| 用例 | 断言 |
|---|---|
| class 变化生效 | 改写 `simple.py` 换实现（`suffix="v2"`），reload → `execute` 返回 `content == "exec-v2"`（新 class 行为）；`_plugin_classes["simple"]` 为新 class |
| description/config 变化生效 | 改写 `config.yaml`（覆盖 SafetyProfile 字段），reload → metadata/`plugin_list` 反映新值 |
| setup 失败回滚 | 改写 `simple.py` 使 `setup()` 抛 → reload 返回 `(False, 含原因)`、`_plugins["simple"]` 仍为旧实例、`execute` 仍返回 `"exec-v1"` |
| discover 不到类回滚 | 改写模块删除/改名类 → reload 返回 False，注册表未动 |
| teardown 异常 | 旧 teardown 抛异常 → 替换已成功、返回 True、warning 记录 |
| in-flight 排空 | 替换时进行中调用用旧实例跑完；之后新调用用新实例；`old_tasks` 归零后旧实例 teardown 被调 |
| 排空超时延迟 teardown | mock 旧任务永不 done → 后台延迟 teardown（`_teardown_tasks` 登记 + done 回调收异常，不被 cancel_all 取消），reload 不阻塞 |
| 新调用不阻塞 reload | reload 排空期间（锁已释放）execute 立即用新实例执行，不等排空 |
| 并发取值互斥 | reload swap 期间 execute 读到的 plugin/sem 同属旧版或新版（不混合） |
| shutdown∥reload | shutdown 与 reload 并发不 double-teardown、不抛异常 |
| 连续 reload | 连续两次 reload 均成功，第二次基于最新 module |
| 目录无 config.yaml | helper 跳过，safety 用 class 默认 |

## 7. 验收标准

1. reload 后新调用确实使用新 class/config。
2. 失败时旧工具仍可用（注册表未动）。
3. 调用只见完整旧版或完整新版（无混合状态）。
4. reload 不阻塞其他工具 / 同工具后续新调用（锁仅覆盖 swap 微秒窗口）。
5. 全部门禁绿：`make test-quick` / pyright / ruff / contract drift。

## 8. 待记录决策

- **锁仅覆盖原子替换**（微秒级），排空在锁外——reload 不阻塞整个工具子系统；swap 与 execute 取值段均无 await，协作式调度下天然原子，锁为显式互斥声明。
- **in-flight 按实例快照**（swap 时快照 `old_tasks`），排空只等旧任务——同名新调用不干扰排空收敛。
- **排空超时 10s，超时后台延迟 teardown**（`_teardown_tasks` 登记 + done 回调收异常；不被 cancel_all 取消，shutdown 显式 gather 收尾）。
- **reload 返回 `(bool, str)`**（成功/失败 + 原因）——调用点 `server.py:627` 必须同步改（见 §9）。

## 9. 调用方适配

**唯一调用点**：`core/input/server.py:627`（`/tools/reload` handler；`api_routes.py` 无调用）。

```python
# 现：ok = await agent.tool_adapter._manager.reload_plugin(name)
#     if ok: ... else: {"ok": False, "error": f"插件 '{name}' 重载失败"}
# 改：
ok, reason = await agent.tool_adapter._manager.reload_plugin(name)
if ok:
    return web.json_response({"ok": True, "name": name})
return web.json_response({"ok": False, "name": name, "error": reason})
```

- **必须解包**：`reload_plugin` 返回 `tuple[bool, str]` 后，`if ok:` 判元组恒 truthy——不解包则重载失败也报成功。
- **测试适配**：`core/input/tests/test_server.py:944/984` 的 `AsyncMock(return_value=True/False)` 改为返回 `(True, "")` / `(False, "reason")`；`Module/tool/tests/test_framework.py` 现有 `is False`/`is True` 断言改为元组。

## 10. 已知取舍（记录在案）

- **回滚非绝对**：`importlib.reload` 会重执行模块文件、重置模块级全局。若旧实例依赖模块级可变状态（如计数器/缓存），setup 失败回滚后旧实例可能读到被清零的全局，行为与 reload 前不同。`Module/tool/protocol.py:397` 约定「插件模块级无状态」——此为插件作者约定，不强制；本 spec 按约定接受该边界。
