"""并行安全守卫 —— 让「这些测试不许 `-n`」从文档约定变成机器不变量。

## 为什么需要

2026-09-27 测试隔离专项。仓里有两层测试**共用一份真实资源**，天然不能并行：

| 层 | 共用资源 | 为什么会互相破坏 |
|----|---------|-----------------|
| `tests/real_mongo/` | 一个**真实 MongoDB 测试库** | 每个用例前后 `drop_database`；身份/会话断言基于固定库状态 |
| `slow`/`integration`/`external` 中的真实 Milvus 用例 | `long_term_memory_v2__test` 集合 | `TestDebugDbApi` 会 drop + recreate 它，`TestMemoryIntegration` 做 `count+1` / 召回断言 |

在此之前这两条约束**只写在 Makefile 注释与文档里**。而 `pytest -n` 是随手就能加的：
`make test-quick` 的默认档位就是 `-n 8`，`PYTEST_N` 还能往上调。于是
「大家记得别加」成了唯一防线——这不是机制。

**实测证据**（本轮复现，不是推断）：把真实 Milvus 用例按文件分给 4 个 worker
（`--dist loadfile -n 4`）连跑 3 次，第 3 次 `test_search_memory_tool` 以
`ToolResult(success=False, error='执行超时 (30.0s)')` 失败——另一个 worker 的
`test_ltm_clear_all` 把集合 drop 掉了。

## 判据挂在 `pytest_runtest_setup`（每条用例执行前）

挂在 setup 而不是 collection 阶段，是因为**只有 setup 才既精确又干净**。本批实测
把三条路都试过一遍，结论记在这里，免得下次有人再走：

| 做法 | 为什么不行 |
|------|-----------|
| 根 conftest 的 `pytest_collection_modifyitems` | 拿到的是 `-m`/`-k` 过滤**之前**的全量（本仓既有实测结论）。`make test-quick -n 8` 会因为一堆**根本不会被执行**的 slow/external 用例整轮变红 |
| `tests/real_mongo/conftest.py::pytest_configure` | conftest 只要被**加载**该 hook 就跑，而 xdist worker 在收集期会加载 `tests/` 下所有 conftest。实测 `pytest -m "unit or service" -n 2 tests/` 直接变成 `ERROR tests/real_mongo - pytest.UsageError`——判据装在"加载"上，不是"选中"上 |
| `pytest_collection_finish` + `pytest.UsageError` / `pytest.exit` | 选择集是对的，但 worker 里**任何**非测试失败的中止都会被 xdist 当成 worker 崩溃：整轮变成 `INTERNALERROR … assert not crashitem`，退出码 3，报错里还夹着一个无辜测试的 nodeid |

`pytest_runtest_setup` 只对**真的会被执行**的用例触发（零假阳性），在用例体内任何
I/O 之前（含 `drop_database`），且用 `pytest.fail` —— 那是正常的 setup error：
实测 `-n 2` 下报 `2 errors`、3.4s 结束、**无 INTERNALERROR**（对照探针同时验证过
`pytest.UsageError` 与 `pytest.exit` 都会触发 INTERNALERROR）。再叠加
`session.shouldstop`（见 `fail_fast_on_parallel`）就不会把几百条用例跑完才报。

**契约的确切边界（有意）**：判据是"这次运行**会真的执行**共用真实资源的用例吗"，
不是"选中了吗"。`tests/real_mongo` 未 opt-in（`RUN_REAL_MONGO_TESTS≠1` 且
`RUN_EXTERNAL_TESTS≠1`）时整层被 `skip`——跳过的用例**不碰任何库**，因此没有可拒的
风险，`pytest tests/real_mongo -n 2` 得到的是 `46 skipped`（附该层自己的 skip 原因）
而不是拒答。启用该层（两个 env 任一）后，同样的命令立刻被拒。
**刻意不**把自己的 hook 标 `tryfirst` 去抢在 skip 判定之前——那会依赖 pluggy 对
`tryfirst` 实现的注册顺序（conftest 插件恰好在 builtin 之后注册），是脆弱假设。

## 判据为什么是"精确的"而不是"slow 层一律串行"

粗粒度做法（"slow 层一律串行"）会连带禁掉**并不碰真实 Milvus**的用例：
`core/input/tests/test_server.py` 的 10 条 slow 用例与 `TestDebugInfo` 的 4 条都请求了
`fake_storage`，各自在内存里跑，并行完全安全；全仓 44 条 slow 里
有 23 条属于这一类（2026-09-28 实测，遍历 `item.fixturenames` 现算——这两个数字是
**描述**，判据是本文件的 `uses_shared_test_milvus()`）。
所以判据取**精确**形式：**只有会读写共享真实 Milvus 集合的
用例**拒绝并行，条件与 `conftest.py::_guard_real_database` 的换桩决策同源
（见 `uses_shared_test_milvus`）。

## 怎么发现"正在并行"

只用 pytest / pytest-xdist 的**标准信号**，不读环境变量、不猜进程名：

- worker 进程：xdist 注入的 ``config.workerinput["workercount"]``（权威值）；
- controller 进程：xdist 自己解析出的 ``--numprocesses`` 与 ``--dist`` 选项。

`-n 0`（xdist 语义 = 不分布）与 `-n 1`（单 worker，用例仍逐条执行）都判为**串行**：
本判据问的是"会不会有两个测试同时在跑"，不是"有没有起子进程"。
"""

