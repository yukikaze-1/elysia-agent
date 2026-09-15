# 测试层：tests/real_mongo/test_real_storage.py
"""真实 MongoDB 测试层：只有真驱动能回答的问题（S2-R6-1）。

**与 `FakeDocumentDBBackend` 的分工**：fake 验的是"仓储逻辑在给定后端返回下是否
正确"；本层验的是"**我们声明的后端契约在真实 MongoDB 8 上是否成立**"——索引声明
能不能被接受、唯一约束真的拒重复键吗、`$setOnInsert` 的语义是否如我所想、TTL 选项
是否真被接受、`schema_drift` 的自检与真实库是否一致。

**仓储一律经 `real_mongo_repo` 工厂构造**（它强制 `database_name=<测试库>` 并核验
落点）——直接 `SomeRepository(base_url=uri)` 会连到默认库 `default`，
见 `conftest.py` docstring 里那次实测事故。

**覆盖选择**（不是"把所有仓储再跑一遍"——那是重复）：只挑
① 声明了但从未在真库上验证过的东西（索引 / 唯一约束 / TTL）；
② 依赖真实驱动语义的关键不变量（条件 update 的原子性、`dict()` 拷贝保护、
   `$setOnInsert`、close 后 ping 必须说不可用）；
③ 门禁的**端到端**形态（`check_schema_drift` 对真库跑一次）。
"""

from __future__ import annotations

import pytest

from core.schema.types import AttachmentId
from core.storage.attachment_repo import (
    COLLECTION as ATTACHMENT_COLLECTION,
    STATUS_ACTIVE,
    STATUS_DELETED,
    AttachmentRepository,
)
from core.storage.background_task_repo import (
    BACKGROUND_TASK_RESULT_TTL_SECONDS,
    COLLECTION as BG_COLLECTION,
    BackgroundTaskResultRepository,
)
from core.storage.base import is_duplicate_key
from core.storage.schema_drift import EXPECTED_INDEXES, check_schema_drift
from core.storage.user_identity_repo import UserIdentityRepository

pytestmark = [
    pytest.mark.external,      # opt-in 层：默认任何档位都不跑（Normal = not external）
    pytest.mark.real_mongo,    # 本层专属 marker：CI 的 real-mongo job 用 -m real_mongo 选中
]


class TestIndexDeclarationsAreRealizable:
    """`ensure_index` 的声明必须能被真库接受——这是"自动覆盖"承诺的前提。"""

    @pytest.mark.asyncio
    async def test_unique_index_actually_rejects_duplicates(self, real_mongo_backend,
                                                           real_mongo_repo):
        """唯一索引**真的**拒重复键（fake 的 unique 是自实现的，证明不了驱动层）。"""
        repo = await real_mongo_repo(BackgroundTaskResultRepository)
        await repo.upsert_pending(task_id="wf-real-1", result="r", original_intent="i",
                                 dispatched_at="2026-09-15T00:00:00Z",
                                 status="completed", is_error=False)
        # upsert 幂等（同 task_id）——不是抛错路径
        await repo.upsert_pending(task_id="wf-real-1", result="r2", original_intent="i",
                                 dispatched_at="2026-09-15T00:00:00Z",
                                 status="completed", is_error=False)
        assert await repo.count_pending() == 1

        # 而**裸插入**同键必须撞唯一索引（证明索引真在库里生效，而不只是被声明）
        await real_mongo_backend.insert_one(BG_COLLECTION, {"task_id": "wf-real-2"})
        with pytest.raises(Exception) as ei:
            await real_mongo_backend.insert_one(BG_COLLECTION, {"task_id": "wf-real-2"})
        assert is_duplicate_key(ei.value), \
            f"唯一索引未生效？异常={ei.value!r}（is_duplicate_key 判否）"

    @pytest.mark.asyncio
    async def test_ttl_index_accepted_with_declared_seconds(self, real_mongo_backend,
                                                           real_mongo_repo):
        """TTL 索引真被接受，且 `expireAfterSeconds` 与声明一致。

        为什么值得单独测：TTL 秒数写在代码里、`schema_drift` 只比"有没有 TTL"
        （不比秒数，避免正常调参被误报）。若服务端**静默忽略**了我们传的选项，
        两边都不会红——只有读回真库的 `list_indexes` 才看得见。
        """
        await real_mongo_repo(BackgroundTaskResultRepository)

        indexes = await real_mongo_backend.list_indexes(BG_COLLECTION) or []
        by_name = {i.get("name"): i for i in indexes}
        ttl = by_name.get("ttl_expires_at_1")
        assert ttl is not None, f"TTL 索引缺失，库中索引={sorted(by_name)}"
        assert ttl.get("expireAfterSeconds") == BACKGROUND_TASK_RESULT_TTL_SECONDS, (
            f"TTL 秒数不符：库={ttl.get('expireAfterSeconds')} "
            f"代码={BACKGROUND_TASK_RESULT_TTL_SECONDS}")

    @pytest.mark.asyncio
    async def test_schema_drift_agrees_with_real_database(self, real_mongo_backend,
                                                         real_mongo_repo):
        """门禁的**端到端**形态：真库上跑 `check_schema_drift` → 无漂移/无缺失/无冲突。

        这一条把 `EXPECTED_INDEXES`（AST 从代码派生）与**真实库**接起来：此前它只在
        mock backend 上被测过，"表与库是否一致"从未验证。新增索引却忘了同步表 →
        这里报 missing；表里有而代码没建 → 报 orphan。
        """
        # 真库上把本层涉及的仓储都 connect 一遍（= 生产启动时 ensure_index 的效果）
        await real_mongo_repo(BackgroundTaskResultRepository)
        await real_mongo_repo(AttachmentRepository)
        await real_mongo_repo(UserIdentityRepository)

        report = await check_schema_drift(real_mongo_backend)

        assert report.scanned is True, "未完成扫描（存储不可用？）——不能当成健康"
        assert not report.missing, f"代码声明但库里没有的索引：{report.missing}"
        assert not report.orphans, f"库里有但代码不声明的孤儿索引：{report.orphans}"
        assert not report.option_conflicts, f"同名索引选项不符：{report.option_conflicts}"
        # `absent` = "声明了索引但 collection 还没建"（全新测试库的常态，本身不是问题）。
        # 这里断言的是**本层建过的三个集合不在其中**——否则就是"我们以为建了、其实没建"。
        touched = {BG_COLLECTION, ATTACHMENT_COLLECTION, "user_identity_profiles"}
        assert touched.isdisjoint(set(report.absent)), (
            f"本层 connect 过的 collection 仍被判定为缺席：{sorted(touched & set(report.absent))}")

    @pytest.mark.asyncio
    async def test_expected_indexes_cover_what_we_created(self, real_mongo_backend,
                                                         real_mongo_repo):
        """反向：真库里的索引名必须与 `EXPECTED_INDEXES` 逐项相等（`_id_` 除外）。"""
        await real_mongo_repo(BackgroundTaskResultRepository)

        live = await real_mongo_backend.list_indexes(BG_COLLECTION) or []
        names = {i.get("name") for i in live} - {"_id_"}
        assert names == set(EXPECTED_INDEXES[BG_COLLECTION]), (
            f"库与表不一致：库={sorted(names)} 表={sorted(EXPECTED_INDEXES[BG_COLLECTION])}")


