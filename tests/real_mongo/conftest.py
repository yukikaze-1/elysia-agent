# 测试层：tests/real_mongo —— 真实 MongoDB 测试层（S2-R6-1）
"""真实 MongoDB 测试层的 opt-in、目标守卫与仓储工厂。

## 为什么需要（报告 S2-R6-1）

评测报告实测的结论是**全仓不存在任何真实 MongoDB 测试**：所有仓储测试要么用
`FakeDocumentDBBackend`（内存 dict），要么在 `_db=None` 上验 fail-closed。于是
"索引声明能被 Mongo 8 接受吗""唯一索引真的会拒重复键吗""`$setOnInsert` 的语义是
我以为的那样吗""TTL 的 `expireAfterSeconds` 真被接受了吗"——这些**只有真驱动能
回答**的问题，全仓零覆盖。

## 三层门（缺一不可，且**不许静默通过**）

1. **marker `real_mongo` + tier `external`**：既不在 `unit|service`（否则
   `make test-quick` 会连真库），也不在 Normal（`-m "not slow and not external"`
   排除）——默认任何档位都不跑，只有显式 `-m real_mongo` 才跑。
2. **`ELYSIA_TEST_MONGODB_URI` 必须显式给出**：没有默认值。默认指向
   `localhost:27017` 就等于把开发机上的**生产库**当测试库（根 conftest 的
   `_guard_real_database` 正是为防这件事）——宁可 skip（带明确原因），绝不猜。
3. **库名必须像测试库（含 `test`）**：不像就 **fail**（不是 skip）——本层会
   `drop_database`，指错库即数据事故。`RUN_REAL_MONGO_TESTS=1` 时连不上同样 **fail**：
   "显式要求了却做不到"按本仓「门禁的三种假绿③」的约定必须**拒答**。
4. **禁止并行**（2026-09-27 补）：本层每个用例前后 `drop_database`（见
   `real_mongo_backend`），worker 之间会互相抹库；且身份/会话断言基于**固定的
   库状态**。此前这条只写在 Makefile 注释里（"不要给 real_mongo 加 -n"）——
   `pytest tests/real_mongo -n 8` 随时可以加上，然后跑几百条、随机互相 drop、
   最后给出一堆莫名其妙的失败。现在它是机器不变量：根 `conftest.py` 的
   `pytest_collection_finish` 在**每个 worker 开跑任何用例之前**检查最终选择集，
   发现并行且选中了本层即拒答（判据与文案见
   `core/tests/_pytest_parallel.py::real_mongo_parallel_violation`）。

   **为什么守卫不写在本文件里**（本批实测结论，值得记下来）：conftest 只要被
   **加载**，它的 `pytest_configure` 就会被调用——而 xdist worker 在收集期会加载
   `tests/` 下所有 conftest。于是"本层禁止并行"写在本地会变成"任何并行运行都
   禁止"：实测 `pytest -m "unit or service" -n 2 tests/`（根本不选本层）直接报
   `ERROR tests/real_mongo - pytest.UsageError`，`make test-quick -n 8` 会整轮变红。
   判据必须装在"**选中**"上，而"选中"只有 `pytest_collection_finish(session)`
   才拿得到（`session.items` 是 `-m`/`-k` 过滤**之后**的结果）。

## ⚠️ 实测踩到过的坑（本层最重要的设计输入）

**`base_url` 只给连接串；目标库由 `database_name` 决定。** 第一次跑本层时我用
`BackgroundTaskResultRepository(base_url="mongodb://…/elysia_real_mongo_test")`
构造仓储，以为 URI 的 path 就是库名——结果 `database_name` 取默认值 `"default"`，
**索引与文档全写进了生产库**（`default.attachments` 的一条测试文档、
`default.background_task_results` 整个集合）。两个后果：

- 本层的隔离**不能只查 URI**，必须查"仓储真的连到了哪个库"——即
  `_assert_targets_test_db`（读后端持有的 database 对象，读不到就 **fail**，
  不许"查不了就算过"）；
- 仓储唯一构造入口是 `real_mongo_repo` 工厂，它强制显式传 `database_name`。

（已清理：删掉误写文档并 drop 掉本次新建的集合；`user_identity_profiles` 未被改动
——`$setOnInsert` 在面对已存在文档时确实不写，这条顺带被真库证实。）
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, TypeVar

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from Module.database.documentdb import DocumentDBBackend

#: 显式 opt-in 开关。不设 → 整层 skip（带原因）；设了 → 连不上就 fail。
_RUN_ENV = "RUN_REAL_MONGO_TESTS"
#: 测试库连接串（**无默认值**——理由见模块 docstring 第 2 条）。
_URI_ENV = "ELYSIA_TEST_MONGODB_URI"

_T = TypeVar("_T")


def _database_name(uri: str) -> str:
    """从连接串取库名（`mongodb://host:port/db?opts` → `db`）。取不到返回 ""。"""
    tail = uri.split("://", 1)[-1]
    if "/" not in tail:
        return ""
    return tail.split("/", 1)[1].split("?", 1)[0].strip()


def _assert_targets_test_db(repo: Any, expected: str) -> None:
    """断言仓储**真的**连在测试库上（读后端持有的 database 对象）。

    为什么不能只查 URI：`base_url` 给的是连接串，库名由 `database_name` 决定——
    第一次跑本层就这么把文档写进了生产库 `default`（见模块 docstring）。
    读不到库名时 **fail**：本层会 drop 库，"检查不了"不能当"通过"。
    """
    backend = getattr(repo, "backend", None)
    db = getattr(backend, "_db", None)
    name = getattr(db, "name", None)
    if not name:
        pytest.fail(
            f"无法确认 {type(repo).__name__} 的目标库（backend/database 不可读）——"
            "本层会 drop 库，故拒绝在无法核验的情况下继续")
    assert name == expected, (
        f"{type(repo).__name__} 连到了 {name!r} 而不是测试库 {expected!r}——"
        "base_url 的 path 不等于库名，库名由 database_name 决定")


async def _drop_test_db(backend: "DocumentDBBackend", db_name: str) -> None:
    """drop 整个测试库（比逐 collection 清空更彻底：索引/残渣一起没）。"""
    client = getattr(backend, "_client", None)
    if client is None:
        return
    try:
        await client.drop_database(db_name)
    except Exception:  # pragma: no cover - 清理失败不影响用例结论
        pass


@pytest.fixture
def real_mongo_uri() -> str:
    """测试库连接串（已确认库名像测试库）。"""
    uri = os.getenv(_URI_ENV, "").strip()
    if not uri:
        pytest.skip(
            f"未设置 {_URI_ENV}——真实 MongoDB 测试层需显式 opt-in"
            "（本地：docker compose -f docker-compose.test.yml up -d；"
            f"CI：ci.yml 的 real-mongo job 注入；或设 {_RUN_ENV}=1 让它硬失败）")
    db_name = _database_name(uri)
    if "test" not in db_name.lower():
        pytest.fail(
            f"拒绝在非测试库上运行：{_URI_ENV} 的库名是 {db_name!r}（不含 'test'）。"
            "本层会 drop 整个库——指错库即数据事故。")
    return uri


@pytest.fixture
def real_mongo_db_name(real_mongo_uri: str) -> str:
    """测试库名（从连接串解析）。仓储的 `database_name` 必须显式传它。"""
    return _database_name(real_mongo_uri)


@pytest_asyncio.fixture
async def real_mongo_backend(real_mongo_uri: str,
                            real_mongo_db_name: str) -> AsyncIterator["DocumentDBBackend"]:
    """直连测试库的真实后端；**进入即清空、退出再清空**（崩溃残留也不带入下一轮）。"""
    from Module.database.documentdb import DocumentDBFactory

    backend = DocumentDBFactory.create(
        f"mongodb/{real_mongo_db_name}", base_url=real_mongo_uri,
        server_selection_timeout_ms=3000)
    try:
        if not await backend.ping():
            raise ConnectionError(f"ping 失败：{real_mongo_uri}")
    except Exception as e:
        await backend.close()
        if os.getenv(_RUN_ENV, "") == "1":
            pytest.fail(f"{_RUN_ENV}=1 但真实 MongoDB 不可达（{real_mongo_uri}）：{e}")
        pytest.skip(f"真实 MongoDB 不可达（{real_mongo_uri}）且未显式要求"
                    f"（{_RUN_ENV}≠1）：{e}")

    await _drop_test_db(backend, real_mongo_db_name)   # 进入即清（上一轮崩溃残留）
    try:
        yield backend
    finally:
        await _drop_test_db(backend, real_mongo_db_name)
        await backend.close()


@pytest_asyncio.fixture
async def real_mongo_repo(
    real_mongo_uri: str, real_mongo_db_name: str,
) -> AsyncIterator[Callable[..., Awaitable[Any]]]:
    """仓储工厂——本层构造仓储的**唯一**入口。

    它强制 `database_name=<测试库>` 并核验落点（见 `_assert_targets_test_db`）。
    直接 `SomeRepository(base_url=uri)` 会连到默认库 `default`（= 生产库）。
    """
    created: list[Any] = []

    async def _make(cls: type[_T], **kwargs: Any) -> _T:
        repo = cls(database_name=real_mongo_db_name, base_url=real_mongo_uri, **kwargs)
        await repo.connect()
        _assert_targets_test_db(repo, real_mongo_db_name)
        created.append(repo)
        return repo

    try:
        yield _make
    finally:
        for repo in created:
            try:
                await repo.close()
            except Exception:  # pragma: no cover - best-effort 收尾
                pass