from __future__ import annotations

import os
from collections.abc import Iterable

import pytest

from core.tests._real_db_guard import is_exempt

__all__ = [
    "PARALLEL_AT_LEAST",
    "fail_fast_on_parallel",
    "parallel_violation_for_item",
    "uses_shared_test_milvus",
    "xdist_parallelism",
]

#: `-n auto` / `-n logical` 解析不出确切 worker 数时使用的**下限**。
#: 对「是否并行」这个判据而言 2 与 24 等价（都 > 1），且不会在错误信息里夸大数字。
PARALLEL_AT_LEAST = 2

#: 自带内存存储替身 → 不碰共享真实 Milvus 集合（`fake_storage` 会换桩 Milvus 客户端）。
_IN_MEMORY_STORAGE_FIXTURES = frozenset({"fake_storage"})

_REAL_MONGO_DB_ENV = "ELYSIA_TEST_MONGODB_URI"


def xdist_parallelism(config: pytest.Config) -> int:
    """本 pytest 进程看到的 xdist 并行度。

    Args:
        config: pytest Config（worker 或 controller 进程均可）。

    Returns:
        1 = 串行（未启用 xdist、`-n 0`、`-n 1`）；≥2 = 有多个 worker 真并行。
        `-n auto` / `-n logical` 取 ``PARALLEL_AT_LEAST``（"至少这么多"）。
    """
    worker_input = getattr(config, "workerinput", None)
    if isinstance(worker_input, dict):
        # worker 进程：xdist 注入的权威值，不依赖 controller 的选项解析。
        count = worker_input.get("workercount")
        if isinstance(count, int) and count > 0:
            return count

    try:
        dist = config.getoption("dist", "no")
        raw = config.getoption("numprocesses", None)
    except (AttributeError, ValueError):
        # xdist 未安装 / 非 pytest 上下文（单测直接调用）→ 无从分布。
        return 1
    if dist == "no" or raw is None or raw == 0:
        return 1
    if isinstance(raw, int):
        return raw if raw > 1 else 1
    # 'auto' / 'logical'：xdist 会按核数展开，必 > 1。
    return PARALLEL_AT_LEAST


