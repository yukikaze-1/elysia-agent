"""真实数据库访问防线 —— 让「unit/service 不连真实 DB」从文档变成门禁。

## 为什么需要

`pytest.ini` 把 `service` 定义为「单模块测试……**不连真实 DB**」，但这条约定从未被
强制执行。而默认配置**恰好指向开发者的生产库**：

    AppConfig().mongodb_uri = "mongodb://localhost:27017"   # core/config/app_config.py:119
    AppConfig().db_name     = "default"                     # core/config/app_config.py:134

于是任何 `Agent(config=AppConfig())` + `await agent.start()` 的测试，只要开发者本机
Mongo 在跑，就会连上并写入——`agent.start()` 加载会话、`agent.stop()` 的 `save_all()`
把测试消息回写 `sessions` / `message_archive`。更糟的是 **`agent.start()` 对连不上库
是静默降级**，所以测试在「库可达（被污染）」与「库不可达（没污染）」两种情况下**都通过**
——问题从不自我暴露。

实测污染（2026-09-11 核查开发者 `default` 库）：

| 集合 | 残留 | 出处 |
|------|------|------|
| `sessions` | 标题 `active` / `lease` / `burst` | `core/agent/tests/test_multi_session.py` |
| `character_versions` | `session_id="test_default_type"` | `core/storage/tests/test_character_version_repo.py` |

2026-09-10 的复盘文档记录过同类残留 50 条并做过清理——**测试跑完后再次出现**，说明
这是一次性清理治不了的系统性问题。（`core/cognitive/tests/test_v2_generation.py` 的
`_inject_fake_storage` 修过同一 bug 的一个实例，但它是**文件内局部 helper**，没有全局防线。）

## 防线做什么

对**所有**测试（仅 `external` 放开），切断对生产库的写入：

| 目标 | 处置 | 为什么 |
|------|------|--------|
| **Mongo**（生产连接串） | 导流到内存 `FakeDocumentDBBackend` | `agent.start()`/`stop()` 读写 `sessions` / `message_archive` |
| **Milvus**（LTM 集合） | 集合名重定向到 `long_term_memory*__test` | `clear_all()` 是 **drop + recreate collection** |

unit/service 另把 Milvus 客户端替换为「构造成功、任何操作即抛」的桩（它们本就不需要向量库）。

**为什么 coverage 是「所有测试」而不是「unit/service」**：`integration`/`slow` 的语义是
「允许使用真实依赖」，但实测这类测试**并不真的需要真库**——`TestMemoryIntegration`
（写测试记忆）、`TestDebugDbApi`（清库）、`test_phase2::test_session_isolation_between_agents`
都用 `FakeLLMBackend`，真库只是被 `AppConfig()` 顺带连上。且它们写入用的是**真实 scope**
（`agent._user_identity.userId` + `chi_builtin_elysia`），与真实记忆无法区分。

**Milvus 尤其不能豁免**：`MemoryRepository.clear_all()` 会 **drop collection**。实测
`core/tests/test_phase3.py::TestDebugDbApi::test_ltm_clear_all` 跑一遍就把整个
`long_term_memory_v2` 清空（row_count 4 → 0）——`make test-full` 一次即抹掉全部长期记忆。
（当时 insert_log 里残留的条目全是测试数据，侥幸无真实记忆损失。）

净效果：测试可以照常跑真实管线，但**碰不到生产数据**。

## 手工检查残留

存量清理脚本（默认 dry-run，先备份，见各自 `--help`）：

- `scripts/dev/purge_test_residue.py`——集合级（测试会话 / `character_versions` 残留）。
- `scripts/dev/purge_test_messages.py`——`default` 会话里逐条的测试消息。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

__all__ = [
    "EXEMPT_MARKERS",
    "RealDatabaseAccessError",
    "install_blocked_milvus",
    "install_fake_mongo",
    "install_test_milvus_collection",
    "is_exempt",
    "make_blocked_client",
    "production_mongo_provider_ids",
]

EXEMPT_MARKERS: frozenset[str] = frozenset({"integration", "slow", "external"})
"""允许连真实数据库的 marker。与 pytest.ini 的分层定义一致。"""


def production_mongo_provider_ids() -> frozenset[str]:
    """生产 Mongo 的工厂标识集合。

    仓储 `connect()` 里的字面量是 `"mongodb/default"`（`core/storage/*_repo.py`），
    与 `config.yaml` 的 `database.db_name: default` 一致。这里取**两者并集**——配置改了
    但仓储字面量未同步时，防线仍能拦住。
    """
    from core.config import AppConfig

    return frozenset({f"mongodb/{AppConfig().database.db_name}", "mongodb/default"})


def _production_mongo_uris() -> frozenset[str]:
    """生产 Mongo 的连接串（配置值 + 历史默认）。"""
    from core.config import AppConfig

    return frozenset({AppConfig().database.mongodb_uri, "mongodb://localhost:27017"})


def _targets_production_mongo(kwargs: dict) -> bool:
    """本次 create 的目标是否就是生产库。

    判据是**实际生效的连接串**而非仅 provider_id：测试若显式把 `MONGODB_URI` 改到别处
    （如指向不可达端口以测失败分支），就不该被当成「生产库访问」而导流。
    调用点发生在测试体内，此时测试自己的 monkeypatch 已生效。

    注意要按**真实代码的同一解析链**回退（`base_url` → `MONGODB_URI` → 配置默认值）：
    都没设置时，实际生效的就是配置默认值（= 生产库），必须导流——否则会落到真工厂，
    报「MongoDB 连接串未设置」而不是被隔离。
    """
    import os

    from core.config import AppConfig

    uri = (
        kwargs.get("base_url")
        or os.getenv("MONGODB_URI")
        or AppConfig().database.mongodb_uri
    )
    return uri in _production_mongo_uris()


def install_fake_mongo(monkeypatch, record: Callable[[str], None] | None = None) -> None:
    """把**生产 Mongo** 连接重定向到 `FakeDocumentDBBackend`（内存）。

    只拦「provider_id 命中生产标识 **且** 连接串就是生产库」的调用——工厂自身的测试
    （`mongodb/elysia` 等）与显式改到别处的测试都不受影响。

    选择「替换为 fake」而非「抛错」：仓储的数据流继续可用，测试行为不变——拦截会在
    测试中途炸出无关的二次错误（如 StorageUnavailableError）。

    Args:
        monkeypatch: pytest 的 monkeypatch fixture。
        record: 命中生产库时的回调（供统计/诊断）。
    """
    from Module.database.documentdb.registry import DocumentDBFactory
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

    production_ids = production_mongo_provider_ids()
    real_create = DocumentDBFactory.create

    def _guarded_create(provider_id: str, **kwargs: object):
        if provider_id in production_ids and _targets_production_mongo(kwargs):
            if record is not None:
                record(f"{provider_id} → FakeDocumentDBBackend")
            return FakeDocumentDBBackend()
        return real_create(provider_id, **kwargs)

    monkeypatch.setattr(DocumentDBFactory, "create", staticmethod(_guarded_create))


def install_blocked_milvus(monkeypatch, record: Callable[[str], None] | None = None) -> None:
    """拦截拟真 Milvus 客户端。

    向量库没有等价的 fake 实现，故用「构造成功、操作即抛」的桩。`MemoryRepository.connect()`
    以 `except Exception` 兜底降级（`core/storage/memory_repo.py:59-62`），因此拦截只导致
    LTM 不可用、不会炸测试——目的仅是**阻止写真实向量库**。
    """
    monkeypatch.setattr(
        "Module.database.vectordb.milvus.MilvusClient",
        make_blocked_client(record or (lambda _w: None), "MilvusClient"),
    )


_TEST_COLLECTION_SUFFIX = "__test"


def install_test_milvus_collection(monkeypatch) -> None:
    """把 LTM 的 Milvus 集合名重定向到 `*__test`——生产记忆对任何测试不可见。

    **为什么必须做（2026-09-11 实测）**：`MemoryRepository.clear_all()` 是
    **drop + recreate collection**（`core/storage/memory_repo.py:163-169`），而
    `DELETE /debug/api/ltm` 会调它。`core/tests/test_phase3.py::TestDebugDbApi::
    test_ltm_clear_all` 因此会在真实 Milvus 上**清空整个 `long_term_memory_v2`**——
    `make test-full` 一次就抹掉全部长期记忆（实测 row_count 4 → 0）。

    反向的污染同样存在：`TestMemoryIntegration` / `test_insert_and_search_async` 往真实
    集合**写**「上周和妖梦约定周六去爬香山」等测试记忆，且用**真实 scope**
    （`agent._user_identity.userId` + `chi_builtin_elysia`）——与真实记忆无法区分。

    重定向后这些测试照常走 Milvus，但只作用于 `long_term_memory*__test`；生产集合不被
    任何测试读写。测试集合由 `connect()` 按需创建。

    幂等：autouse 防线与 `fake_storage` 都会调它，重复调用不叠加后缀。
    """
    from core.storage.memory_repo import MemoryRepository
    from core.storage.memory_repo_v2 import MemoryRepositoryV2

    for cls in (MemoryRepository, MemoryRepositoryV2):
        current = cls.COLLECTION
        if not current.endswith(_TEST_COLLECTION_SUFFIX):
            monkeypatch.setattr(cls, "COLLECTION", current + _TEST_COLLECTION_SUFFIX)

_MESSAGE = (
    "该测试试图访问真实数据库（{where}）。\n"
    "pytest.ini 把 unit/service 定义为「不连真实 DB」，而默认配置指向开发者本机生产库"
    "（mongodb://localhost:27017 / db_name=default）——继续执行会污染真实数据。\n"
    "修法（二选一）：\n"
    "  1) 注入内存 fake 存储：core/storage/tests/_fake_documentdb.FakeDocumentDBBackend\n"
    "     （参考 core/cognitive/tests/test_v2_generation.py::_inject_fake_storage）\n"
    "  2) 该测试确实需要真库 → 改标 @pytest.mark.integration（另需 external 时同标）\n"
)


class RealDatabaseAccessError(RuntimeError):
    """unit/service 测试试图访问真实数据库。"""


def is_exempt(marker_names: Iterable[str]) -> bool:
    """给定 marker 名集合，判断该测试是否允许连真实数据库。"""
    return bool(EXEMPT_MARKERS & set(marker_names))


def _blocker(record: Callable[[str], None], where: str) -> Callable[..., None]:
    """生成「记录 + 抛错」的可调用对象。"""

    def _raise(*_args: object, **_kwargs: object) -> None:
        record(where)
        raise RealDatabaseAccessError(_MESSAGE.format(where=where))

    return _raise


class _BlockedHandle:
    """桩句柄：任何属性 / 下标 / 调用都返回「记录 + 抛错」。

    兼容两种常见访问链：
      - `client[db][coll].insert_one(...)`（下标 + 属性 + 调用）
      - `client.admin.command(...)`（属性 + 调用）
    """

    def __init__(self, record: Callable[[str], None], path: str) -> None:
        # 用 object.__setattr__ 写入实例 __dict__，避免 __getattr__ 递归。
        object.__setattr__(self, "_record", record)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, attribute: str) -> Callable[..., None]:
        return _blocker(self._record, f"{self._path}.{attribute}")

    def __getitem__(self, key: object) -> "_BlockedHandle":
        return _BlockedHandle(self._record, f"{self._path}[{key!r}]")

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        return _blocker(self._record, self._path)()


def make_blocked_client(
    record: Callable[[str], None],
    display_name: str,
) -> type:
    """构造一个真实客户端的桩类。

    Args:
        record: 每次访问尝试的回调（记录访问路径，供 teardown 判定）。
        display_name: 客户端显示名（进错误信息，如 `AsyncIOMotorClient`）。

    Returns:
        可直接替换真实客户端类的桩类——**构造成功**，任何操作**记录并抛**
        `RealDatabaseAccessError`。
    """

    class _BlockedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            # 故意允许构造：只建后端、不做 I/O 的测试不应被误伤。
            pass

        def __getattr__(self, attribute: str) -> _BlockedHandle:
            # 返回 handle 而非 blocker：既支持 `client.admin.command(...)`（继续取属性），
            # 也支持 `client.close()`（直接调用）。
            return _BlockedHandle(record, f"{display_name}.{attribute}")

        def __getitem__(self, key: object) -> _BlockedHandle:
            return _BlockedHandle(record, f"{display_name}[{key!r}]")

    _BlockedClient.__name__ = display_name
    _BlockedClient.__qualname__ = display_name
    return _BlockedClient