class TestRealDriverSemantics:
    """依赖真实驱动语义的不变量（fake 会"照我说的做"，证明不了驱动行为）。"""

    @pytest.mark.asyncio
    async def test_remove_ref_is_conditional_and_idempotent(self, real_mongo_repo):
        """#41 依赖的前提：`remove_ref` 是**条件 update**，重复调用不减成负数。

        fake 是"照实现写的"，真库才验证 `{"refs": message_id}` 这个过滤条件真的
        只命中一次（若过滤写错，第二次调用会把 ref_count 减到 0 并误软删）。
        """
        repo = await real_mongo_repo(AttachmentRepository)
        await repo.create({
            "attachment_id": "att_real_1", "kind": "image",
            "stored_name": "att_real_1.png", "status": STATUS_ACTIVE,
            "refs": ["m1", "m2"], "ref_count": 2,
        })

        await repo.remove_ref(AttachmentId("att_real_1"), "m1")
        doc = await repo.get(AttachmentId("att_real_1"))
        assert doc["ref_count"] == 1 and doc["refs"] == ["m2"]
        assert doc["status"] == STATUS_ACTIVE, "还有引用时不得软删"

        # 重复释放同一条（journal 重试的形状）→ no-op，不减成负数
        await repo.remove_ref(AttachmentId("att_real_1"), "m1")
        doc = await repo.get(AttachmentId("att_real_1"))
        assert doc["ref_count"] == 1 and doc["refs"] == ["m2"]

        # 释放最后一条 → 归零即软删（GC 规则的输入）
        await repo.remove_ref(AttachmentId("att_real_1"), "m2")
        doc = await repo.get(AttachmentId("att_real_1"))
        assert doc["ref_count"] == 0 and doc["status"] == STATUS_DELETED
        assert doc.get("deleted_at"), "软删必须带时间戳（GC 24h 规则的输入）"

    @pytest.mark.asyncio
    async def test_insert_one_does_not_mutate_caller_dict(self, real_mongo_backend):
        """`insert_one` 不得把注入的 `_id` 写回调用方的 dict（本仓明确约定）。

        motor 原生行为是**会**修改传入的 dict——项目为此在 backend 里传 `dict()`
        拷贝。这是纯驱动侧不变量，fake 上永远不会暴露。
        """
        payload = {"k": "v"}

        await real_mongo_backend.insert_one(ATTACHMENT_COLLECTION, payload)

        assert "_id" not in payload, f"调用方 dict 被注入 _id：{payload}"

    @pytest.mark.asyncio
    async def test_bootstrap_if_absent_does_not_overwrite(self, real_mongo_repo):
        """`$setOnInsert` 语义：已存在就绝不被 bootstrap 覆盖（身份权威的前提）。"""
        from core.profile.user_identity import UserIdentity

        repo = await real_mongo_repo(UserIdentityRepository)
        first = await repo.bootstrap_if_absent(UserIdentity(displayName="A"))
        second = await repo.bootstrap_if_absent(UserIdentity(displayName="B"))

        assert first is not None and second is not None
        assert second.revision == first.revision, "bootstrap 不得推进 revision"
        assert second.displayName == "A", "bootstrap 不得覆盖既有身份"

    @pytest.mark.asyncio
    async def test_ping_is_false_after_close(self, real_mongo_uri, real_mongo_db_name):
        """`_mark_unavailable` 依赖的真实判据：close 之后 probe 必须**说不可用**。

        这条守的是"假阳性"那一类缺陷：`_db is not None` 不代表可用（S2-B5/P1-05），
        健康判据必须是真 ping。用**独立**实例验证（不动 fixture 的连接）。
        """
        from Module.database.documentdb import DocumentDBFactory

        probe = DocumentDBFactory.create(
            f"mongodb/{real_mongo_db_name}", base_url=real_mongo_uri)
        assert await probe.ping() is True, "连通性前置：这个实例本该可用"
        await probe.close()

        assert await probe.ping() is False, (
            "close 之后 ping 仍返回 True —— 那 `_health_check` 就永远看不到"
            "『客户端已废』，正是 P1-05 要消除的假阳性")