def uses_shared_test_milvus(
    marker_names: Iterable[str], fixture_names: Iterable[str],
) -> bool:
    """该用例是否**可能**读写共享的真实 Milvus 测试集合。

    判据刻意与 `conftest.py::_guard_real_database` 的换桩决策**同源**，避免两处漂移：
    防线把 Milvus 客户端换成「操作即抛」的桩，当且仅当用例**不是**豁免层、
    **且**没有自带内存存储替身。取反即"可能触及真实集合"。

    `no_storage_guard` 的用例自管存储、防线连 Milvus 也不替它换桩——按上式的取反
    它们算"可能触及"。但当前唯一使用者 `core/storage/tests/test_idempotency_repo.py`
    只碰 Mongo（且用自有 fake），属 unit/service 层；若把它也算进来，
    `make test-quick -n 8` 会因一条根本不碰 Milvus 的用例整轮变红——那是**假阳性**，
    比漏报更糟（守卫的第一要务是不误伤）。故此处按"是否豁免层"判定，
    并把这条已知边界写进 `docs/testing.md`。

    Args:
        marker_names: 用例的 marker 名集合。
        fixture_names: 用例的 fixture 名集合（`item.fixturenames`）。

    Returns:
        True = 会走真实 Milvus；False = 已被内存替身/桩隔离。
    """
    if _IN_MEMORY_STORAGE_FIXTURES & set(fixture_names):
        return False
    return is_exempt(marker_names)


def parallel_violation_for_item(item: pytest.Item) -> str | None:
    """该用例在**当前并行度**下是否违反"共用真实资源不得并行"。

    Args:
        item: 即将执行的用例。

    Returns:
        违规原因（含修法指引）；串行、或该用例不共用真实资源时 None。
    """
    workers = xdist_parallelism(item.config)
    if workers < PARALLEL_AT_LEAST:
        return None  # 串行：两条判据都不成立（常见路径，尽早返回）
    if item.get_closest_marker("real_mongo") is not None:
        return _real_mongo_reason(workers, item.nodeid)
    if uses_shared_test_milvus(
            (m.name for m in item.iter_markers()), item.fixturenames):
        return _milvus_reason(workers, item.nodeid)
    return None


def _real_mongo_reason(workers: int, nodeid: str) -> str:
    db = os.getenv(_REAL_MONGO_DB_ENV, "").strip() or "<未设置>"
    return (
        f"tests/real_mongo 必须串行运行（当前并行度 = {workers}）：本层每个用例前后都会 "
        f"drop + recreate 共享的测试库（{db}），并行下 worker 互相抹库、身份与会话断言"
        f"基于固定库状态。\n"
        "tests/real_mongo must run serially because the suite drops and recreates a "
        "shared MongoDB database. Run without pytest-xdist / -n.\n"
        f"正确入口：`make test-real-mongo`（不加 -n）。首个违规用例：{nodeid}"
    )


def _milvus_reason(workers: int, nodeid: str) -> str:
    return (
        f"该用例读写**共享真实 Milvus 测试集合**（防线重定向后的 long_term_memory_v2__test），"
        f"不能在 {workers} 个 worker 下并行运行：\n"
        "core/tests/test_phase3.py::TestDebugDbApi 会 drop + recreate 该集合，"
        "core/tests/test_phase2.py::TestMemoryIntegration 做 count+1 / 召回断言"
        "——并行下实测出现过「集合被另一个 worker drop 掉 → search_memory 30s 超时失败」。\n"
        "修法：这一层**不加 -n**（`make test-full` 本就是串行；单跑请用不带 -n 的 pytest）。\n"
        f"首个违规用例：{nodeid}"
    )


def fail_fast_on_parallel(item: pytest.Item) -> None:
    """根 `conftest.py::pytest_runtest_setup` 的调用点：违规即 fail-fast。

    两件事：
    ① `pytest.fail(..., pytrace=False)` —— 正常的 setup error（**不是**
       `pytest.UsageError`/`pytest.exit`：那两者在 worker 里会被 xdist 当成 worker
       崩溃，整轮退化成 `INTERNALERROR … assert not crashitem`，实测如此）；
    ② `session.shouldstop = True` —— 本 worker 报**一条**即停。没有它，
       `-n 2` 下每个违规用例都会各报一遍（实测 40 条 vs 2 条），日志被同一段原因刷屏。

    Args:
        item: 即将执行的用例。
    """
    reason = parallel_violation_for_item(item)
    if reason is None:
        return
    item.session.shouldstop = True
    pytest.fail(reason, pytrace=False)
