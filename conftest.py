"""根 conftest —— 真实数据库访问防线（2026-09-11）。

## 问题

`pytest.ini` 把 `unit` / `service` 定义为「不连真实 DB」，但默认配置恰好指向开发者
本机生产库（`AppConfig().database.mongodb_uri = "mongodb://localhost:27017"`、
`db_name = "default"`）。任何 `Agent(config=AppConfig())` + `await agent.start()` 的
unit/service 测试，只要本机 Mongo 在跑就会连上并写入——且 `agent.start()` 对连不上库
**静默降级**，所以「写生产库」与「没写」两种情况下测试都绿，问题从不暴露。

实测污染（2026-09-11 核查 `default` 库）：`sessions` 标题 `active`/`lease`/`burst`、
`character_versions.session_id="test_default_type"`；2026-09-10 复盘曾清过一次，
**测试跑完后再次出现**。

## 防线

**autouse `_guard_real_database`**：凡未标 integration/slow/external 的测试，把**指向生产库**
的 Mongo 连接导流到内存 `FakeDocumentDBBackend`、把 Milvus 客户端拦截为抛错桩。

- 判据是**实际连接串**（`AppConfig().database.mongodb_uri`），不是只看 provider_id——
  测试若显式把 `MONGODB_URI` 改到别处（例如指向不可达端口以验证失败分支），不受影响。
- **导流而非判失败**：单元/服务层本就不该连真库，静默导流既消除污染、又不必为上百个
  既有测试逐一加标记。运行结束时终端会汇总被导流的次数，保持可见。
- 需要「防线别插手」的测试（如注入自有 fake 并驱动真实 `connect()` 路径）可请求
  `no_storage_guard` fixture；需要显式声明隔离的用 `fake_storage`。

背景与实测证据详见 `core/tests/_real_db_guard.py` 的模块 docstring。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:  # 保证 `core.tests._real_db_guard` 可导入
    sys.path.insert(0, str(_ROOT))

from core.tests._real_db_guard import (  # noqa: E402
    install_blocked_milvus,
    install_fake_mongo,
    is_exempt,
)

_REDIRECTED_TESTS: set[str] = set()
"""被防线导流过的测试 nodeid（供 session 结束汇总）。"""


@pytest.fixture
def fake_storage(monkeypatch):
    """把生产 Mongo 导流到内存 fake、Milvus 拦截为桩——「本测试不碰真实库」的显式声明。

    适用于需要 `agent.start()` 跑完整管线、但不应写生产库的 unit/service 测试。
    请求本 fixture 的测试由 autouse 防线放行（见 `_guard_real_database`）。
    """
    install_fake_mongo(monkeypatch)
    install_blocked_milvus(monkeypatch)


@pytest.fixture
def no_storage_guard():
    """本测试**自行管理**存储连接，请 autouse 防线让位。

    用于注入自有 fake（如 `_DurableFakeDB`）并驱动真实 `connect()` 路径的测试——
    防线的导流会改变其语义。
    """


@pytest.fixture(autouse=True)
def _guard_real_database(request, monkeypatch):
    """unit / service 测试不得连接真实数据库（详见模块 docstring）。"""
    if is_exempt(m.name for m in request.node.iter_markers()):
        yield
        return
    if {"fake_storage", "no_storage_guard"} & set(request.fixturenames):
        yield  # 已显式隔离 / 自管存储
        return

    def _record(_where: str) -> None:
        _REDIRECTED_TESTS.add(request.node.nodeid)

    install_fake_mongo(monkeypatch, _record)
    install_blocked_milvus(monkeypatch, _record)
    yield


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """汇总被导流的测试——保持可见，但不判失败（避免为既有测试批量加标记）。"""
    if not _REDIRECTED_TESTS:
        return
    terminalreporter.write_sep(
        "-", f"真实库防線：{len(_REDIRECTED_TESTS)} 个 unit/service 测试的连接被导流到内存 fake"
    )
    for nodeid in sorted(_REDIRECTED_TESTS)[:10]:
        terminalreporter.write_line(f"  {nodeid}")
    if len(_REDIRECTED_TESTS) > 10:
        terminalreporter.write_line(f"  … 其余 {len(_REDIRECTED_TESTS) - 10} 个省略")
