"""架构边界静态门禁（ARCH-INC-P1-03）。

背景见 `docs/review/2026-08-30-后端架构分层与模块化增量评估.md` §4：分层方向总体健康，
但边界主要靠**文档与人工 review** 维持——已治理过的反向依赖、私有接口泄漏会随功能提交
重新出现（上一轮清理的包级反向依赖就是这样回来的）。本脚本把这几条纪律固化为自动检查。

## 五条规则

| 规则 | 内容 | 存量基线 | 违规后果 |
|---|---|---|---|
| R1 | `Module/` 生产代码不得依赖 `core/` | 3 处（bench / `__main__`，见 allowlist） | 失败 |
| R2 | 领域/基础设施层（`core.agent`/`storage`/`memory`/`character`/`cognitive`/`personality`）不得反向依赖 `core.input` | **0 处** | 失败 |
| R3 | 禁止**跨包**导入下划线私有符号 | **0 处**（2026-09-11 批次 A 全部公开化） | 失败 |
| R4 | 不得新增 **eager** import 环（运行时硬环） | **0 处** | 失败 |
| R6 | 裸 `create_task`/`ensure_future` 必须逐点登记所有权 | 19 个登记点 / 22 处 spawn（见 `_TASK_SPAWN_ALLOWLIST`） | 失败 |

> **R5 已于 2026-09-17（Batch 2.1）删除**——它是**死规则**：`_type_only_edges()`
> 定义了却**从未被 `main()` 调用**（全仓 grep 只命中它自己的 `def`），因此它声称的
> "`TYPE_CHECKING` 类型边仅报告"在运行时**没有任何执行点**：不打印、不参与退出码、
> 无任何消费者。而模块顶部的规则表与 CLAUDE.md 都把它列为一条在跑的规则——
> 这正是本批要消灭的假门禁里最隐蔽的一种：**不是"永远绿"，而是"根本不存在却写在文档里"**
> （连"绿"都不用装）。实测其效果：一个纯 `TYPE_CHECKING` 的 `Module → core.agent` 边，
> 在 `make check-arch` 下**完全静默**（退出 0、无输出）。
>
> 处置：**整条删掉**（`_type_only_edges()` + `_Module.type_edges` 桶 + 三分表里的第三桶），
> 而不是"降级成 `--report` 开关"。理由：`TYPE_CHECKING` 边**允许存在是本项目的既定设计**
> （纯静态概念耦合，运行时不存在依赖），所以它本来就不该是一条门禁；而"保留一个默认关闭的
> 报告开关"只会留下一段无消费者的代码——那正是 R5 当初退化成死规则的方式。
> （2026-09-16 的架构审计 §S1/§733 已给出同一结论：「删除 `_type_only_edges()`
> 并同步删 `:15`/`:50` 的表格行」，且明确**反对**默认接线——"222 行『允许』报告会
> 训练人忽略门禁输出"。）
>
> 保留的是**「TYPE_CHECKING 不参与任何规则」这条语义**：`_collect_imports()` 现在直接
> 跳过 `if TYPE_CHECKING:` 块——判据仍在，只是不再有那桶无人读的数据。

`frontend_contract/` 的零依赖（不 import `core/`、`Module/`）由 R1 的同一条实现顺带覆盖
（见 `_DISCIPLINE_ZERO_DEP`），因为 `CLAUDE.md` 已把它写成硬约定却无门禁。

### R6：为什么登记而不是禁止（ARCH-INC-P2-03）

评估 §ARCH-INC-P2-03 的诉求不是「消灭 `asyncio.create_task`」——很多站点本来就是对的
（长期循环存字段、逐调用任务存集合、关机逐个 cancel + await）。它要的是**每处都能回答
四个问题**：任务存在哪里、谁取消、谁等待、异常谁看。所以 R6 的形态是 allowlist +
**期望条数**：

- 只做「是否出现过 `create_task`」的布尔判断 → 同一函数里再塞一个裸 spawn 会漏过；
- 记条数 → 新增（`count > expected`）与删除（`count < expected`）**都**报，
  清理后忘删条目也会被自己揪出来（陈旧登记单独报）。

新增后台任务应走 `Module/_common/task_scope.py` 的 `AsyncTaskScope`
（强引用 + done callback 统一记未处理异常 + `cancel_all`/`drain`/`aclose`），
它本身就是 R6 表里唯一被允许直接调用 `loop.create_task` 的实现点。

## eager / lazy / TYPE_CHECKING 的三分（关键）

同一个 import 在**不同规则**下的意义不同，因此分三桶统计：

- **eager**：模块体、**类体**、模块级 `if`/`try` 内的 import——**导入时真正执行**；
- **lazy**：`def` / `async def` **函数体内**的 import——不在导入时执行；
- **type**：`if TYPE_CHECKING:` 内的 import——仅类型检查可见。

| 规则 | 采用桶 | 理由 |
|---|---|---|
| R1 依赖方向（Module→core、零依赖层） | eager + lazy | 包级依赖是**结构事实**：`bench.py` 在函数内懒导入 `core.agent`，Module 依然依赖 core，不因延迟执行而消失 |
| R2 领域层→input | eager + lazy | 同上 |
| R3 跨包私有符号 | eager + lazy | 懒导入私有符号仍是私有接口泄漏 |
| R4 eager 环 | **仅 eager** | 函数内懒导入正是本项目**刻意**打断环的手段（见下） |
| TYPE_CHECKING 类型边 | 不参与任何规则 | 允许（既定设计）；`--report` 可见，**不是门禁** |

这条区分是必须的，两个方向都踩过：

- 若 R4 把函数内 import 也算 eager，会误报 2 个**伪环**——`core/memory/long_term.py` 用
  函数内懒导入打断与 `scoped_long_term` 的环并注释说明，`core/schema/types.py` 同理。
  （初版实现即如此误报。）
- 若 R1/R3 只算 eager，会**漏掉全部 5 处真实泄漏**——`bench.py`/`documentdb/__main__.py`
  的 core 依赖、`cognitive/tools.py` 的 `summarize_tool_arguments`、`personality/compression.py`
  的 `C6_L1_DEFAULT` 都在函数内。（初版实现即如此漏报。）

## 刻意不做的

- **不设文件行数硬门禁**（评估 §4 明确）：行数只作审查提示，职责与复杂度才是依据。
- 不自动修复、不重写 import——本脚本只报告。

## 用法

    python scripts/check_architecture.py            # 违规 → 打印并退出码 1
    python scripts/check_architecture.py --list     # 同时列出 allowlist 存量（评审用）
    python scripts/check_architecture.py --json     # 机器可读

可编程调用：`check_repository(root) -> list[Violation]`。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_SCAN_ROOTS = ("Module", "core", "frontend_contract")

# ---- R1：Module/ 生产代码依赖 core/ 的**显式豁免**（评估 §4 P1-03 要求 allowlist）----
_MODULE_TO_CORE_ALLOWLIST: dict[str, str] = {
    "Module/bench/bench.py": (
        "延迟基准工具（非产品运行时路径）；需构造真实 Agent 才能测端到端延迟。"
        "评估 §4 记为「需明确豁免或迁位」，此处取豁免"
    ),
    "Module/database/documentdb/__main__.py": (
        "`python -m` CLI 入口，非被 import 的库代码；运行时才取 core.utils.new_message_id"
    ),
}

# ---- R1 同族：零依赖层不得反向依赖 ----
_DISCIPLINE_ZERO_DEP: dict[str, tuple[str, ...]] = {
    # frontend_contract 是共享底座：Module/output 依赖它是**有意**的（CLAUDE.md），
    # 反向绝不允许——否则契约单源失效。
    "frontend_contract": ("core", "Module"),
}

# ---- R2：这些层不得反向依赖传输层 ----
_R2_FORBIDDEN_TARGET = ("core.input",)
_R2_SOURCES = (
    "core.agent",
    "core.storage",
    "core.memory",
    "core.character",
    "core.cognitive",
    "core.personality",
    "core.adapter",
    "core.prompt",
    "core.schema",
    "core.tool_runtime",
    "core.monitoring",
)

# ---- R3：跨包私有符号导入的**显式豁免** ----
#
# 2026-09-11（批次 A「配套 API 收口」）后**已清空**：原 6 个符号全部公开化——
#   `summarize_tool_arguments`        → `Module.tool.safety.summarize_tool_arguments`
#   `_ACTIVE_STATES`           → `core.storage.desktop_call_repo.is_active_desktop_call_state()`
#   `IN_PROGRESS_RETRY_AFTER_MS` → 同名公开常量
#   `_derive_title`            → `core.storage.session_repo.derive_session_title`
#   `C6_L1_DEFAULT`           → 同名公开常量
#   `resolve_image_source`    → 提取为 `core/schema/adapters/image_source.resolve_image_source`
# 新增条目必须附理由；空表意味着 R3 全量硬失败（这是期望状态，勿为通过而随意登记）。
_PRIVATE_SYMBOL_ALLOWLIST: dict[tuple[str, str], str] = {}

# ---- R6：裸后台任务创建点的**显式豁免**（ARCH-INC-P2-03）----
#
# key = (相对路径, 外层限定名)；value = (期望条数, 理由)。
#
# 「裸」= 直接 ``asyncio.create_task`` / ``ensure_future`` 而**不经**
# ``Module/_common/task_scope.py`` 的 ``AsyncTaskScope``。允许存在，但必须逐点
# 说明所有权：任务存在哪里、谁取消、谁等待、异常谁看。评估 §ARCH-INC-P2-03 要的
# 不是「消灭 create_task」，而是「每处都能回答这四个问题」——本表就是这份答案。
#
# 期望条数而非布尔：新增与删除都算漂移（详见 _collect_task_spawns）。
_TASK_SPAWN_METHODS = ("create_task", "ensure_future")
_TASK_SPAWN_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    # ── 原语自身：唯一被允许直接调用底层 API 的地方 ──
    ("Module/_common/task_scope.py", "AsyncTaskScope.spawn"):
        (1, "AsyncTaskScope 的实现本体——它就是收口点本身"),

    # ── Module/：所有权已在 2026-09-12 普查中逐点核实 ──
    ("Module/tool/manager.py", "ToolManager.execute"):
        (1, "_tasks[唯一 key] + _inflight 桶；本帧 await，finally cancel；shutdown 限时 drain"),
    ("Module/tool/manager.py", "ToolManager.execute_stream"):
        (1, "_tasks[唯一 key] + _inflight 桶；队列哨兵协议消费，finally cancel"),
    ("Module/tool/manager.py", "ToolManager._schedule_teardown"):
        (1, "_teardown_tasks 字典 + done callback；shutdown() 显式 gather（刻意不被 cancel_all）"),
    ("Module/workflow_engine/workflow_engine.py", "WorkflowEngine.start"):
        (1, "_queue_worker 字段；stop() cancel + gather(return_exceptions=True)"),
    ("Module/workflow_engine/workflow_engine.py", "WorkflowEngine._drain_queue"):
        (2, "_task_futures[task_id] 注册表；stop() wait→cancel→再 wait；逐任务 cancel 走 lifecycle"),
    ("Module/workflow_engine/workflow_engine.py", "WorkflowEngine.resume"):
        (1, "_task_futures[task_id]（快路径，同 _drain_queue）；spawn 失败显式回滚槽位"),
    # debug_api 的 _poll_loop 已于 2026-09-12 收口到 AsyncTaskScope（懒启动：首个
    # SSE 订阅者才拉起）——不再是裸 create_task，故此处条目已删除。R6 会因条数漂移
    # 报错正是提醒：收口后必须同步删掉 allowlist 条目。
    ("Module/workflow_engine/core/executor.py", "Executor.execute"):
        (1, "orphan_tools 集合 + 两个 done callback（discard / 取异常）；正常路径 shield await，stop() 兜底清扫"),
    ("Module/tts/gpt_sovits.py", "GPTSoVitsBackend._call_api"):
        (1, "局部变量；生成器 finally 内 cancel + await，synthesize() 必然耗尽生成器"),

    # ── core/：长期循环或逐调用任务，均有具名字段/集合接管 ──
    ("core/tool_runtime/scheduler.py", "ToolCallDispatcher.start"):
        (2, "_workers 列表 + _prune_task 字段；stop() 逐个 cancel + gather"),
    ("core/monitoring/idle_monitor.py", "IdleMonitor.start"):
        (1, "_task 字段；stop() cancel + await"),
    ("core/memory/embedding_provider.py", "EmbeddingProvider.ensure"):
        (1, "_task 字段；ensure(wait=True) 内 await，LongTermMemory.close() cancel"),
    ("core/memory/session.py", "SessionMemory.add_message"):
        (1, "_archive_tasks 集合 + done callback；close_archive 限时 wait→cancel→再 wait"),
    ("core/adapter/tts_adapter.py", "TTSStreamManager.stream_sentence"):
        (1, "_tasks 列表 + _task_by_sid 索引；wait_all / cancel_all / cancel_stream 接管"),
    ("core/input/ws_handler.py", "_open_ws_connection"):
        (1, "_WsConnection.monitor_task 字段；_close_ws_connection cancel"),
    ("core/input/voice_sessions.py", "VoiceSessionManager.start_realtime"):
        (2, "_ack_task / _idle_task 字段；_teardown_session cancel"),
    ("core/input/voice_sessions.py", "VoiceSessionManager.stop_recording"):
        (1, "_in_flight_stt 字段；_reset_recording / discard_recording / close cancel"),
    ("core/input/voice_sessions.py", "VoiceSessionManager._make_dispatch_stt._dispatch"):
        (1, "_stt_tasks 集合；teardown 批量 cancel + gather drain"),
    # #20b（2026-09-16）：`CognitiveFinalize._archive_final_turn` 的裸 create_task 已收口到
    # `CognitiveLoop._after_response_scope`（`AsyncTaskScope.spawn`），故条目删除——
    # 收口一项就删一条，否则 R6 会因条数漂移报"陈旧登记"。
    ("core/personality/engine.py", "CharacterEngine.buffer_message"):
        (1, "返回给调用方（CognitiveLoop 经 `_after_response_scope.adopt` 纳入同一收尾域）"),
}


@dataclass
class Violation:
    """一条边界违规。"""

    rule: str
    location: str
    detail: str
    accepted: bool = False
    """True = 命中 allowlist 的存量项（不导致失败，仅 --list 时展示）。"""

    def render(self) -> str:
        mark = "✔ 已豁免" if self.accepted else "✘ 违规"
        return f"  [{self.rule}] {mark} {self.location}\n        {self.detail}"


@dataclass
class _Module:
    path: Path
    name: str
    eager: list[tuple[str, list[str]]] = field(default_factory=list)
    lazy: list[tuple[str, list[str]]] = field(default_factory=list)
    task_spawns: Counter[str] = field(default_factory=Counter)
    """R6：本文件按「限定名」计数的裸后台任务创建点（见 _collect_task_spawns）。

    ``Counter[str]`` 而非 list——allowlist 记录的是**期望条数**，加/删各算漂移。
    """

    @property
    def runtime(self) -> list[tuple[str, list[str]]]:
        """运行时依赖 = eager + lazy（R1/R2/R3 用；TYPE_CHECKING 边不算）。"""
        return self.eager + self.lazy


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def _module_name(path: Path) -> str:
    return str(path.with_suffix("")).replace("/", ".")


def _is_production(path: Path) -> bool:
    """生产代码 = 排除 tests/、test_*.py、conftest.py、__pycache__。"""
    s = str(path)
    if "__pycache__" in s:
        return False
    if "/tests/" in s or path.name.startswith("test_") or path.name == "conftest.py":
        return False
    return True


def _collect_imports(
    tree: ast.Module,
) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    """返回 (eager, lazy) 两桶。

    eager：模块体 / 类体 / 模块级 if|try 内的 import——导入时真正执行。
    lazy：函数体内的 import——不在导入时执行（本项目刻意用它打断环）。

    **`if TYPE_CHECKING:` 内的 import 整个跳过**（既不入 eager 也不入 lazy）。
    这就是"类型边不参与任何规则"的实现方式——本项目**刻意允许**类型边存在
    （纯静态概念耦合，运行时不存在依赖），故不检测、不报告。
    历史：这里原本还有第三桶 `type_edges` 供"R5 仅报告"使用，但 `main()` 从未
    调用报告函数 ⇒ 该桶无任何消费者、纯死代码。2026-09-17（Batch 2.1）连同
    `_type_only_edges()` 一并删除——见模块 docstring 的 R5 说明。
    """
    eager: list[tuple[str, list[str]]] = []
    lazy: list[tuple[str, list[str]]] = []

    def walk(body: list[ast.stmt], tc: bool, in_func: bool) -> None:
        for node in body:
            if isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue  # 相对导入由 ruff TID252 负责
                if tc:
                    continue  # TYPE_CHECKING 类型边：刻意不检测
                bucket = lazy if in_func else eager
                bucket.append((node.module, [a.name for a in node.names]))
            elif isinstance(node, ast.Import):
                if not tc:
                    bucket = lazy if in_func else eager
                    for alias in node.names:
                        bucket.append((alias.name, []))
            elif isinstance(node, ast.If):
                test = node.test
                is_tc = (
                    isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
                ) or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
                walk(node.body, tc or is_tc, in_func)
                walk(node.orelse, tc, in_func)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, tc, in_func)  # 类体在导入时执行
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(node.body, tc, True)  # 函数体：lazy
            elif isinstance(node, ast.Try):
                walk(node.body, tc, in_func)
                walk(node.orelse, tc, in_func)
                walk(node.finalbody, tc, in_func)
                for handler in node.handlers:
                    walk(handler.body, tc, in_func)

    walk(tree.body, False, False)
    return eager, lazy


def _collect_task_spawns(tree: ast.Module) -> Counter[str]:
    """R6：收集裸后台任务创建点，按「外层限定名」计数（ARCH-INC-P2-03）。

    识别 ``<任意表达式>.create_task(...)`` 与 ``.ensure_future(...)``——覆盖
    ``asyncio.create_task``、``asyncio.ensure_future``、
    ``asyncio.get_running_loop().create_task``、``loop.create_task`` 全部写法。

    限定名取「类.方法」链（模块级为 ``<module>``）。计数而非仅记录存在性：
    allowlist 声明期望条数，**新增**与**删除**都算漂移——既堵住「同一函数里再塞
    一个裸 spawn」，也让清理后的陈旧条目自动暴露。
    """
    stack: list[str] = []
    counts: Counter[str] = Counter()

    class _Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _TASK_SPAWN_METHODS:
                counts[".".join(stack) or "<module>"] += 1
            self.generic_visit(node)

    _Visitor().visit(tree)
    return counts


def _scan(root: Path) -> tuple[list[_Module], set[str]]:
    """扫描三个顶层包，返回 (生产模块列表, 全部已知模块名)。"""
    modules: list[_Module] = []
    known: set[str] = set()

    for scan_root in _SCAN_ROOTS:
        base = root / scan_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            known.add(_module_name(path.relative_to(root)))
            if not _is_production(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as e:  # pragma: no cover - 语法错误应由 lint 先拦
                raise SystemExit(f"解析失败 {path}: {e}") from e
            eager, lazy = _collect_imports(tree)
            modules.append(
                _Module(
                    path, _module_name(path.relative_to(root)),
                    eager, lazy,
                    task_spawns=_collect_task_spawns(tree),
                )
            )

    # 补包名（__init__ 未显式出现时的父包）
    for name in list(known):
        parts = name.split(".")
        for i in range(1, len(parts)):
            known.add(".".join(parts[:i]))
    return modules, known


def _resolve(dep: str, known: set[str]) -> str | None:
    """把 import 目标解析为仓库内部模块（最长前缀匹配）；外部依赖返回 None。"""
    if dep in known:
        return dep
    parts = dep.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in known:
            return candidate
    return None


def _top_pkg(dotted: str) -> str:
    return dotted.split(".")[0]


def _sub_pkg(dotted: str) -> str:
    parts = dotted.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


def _find_eager_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 求强连通分量，返回节点数 >1 的环。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.append(sorted(comp))

    for v in list(graph):
        if v not in index:
            strong(v)
    return cycles


def check_repository(root: Path | None = None) -> list[Violation]:
    """执行全部规则，返回违规列表（含 accepted=True 的存量豁免项）。

    Args:
        root: 仓库根；默认本脚本所在仓库。

    Returns:
        Violation 列表；仅 `accepted=False` 的项表示需要处理。
    """
    root = root or _ROOT
    modules, known = _scan(root)
    violations: list[Violation] = []

    for mod in modules:
        rel = _rel(mod.path) if root == _ROOT else str(mod.path.relative_to(root))
        top = _top_pkg(mod.name)

        # R1/R2/R3 用运行时依赖（eager + lazy）；见模块 docstring「三分」表
        for target, names in mod.runtime:
            resolved = _resolve(target, known)

            # ---- R1: Module/ → core/ ----
            if top == "Module" and _top_pkg(target) == "core":
                reason = _MODULE_TO_CORE_ALLOWLIST.get(rel)
                violations.append(Violation(
                    "R1", f"{rel} → {target}",
                    reason or "Module/ 生产代码不得依赖 core/（评估 §4 P1-03 规则 1）",
                    accepted=reason is not None,
                ))

            # ---- R1 同族: 零依赖层反向依赖 ----
            for zero_dep, forbidden in _DISCIPLINE_ZERO_DEP.items():
                if top != zero_dep:
                    continue
                if _top_pkg(target) not in forbidden:
                    continue
                violations.append(Violation(
                    "R1", f"{rel} → {target}",
                    f"{zero_dep}/ 是共享底座，不得反向依赖 {'/'.join(forbidden)}/"
                    "（CLAUDE.md 硬约定：零依赖）",
                ))

            # ---- R2: 领域/基础层 → core.input ----
            if any(mod.name == s or mod.name.startswith(s + ".") for s in _R2_SOURCES):
                if any(target == f or target.startswith(f + ".") for f in _R2_FORBIDDEN_TARGET):
                    violations.append(Violation(
                        "R2", f"{rel} → {target}",
                        "领域/基础设施层不得反向依赖传输层 core.input（评估 §4 P1-03 规则 2）",
                    ))

            # ---- R3: 跨包私有符号 ----
            for name in names:
                if not name.startswith("_") or name.startswith("__"):
                    continue
                if _sub_pkg(target) == _sub_pkg(mod.name):
                    continue  # 同一子包内（含 _common/_utils）按评估 §4 规则 3 放行
                key = (target, name)
                reason = _PRIVATE_SYMBOL_ALLOWLIST.get(key)
                violations.append(Violation(
                    "R3", f"{rel} → {target}.{name}",
                    reason or "禁止跨包导入下划线私有符号（评估 §4 P1-03 规则 3）",
                    accepted=reason is not None,
                ))

    # ---- R4: eager 环（建图只用 eager——函数内懒导入是刻意打断环的手段）----
    eager_graph: dict[str, set[str]] = defaultdict(set)
    for mod in modules:
        for target, _names in mod.eager:
            resolved = _resolve(target, known)
            if resolved and resolved != mod.name:
                eager_graph[mod.name].add(resolved)

    for cycle in _find_eager_cycles(dict(eager_graph)):
        violations.append(Violation(
            "R4", " → ".join(cycle) + f" → {cycle[0]}",
            f"新增 eager import 环（{len(cycle)} 节点）；如需类型边请用 TYPE_CHECKING，"
            "如需运行时解耦请用函数内懒导入（评估 §4 P1-03 规则 4）",
        ))

    # ---- R6: 裸后台任务创建点必须逐点登记所有权（ARCH-INC-P2-03）----
    # allowlist 描述的是**本仓库**的存量站点，因此只对真实仓库生效；合成 fixture
    # 仓库（测试用）里一切都是「未登记」，不套用也不做陈旧反查。
    is_real_repo = root == _ROOT
    seen_spawns: set[tuple[str, str]] = set()
    for mod in modules:
        rel = _rel(mod.path) if is_real_repo else str(mod.path.relative_to(root))
        for qualname, count in sorted(mod.task_spawns.items()):
            key = (rel, qualname)
            seen_spawns.add(key)
            entry = _TASK_SPAWN_ALLOWLIST.get(key) if is_real_repo else None
            if entry is None:
                violations.append(Violation(
                    "R6", f"{rel}::{qualname}（{count} 处）",
                    "裸后台任务创建点未登记。优先改用 Module/_common/task_scope.py 的 "
                    "AsyncTaskScope（强引用 + 异常观察 + cancel/drain），确需裸用则在 "
                    "_TASK_SPAWN_ALLOWLIST 登记「任务存哪 / 谁取消 / 谁等待 / 异常谁看」"
                    "（评估 §ARCH-INC-P2-03）",
                ))
            elif entry[0] != count:
                violations.append(Violation(
                    "R6", f"{rel}::{qualname}（登记 {entry[0]} 处，实到 {count} 处）",
                    f"裸 create_task 条数漂移——{entry[1]}。"
                    "新增请改用 AsyncTaskScope；删除请同步更新登记条数",
                ))

    if is_real_repo:
        for (rel, qualname), (expected, reason) in sorted(_TASK_SPAWN_ALLOWLIST.items()):
            if (rel, qualname) not in seen_spawns:
                violations.append(Violation(
                    "R6", f"{rel}::{qualname}（登记 {expected} 处，实际 0 处）",
                    f"陈旧登记：该创建点已不存在或已改经 AsyncTaskScope，请删除此条（原理由：{reason}）",
                ))

    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：违规 → 退出码 1。"""
    parser = argparse.ArgumentParser(description="架构边界静态门禁（ARCH-INC-P1-03）")
    parser.add_argument("--list", action="store_true", help="同时列出 allowlist 存量豁免项")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--root", default=None, help="仓库根（默认脚本所在仓库）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _ROOT
    violations = check_repository(root)
    hard = [v for v in violations if not v.accepted]
    accepted = [v for v in violations if v.accepted]

    if args.json:
        print(json.dumps({
            "violations": [vars(v) for v in hard],
            "accepted": [vars(v) for v in accepted],
            "ok": not hard,
        }, ensure_ascii=False, indent=2))
        return 1 if hard else 0

    if hard:
        print(f"架构边界违规 {len(hard)} 项：")
        for v in hard:
            print(v.render())
        print("\n修复或（确属有意）在 scripts/check_architecture.py 的 allowlist 中登记理由。")
        return 1

    print("架构边界检查通过：R1 Module→core / R2 领域层→input / R3 跨包私有符号 / R4 eager 环 / R6 裸后台任务登记")
    if args.list:
        print(f"\n存量豁免（allowlist，{len(accepted)} 项）：")
        for v in accepted:
            print(v.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
