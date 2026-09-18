# core/character/tests/test_write_chain_compensation.py
"""写链补偿契约（CODE_REVIEW_REPORT #46 gap row #8）。

背景：`core/character/service/definitions.py` 的五条写链都跨集合写。`archive()` 的
两集合链已被 `test_archive_consistency.py` 覆盖，但 create/update/clone/rollback 的
**中途失败补偿**从未被断言——`insert_definition` / `delete_revision` 在本目录里
grep 不到任何调用点断言。

本文件钉住的写序与补偿（行号对应 definitions.py，写链表见交付报告）：

| 链 | 写序 | 中途失败处理 |
|---|---|---|
| create (84-111) | `rev_repo.append_revision(cid,1)` → `repo.insert_definition` | 第二步失败 → **有补偿** `delete_revision(cid,1)` |
| update (113-164) | `rev_repo.append_revision(cid,n)` → `repo.cas_revision_pointer` | CAS 返回 False → **有补偿** `delete_revision(cid,n)`；CAS **抛异常** → 无补偿（孤儿 revision） |
| rollback (230-290) | 同 update | 同 update |
| clone (166-196) | 完全委托 `create`（无自有写入） | 同 create |
| archive (198-228) | `identity.update_if_revision` → `repo.archive` | **无补偿**：profile 已改指而归档失败时半程写入保留 |

**2026-09-15 更新（#46 缺口行 8 的 4/5/6 号缺陷已修）**：本文件最初把四处缺陷当作
「当前契约」钉住。修复后这些用例改成断言**修好的行为**，并补了判定分支：
`_commit_revision_pointer` 的判据从"有没有抛异常"改成"**指针到哪了**"——
CAS 抛异常时先回读，指针没动才补偿、已推进则按成功、无法判定则留孤儿 + error 日志
（**宁可泄漏不可误删**）。仍未修的是 archive 的半程写入与实例传播的非白名单异常冒泡
（见文件末尾两条「当前契约」用例），它们需要各自的设计决定。
"""

import pytest

from core.character.draft import CharacterDraft
from core.character.instance import CharacterInstance
from core.character.service import (
    BUILTIN_FALLBACK_CHARACTER_ID,
    CharacterNotFoundError,
    CharacterService,
    RevisionConflictError,
)
from core.profile.profile_store import UserIdentityStore
from core.schema.utils import now_iso
from core.storage.base import StorageUnavailableError
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.storage.character_repo import COLLECTION as CHARACTERS_COLLECTION
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import COLLECTION as REVISIONS_COLLECTION
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service

BODY = "你是写链补偿测试角色。"

# 写链调用点的日志名（断言顺序用；字面量集中在此，改坏顺序会直接红）
APPEND = "rev_repo.append_revision"
INSERT = "repo.insert_definition"
DELETE = "rev_repo.delete_revision"
CAS = "repo.cas_revision_pointer"
IDENTITY = "identity.update_if_revision"
ARCHIVE = "repo.archive"


def _draft(**overrides) -> CharacterDraft:
    """最小合法草稿（形状照抄 test_service.py）。"""
    values: dict = {
        "displayName": "写链角色",
        "promptProfile": {
            "corePersonality": BODY,
            "relationshipStyle": BODY,
            "speakingStyle": BODY,
        },
    }
    values.update(overrides)
    return CharacterDraft(**values)


def _service(*, store: UserIdentityStore | None = None) -> CharacterService:
    """真实仓储 + 内存假后端（`catalog=None`：内置目录不参与写链，故不装配）。"""
    return CharacterService(
        repo=CharacterRepository(db=FakeDocumentDBBackend()),
        rev_repo=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        catalog=None,
        user_identity_store=store if store is not None else UserIdentityStore(),
    )


def _rows(repo, collection: str) -> list[dict]:
    """假后端里某集合的原始行——用来断言「一行都没留下」这类库级事实。"""
    backend = repo.backend
    assert isinstance(backend, FakeDocumentDBBackend)
    return backend.collections.get(collection, [])


async def _create(svc: CharacterService, display_name: str = "写链角色") -> str:
    """建一个 custom 角色，返回 characterId。"""
    detail = await svc.create(
        _draft(displayName=display_name), owner_user_id="usr_local_default"
    )
    return detail.summary.characterId


class _WriteLog:
    """按序记录仓储写调用，并可在指定调用点注入异常。

    沿用 `test_archive_consistency.py` 的手法（真实仓储 + 假后端，只替换被测调用点），
    不引入新的测试 harness；`spy()` 返回原始实现，供「并发写者」替身绕过日志调用。
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, tuple]] = []

    def spy(self, monkeypatch, owner, attr, label, *, raises=None, before=None):
        """给 `owner.attr` 装记录器：记录 → (可选) before 钩子 → 注入异常或调真实实现。"""
        original = getattr(owner, attr)

        async def _wrapper(*args, **kwargs):
            self.entries.append((label, args))
            if before is not None:
                await before()
            if raises is not None:
                raise raises
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, attr, _wrapper)
        return original

    @property
    def names(self) -> list[str]:
        return [label for label, _ in self.entries]

    def args_for(self, label: str) -> list[tuple]:
        return [args for name, args in self.entries if name == label]


async def _install_cas_loser(svc: CharacterService, monkeypatch, *, append_impl=None) -> None:
    """把下一次 `cas_revision_pointer` 变成「并发写者先提交」的**真实**落败。

    做法：真实并发写者会先 append 自己的 revision（max+1）再把指针 CAS 到它；被测调用
    随后拿旧的 expected_revision 做 CAS，filter 不再命中 → 真 CAS 返回 False。走的是
    真实 CAS 语义（不是伪造返回值）。
    """
    append = append_impl if append_impl is not None else svc.rev_repo.append_revision
    original_cas = svc.repo.cas_revision_pointer

    async def _rival(character_id, expected_revision, new_revision, updated_at):
        current_max = await svc.rev_repo.max_revision(character_id)
        source = await svc.rev_repo.get_revision(character_id, current_max)
        assert source is not None
        champion = source.model_copy(
            deep=True, update={"revision": current_max + 1, "displayName": "并发对手"}
        )
        await append(champion)
        assert await original_cas(
            character_id, expected_revision, current_max + 1, updated_at=now_iso()
        ) is True
        return await original_cas(
            character_id, expected_revision, new_revision, updated_at=updated_at
        )

    monkeypatch.setattr(svc.repo, "cas_revision_pointer", _rival)


async def _install_rival_rollback(svc: CharacterService, monkeypatch) -> None:
    """让"下一次 CAS"落败，但**没有**任何更高版本出现——并发写者把指针回滚了。

    与 `_install_cas_loser` 的区别：那个模拟"对手 append 更高版本并抢先发布"（于是本次
    append 的 revision 不是最高版本，补偿必须让位给"不许挖洞"）；这个模拟"对手把指针退回
    旧版本"（revisions 集合没有新增），此时本次 append 的 revision 仍是最高版本，补偿
    **应当**照旧删除。两条路都要有用例，否则"加了前提"很容易变成"把补偿关掉了"。

    必须在 CAS **内部**移动指针：`update()` 开头的 `currentRevision != expected_revision`
    预检会先拦下"指针已经不对"的普通情形，测试就测不到 CAS 这一段。
    """
    original_cas = svc.repo.cas_revision_pointer

    async def _rival(character_id, expected_revision, new_revision, updated_at):
        await original_cas(character_id, expected_revision, 1, updated_at=now_iso())
        return await original_cas(
            character_id, expected_revision, new_revision, updated_at=updated_at)

    monkeypatch.setattr(svc.repo, "cas_revision_pointer", _rival)


# --------------------------------------------------------------------------- create


@pytest.mark.asyncio
async def test_create_definition_write_failure_compensates_revision(monkeypatch):
    """create 第二条写失败 → **有补偿**：delete_revision(cid, 1)，不留孤儿 revision。"""
    svc = _service()
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(
        monkeypatch, svc.repo, "insert_definition", INSERT,
        raises=StorageUnavailableError("注入：characters 写失败"),
    )
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    with pytest.raises(StorageUnavailableError):
        await svc.create(_draft(), owner_user_id="usr_local_default")

    assert log.names == [APPEND, INSERT, DELETE]
    character_id = log.args_for(APPEND)[0][0].characterId
    assert character_id.startswith("chr_custom_")
    assert log.args_for(DELETE) == [(character_id, 1)]
    # 幸存状态一致：两个集合里都没有这个角色的任何行
    assert await svc.rev_repo.get_revision(character_id, 1) is None
    assert await svc.rev_repo.max_revision(character_id) == 0
    assert await svc.repo.get_definition(character_id) is None
    assert await svc.get(character_id) is None
    assert _rows(svc.rev_repo, REVISIONS_COLLECTION) == []
    assert _rows(svc.repo, CHARACTERS_COLLECTION) == []


@pytest.mark.asyncio
async def test_create_compensation_failure_keeps_original_error_and_orphan(monkeypatch, caplog):
    """补偿自身失败 → **原始异常照常抛出**（不顶替），孤儿留下但有一条带 traceback 的 warning。

    修前（#46 缺口行 8 · 6 号缺陷）：补偿是裸 `await delete_revision(...)`，它抛
    `StorageUnavailableError` 时调用方看到的是**补偿失败**——原始成因
    「insert_definition 失败」被掩盖，异常链上也没有 `__cause__` 指回去。

    修后：善后动作降级成 warning（`definitions.py::_compensate_delete_revision`），
    病因照原样上抛。孤儿 revision 仍会留下——**这是有意的**：补偿是"尽力而为"，
    留一条可对账的孤儿（`list_revisions` 里看得见）比把病因吞掉好。
    """
    import logging

    svc = _service()
    log = _WriteLog()
    original_error = StorageUnavailableError("注入：characters 写失败")
    compensation_error = StorageUnavailableError("注入：补偿删除也失败")
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.repo, "insert_definition", INSERT, raises=original_error)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE, raises=compensation_error)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(StorageUnavailableError) as excinfo:
            await svc.create(_draft(), owner_user_id="usr_local_default")

    assert excinfo.value is original_error               # 病因不被善后失败顶替
    assert excinfo.value is not compensation_error
    character_id = log.args_for(APPEND)[0][0].characterId
    assert log.args_for(DELETE) == [(character_id, 1)]   # 补偿确实尝试过
    assert await svc.rev_repo.get_revision(character_id, 1) is not None   # 孤儿行
    assert await svc.repo.get_definition(character_id) is None
    assert await svc.get(character_id) is None
    assert any("补偿失败" in r.getMessage() for r in caplog.records), \
        "补偿失败必须留痕（否则孤儿是静默产生的）"


@pytest.mark.asyncio
async def test_create_success_never_compensates(monkeypatch):
    """成功路径**不得**触发补偿：顺序为 revision → definition，delete 调用 0 次。"""
    svc = _service()
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.repo, "insert_definition", INSERT)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    detail = await svc.create(_draft(), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId

    assert log.names == [APPEND, INSERT]
    assert log.args_for(DELETE) == []
    assert await svc.rev_repo.get_revision(character_id, 1) is not None
    got = await svc.get(character_id)
    assert got is not None and got.summary.currentRevision == 1


@pytest.mark.asyncio
async def test_create_append_failure_writes_nothing_and_never_compensates(monkeypatch):
    """第一条写就失败 → 不写 definition，也没有补偿调用（无可补偿之物）。"""
    svc = _service()
    log = _WriteLog()
    log.spy(
        monkeypatch, svc.rev_repo, "append_revision", APPEND,
        raises=StorageUnavailableError("注入：revision 写失败"),
    )
    log.spy(monkeypatch, svc.repo, "insert_definition", INSERT)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    with pytest.raises(StorageUnavailableError):
        await svc.create(_draft(), owner_user_id="usr_local_default")

    assert log.names == [APPEND]
    assert _rows(svc.rev_repo, REVISIONS_COLLECTION) == []
    assert _rows(svc.repo, CHARACTERS_COLLECTION) == []


# --------------------------------------------------------------------------- update


@pytest.mark.asyncio
async def test_update_cas_loss_keeps_revision_when_higher_version_exists(monkeypatch):
    """CAS 落败且库中已有**更高版本** → **不删**（删了会在历史里挖洞）。

    2026-09-15 只读分析实测：`max_revision()` 读 revisions 集合、CAS 比对 Definition 的
    `currentRevision`，两者可以不同步——并发写者能读到我们**已落库但未发布**的 revision 2，
    于是 append 3 并把指针 CAS 到 3。此时若删掉 2，历史变成 `{1,3}`：中间缺一格，
    `reconcile_instance_revision` 逐格取 revision 时**永久**拉「角色 revision 缺失」，
    而 load 路径（`runtime_loader`）没有兜底 → 该实例再也加载不出来（会话创建/切换 503）。

    所以补偿加了前提：**仅当自己仍是最高版本时才删**。留孤儿只是泄漏（`list_revisions`
    里可见、可对账），挖洞不可恢复——与 #41 同一条纪律：选可恢复的那一侧。
    """
    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    append_original = log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    await _install_cas_loser(svc, monkeypatch, append_impl=append_original)

    with pytest.raises(RevisionConflictError):
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert log.names == [APPEND], "库中已有更高版本 → 不得补偿删除"
    assert log.args_for(DELETE) == []
    assert await svc.rev_repo.get_revision(character_id, 2) is not None   # 孤儿保留
    assert await svc.rev_repo.get_revision(character_id, 3) is not None   # 并发写者的版本
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 3
    got = await svc.get(character_id)
    assert got is not None and got.summary.displayName == "并发对手"
    # 关键不变式：历史**连续**（缺格会让 reconcile 永久失败）
    revisions = await svc.list_revisions(character_id)
    assert sorted(item.revision for item in revisions.items) == [1, 2, 3]


@pytest.mark.asyncio
async def test_update_cas_loss_still_compensates_when_still_highest(monkeypatch):
    """CAS 落败但自己仍是最高版本 → **照旧补偿删除**（前提没把这条路径关掉）。

    现实形态：另一个客户端**回滚**了指针（指针退回旧版本，而 revisions 集合里没有新增
    更高版本）。此时删掉本次 append 的孤儿不会挖洞——补偿的原始意图仍要成立。
    """
    svc = _service()
    character_id = await _create(svc)
    await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    await _install_rival_rollback(svc, monkeypatch)

    with pytest.raises(RevisionConflictError):
        await svc.update(character_id, _draft(displayName="v3"), expected_revision=2)

    assert log.names == [APPEND, DELETE], "自己仍是最高版本 → 应当补偿删除"
    assert log.args_for(DELETE) == [(character_id, 3)]
    assert await svc.rev_repo.get_revision(character_id, 3) is None
    revisions = await svc.list_revisions(character_id)
    assert sorted(item.revision for item in revisions.items) == [1, 2]


@pytest.mark.asyncio
async def test_update_cas_loss_reports_true_current_revision(monkeypatch):
    """CAS 落败时 409 里的 currentRevision 必须是**回读到的真值**（5 号缺陷，已修）。

    修前：definitions.py 用 `definition.currentRevision`（本次调用开头读到的内存值）构造
    `RevisionConflictError`，而 CAS 落败恰恰意味着库里**不是**这个值；路由层把它原样放进
    409 body（`core/input/character_routes.py` 的 `details.currentRevision`）+ `retryable=True`
    → 客户端按一个必然再次冲突的值重试（活锁）。

    修后：`_commit_revision_pointer` 在补偿之后**绕过缓存**回读（`get_definition(fresh=True)`，
    缓存里那份与内存里是同一个对象，直接读会拿到过期值），把真值填进 409。
    """
    svc = _service()
    character_id = await _create(svc)
    await _install_cas_loser(svc, monkeypatch)

    with pytest.raises(RevisionConflictError) as excinfo:
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 3   # 真实当前版本
    assert excinfo.value.current_revision == 3, \
        "409 必须回报真值——回报过期值会让客户端按必然再冲突的版本重试"


@pytest.mark.asyncio
async def test_update_cas_loss_reports_true_revision_despite_stale_cache(monkeypatch):
    """**跨进程写者**把指针推走后，409 仍须报真值——这条才真正压住 `fresh=True`。

    上一条用例的"并发对手"跑在**同一个 repo 实例**里，它的 CAS 成功会顺手
    `_cache_invalidate`，于是回读即使命中缓存也能拿到新值——那条用例因此**测不到**
    "绕过缓存"这件事（注入验证时被当场抓到：把 `fresh` 判断去掉，它照样绿）。

    真实形态是**另一个进程**（或多实例部署）写了库：本进程的缓存不会被通知，仍然是 1，
    而库里已经是 3。这里直接把库里指针改掉来复现（不经本 repo 的写路径，正是"别的进程
    写的"语义），再触发一次 CAS 落败：

      - `fresh=True` → 回读得 3 → 409 报 3（客户端知道该用 3 重试）；
      - 走缓存 → 拿到内存里那个 1 → 409 报 1 → 客户端按 1 重试必然再冲突（活锁）。
    """
    svc = _service()
    character_id = await _create(svc)
    # 断言前提：此刻缓存里确实有一份（否则这条用例又白测了）
    assert await svc.repo.get_definition(character_id) is not None

    # "另一个进程"把指针推到 3（绕过本 repo，故不会 invalidate 本地缓存）
    await svc.repo.backend.update_one(
        CHARACTERS_COLLECTION,
        {"character_id": character_id},
        {"$set": {"current_revision": 3, "updated_at": now_iso()}},
    )
    assert svc.repo._cache[character_id].currentRevision == 1  # noqa: SLF001 — 缓存仍是旧的

    with pytest.raises(RevisionConflictError) as excinfo:
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert excinfo.value.current_revision == 3, \
        "跨进程写者推进指针后，409 必须报库里的真值而不是本地缓存/内存里的旧值"


@pytest.mark.asyncio
async def test_update_cas_loss_with_unreadable_store_reports_unknown(monkeypatch):
    """回读也失败 → 409 报 `None`（未知），**不得**退回那个过期值。

    路由层对 `current_revision is None` 的处理是**省略** `details.currentRevision`
    （`core/input/character_routes.py`），即"我不知道当前版本"——这比给一个错的版本号诚实：
    客户端据此走"重新拉取再重试"而不是"拿这个号重试"。
    """
    svc = _service()
    character_id = await _create(svc)
    await _install_cas_loser(svc, monkeypatch)
    original_get = svc.repo.get_definition

    async def _unreadable(cid, *, fresh: bool = False):
        # 只打断**绕过缓存的回读**：`update` 开头的普通读仍要能工作，否则用例会在更早的
        # 地方就失败，测不到"回读失败 → 报未知"这条分支。
        if fresh:
            raise StorageUnavailableError("注入：回读也失败")
        return await original_get(cid)

    with pytest.raises(RevisionConflictError) as excinfo:
        monkeypatch.setattr(svc.repo, "get_definition", _unreadable)
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert excinfo.value.current_revision is None


@pytest.mark.asyncio
async def test_update_cas_storage_failure_compensates_when_pointer_unmoved(monkeypatch):
    """CAS **抛异常**且回读证明指针没动 → **有补偿**，不留孤儿（4 号缺陷，已修）。

    修前：只有 `ok is False` 分支补偿，抛异常那条路径直接漏过 → revision 2 已 durable
    而指针仍指向 1，孤儿永驻（`resolve_runtime_source(cid, 2)` 能解析到从未发布过的版本、
    `list_revisions` 把它当历史展示）。

    修后：判据从"有没有抛"改成"**指针到哪了**"——回读 == expected 即"确定没发布"，
    补偿删除安全；随后**原始异常照常上抛**（调用方仍需知道这次写失败了）。
    """
    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    log.spy(
        monkeypatch, svc.repo, "cas_revision_pointer", CAS,
        raises=StorageUnavailableError("注入：CAS 存储失败"),
    )

    with pytest.raises(StorageUnavailableError):
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert log.names == [APPEND, CAS, DELETE]            # 抛异常路径也补偿了
    assert log.args_for(DELETE) == [(character_id, 2)]
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 1    # 指针未动
    assert await svc.rev_repo.max_revision(character_id) == 1            # 孤儿已清
    assert await svc.rev_repo.get_revision(character_id, 2) is None
    revisions = await svc.list_revisions(character_id)
    assert [item.revision for item in revisions.items] == [1]


@pytest.mark.asyncio
async def test_update_cas_exception_with_advanced_pointer_is_treated_as_success(monkeypatch):
    """CAS 抛异常但回读显示指针**已到目标版本** → 视为成功（响应丢失，不是写失败）。

    这是"不猜着删"的另一半：驱动超时完全可能发生在**服务端已提交**之后。若此时按"抛异常
    = 没写成功"处理并删掉 revision，就会删掉一条**已发布**的版本（指针指向不存在的版本
    = 数据丢失，比孤儿严重得多）。故必须先回读再决定。
    """
    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    original_cas = svc.repo.cas_revision_pointer

    async def _commit_then_raise(character_id_, expected_revision, new_revision, updated_at):
        # 真实落库成功，然后"响应丢失"
        assert await original_cas(
            character_id_, expected_revision, new_revision, updated_at=updated_at)
        raise StorageUnavailableError("注入：写已提交但响应丢失")

    log.spy(monkeypatch, svc.repo, "cas_revision_pointer", CAS)
    monkeypatch.setattr(svc.repo, "cas_revision_pointer", _commit_then_raise)

    result = await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert log.args_for(DELETE) == [], "已发布的 revision 绝不能删"
    assert result.character.summary.currentRevision == 2
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 2
    assert await svc.rev_repo.get_revision(character_id, 2) is not None


@pytest.mark.asyncio
async def test_update_cas_exception_with_undecidable_pointer_keeps_orphan(monkeypatch, caplog):
    """回读失败（指针位置**无法判定**）→ 一个字节都不删，留孤儿 + error 日志。

    宁可泄漏不可误删：无法证明"没发布"时删掉 revision，就是把一次不确定变成一次可能的
    数据丢失。孤儿是可见、可对账的（`list_revisions`），日志里带 traceback。
    """
    import logging

    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    original_error = StorageUnavailableError("注入：CAS 存储失败")
    log.spy(monkeypatch, svc.repo, "cas_revision_pointer", CAS, raises=original_error)
    original_get = svc.repo.get_definition

    async def _unreadable(cid, *, fresh: bool = False):
        # 同上前提：只打断回读，普通读照常（否则连 expected_revision 校验都过不去）。
        if fresh:
            raise StorageUnavailableError("注入：回读也失败")
        return await original_get(cid)

    monkeypatch.setattr(svc.repo, "get_definition", _unreadable)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(StorageUnavailableError) as excinfo:
            await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert excinfo.value is original_error
    assert log.args_for(DELETE) == [], "无法判定时不得删——宁可留孤儿"
    assert await svc.rev_repo.get_revision(character_id, 2) is not None
    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "留孤儿必须留 error 级痕迹（否则静默）"


@pytest.mark.asyncio
async def test_update_compensation_failure_keeps_conflict_error(monkeypatch, caplog):
    """update 的补偿失败 → **409 照常抛出**（不被补偿异常顶替），孤儿留下并留痕。"""
    import logging

    svc = _service()
    character_id = await _create(svc)
    await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    compensation_error = StorageUnavailableError("注入：补偿删除失败")
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE, raises=compensation_error)
    await _install_rival_rollback(svc, monkeypatch)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RevisionConflictError) as excinfo:
            await svc.update(character_id, _draft(displayName="v3"), expected_revision=2)

    assert excinfo.value.current_revision == 1, "409 报真值，且不被善后失败顶替"
    assert log.args_for(DELETE) == [(character_id, 3)]
    assert await svc.rev_repo.get_revision(character_id, 3) is not None   # 孤儿（补偿未生效）
    assert any("补偿失败" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_update_success_orders_revision_before_pointer_and_never_compensates(monkeypatch):
    """成功路径：顺序为 revision → 指针 CAS，且补偿调用 0 次。"""
    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.repo, "cas_revision_pointer", CAS)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    result = await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert log.names == [APPEND, CAS]
    assert log.args_for(DELETE) == []
    assert log.args_for(APPEND)[0][0].revision == 2
    assert log.args_for(CAS) == [(character_id, 1, 2)]
    assert result.character.summary.currentRevision == 2
    assert await svc.rev_repo.get_revision(character_id, 2) is not None


# --------------------------------------------------------------------------- rollback


@pytest.mark.asyncio
async def test_rollback_cas_loss_keeps_revision_when_higher_version_exists(monkeypatch):
    """rollback 同 update：库中已有更高版本 → **不删**，历史保持连续。"""
    svc = _service()
    character_id = await _create(svc)
    await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)
    log = _WriteLog()
    append_original = log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    await _install_cas_loser(svc, monkeypatch, append_impl=append_original)

    with pytest.raises(RevisionConflictError):
        await svc.rollback(character_id, target_revision=1, expected_revision=2)

    assert log.names == [APPEND], "库中已有更高版本 → 不得补偿删除"
    assert log.args_for(DELETE) == []
    assert await svc.rev_repo.get_revision(character_id, 3) is not None   # 孤儿保留
    assert await svc.rev_repo.get_revision(character_id, 4) is not None   # 并发写者的版本
    revisions = await svc.list_revisions(character_id)
    assert sorted(item.revision for item in revisions.items) == [1, 2, 3, 4], \
        "历史必须连续——缺格会让 reconcile 永久失败"
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 4
    assert definition.currentRevision != 2            # 指针没有被回滚重写的旧值覆盖


@pytest.mark.asyncio
async def test_rollback_cas_storage_failure_compensates_when_pointer_unmoved(monkeypatch):
    """rollback 同 update：CAS 抛异常但回读证明指针没动 → 补偿删除，不留孤儿。

    两条链共用 `_commit_revision_pointer`——这条用例的存在是为了防止将来有人只改
    `update` 那条而把 `rollback` 落在后面（本项目反复出现的「两条路径不对称」）。
    """
    svc = _service()
    character_id = await _create(svc)
    await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    log.spy(
        monkeypatch, svc.repo, "cas_revision_pointer", CAS,
        raises=StorageUnavailableError("注入：CAS 存储失败"),
    )

    with pytest.raises(StorageUnavailableError):
        await svc.rollback(character_id, target_revision=1, expected_revision=2)

    assert log.names == [APPEND, CAS, DELETE]
    assert log.args_for(DELETE) == [(character_id, 3)]
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 2
    assert await svc.rev_repo.max_revision(character_id) == 2
    revisions = await svc.list_revisions(character_id)
    assert [item.revision for item in revisions.items] == [2, 1]


@pytest.mark.asyncio
async def test_rollback_success_orders_revision_before_pointer_and_never_compensates(monkeypatch):
    """成功路径：顺序为 revision → 指针 CAS，补偿调用 0 次，内容确实回到目标版本。"""
    svc = _service()
    character_id = await _create(svc)
    await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.repo, "cas_revision_pointer", CAS)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    result = await svc.rollback(character_id, target_revision=1, expected_revision=2)

    assert log.names == [APPEND, CAS]
    assert log.args_for(DELETE) == []
    assert log.args_for(APPEND)[0][0].revision == 3
    assert log.args_for(CAS) == [(character_id, 2, 3)]
    assert result.character.summary.currentRevision == 3
    got = await svc.get(character_id)
    assert got is not None and got.summary.displayName == "写链角色"   # 回到 revision 1 的内容


# --------------------------------------------------------------------------- clone


@pytest.mark.asyncio
async def test_clone_definition_write_failure_compensates_the_new_revision(monkeypatch):
    """clone 无自有写入（整条链委托 create）：失败时补偿**新角色**的 revision 1，源角色不受影响。"""
    svc = _service()
    source_id = await _create(svc)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(
        monkeypatch, svc.repo, "insert_definition", INSERT,
        raises=StorageUnavailableError("注入：characters 写失败"),
    )
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    with pytest.raises(StorageUnavailableError):
        await svc.clone(source_id, display_name="克隆体")

    assert log.names == [APPEND, INSERT, DELETE]
    cloned_id = log.args_for(APPEND)[0][0].characterId
    assert cloned_id != source_id
    assert log.args_for(DELETE) == [(cloned_id, 1)]
    assert await svc.rev_repo.get_revision(cloned_id, 1) is None
    assert await svc.get(cloned_id) is None
    source = await svc.get(source_id)
    assert source is not None and source.summary.characterId == source_id
    assert await svc.rev_repo.max_revision(source_id) == 1


@pytest.mark.asyncio
async def test_clone_success_never_compensates(monkeypatch):
    """成功路径：新角色 revision 1 落库，补偿调用 0 次，源角色 revision 不变。"""
    svc = _service()
    source_id = await _create(svc)
    log = _WriteLog()
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    detail = await svc.clone(source_id, display_name="克隆体")

    assert log.args_for(DELETE) == []
    cloned_id = detail.summary.characterId
    assert cloned_id != source_id
    assert await svc.rev_repo.get_revision(cloned_id, 1) is not None
    assert await svc.rev_repo.max_revision(source_id) == 1


# ------------------------------------------- update/rollback 的实例传播（第 3+ 次写）


def _instance_service() -> tuple[CharacterService, CharacterInstanceRepository]:
    """带 instance 仓储的服务——让 update/rollback 走完整的「定义 → 实例传播」写链。"""
    instance_repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    return (
        CharacterService(
            repo=CharacterRepository(db=FakeDocumentDBBackend()),
            rev_repo=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
            catalog=None,
            user_identity_store=UserIdentityStore(),
            instance_repo=instance_repo,
        ),
        instance_repo,
    )


async def _seed_follow_instance(
    svc: CharacterService, instance_repo: CharacterInstanceRepository, character_id: str
) -> None:
    """给角色挂一个 follow_latest 实例（传播链的目标）。"""
    await instance_repo.save(
        CharacterInstance(
            instanceId="chi_follow",
            characterId=character_id,
            revision=1,
            revisionPolicy="follow_latest",
            stateVersion=1,
        )
    )


@pytest.mark.asyncio
async def test_update_instance_propagation_failure_is_pending_not_compensated(monkeypatch):
    """第 3+ 次写（instance 传播）失败：白名单异常被吞成 pendingInstances——**不补偿、不回滚**。

    instances.py:236-266 `_propagate_revision` 捕获
    (CharacterBusyError / CharacterStateConflictError / StorageUnavailableError) → 记 warning
    + 计 pending，既不向调用方抛错，也不回滚已提交的定义指针。这是**刻意的最终一致性**：
    恢复机制是 `reconcile_instance_revision` 的懒对账（runtime_loader 也走它），
    前提是 pending 标记已落库——本用例把这一点也钉住。
    """
    svc, instance_repo = _instance_service()
    character_id = await _create(svc)
    await _seed_follow_instance(svc, instance_repo, character_id)
    log = _WriteLog()
    log.spy(monkeypatch, instance_repo, "set_pending_revision", "instance.set_pending_revision")
    log.spy(
        monkeypatch, instance_repo, "cas_apply_revision", "instance.cas_apply_revision",
        raises=StorageUnavailableError("注入：instance CAS 失败"),
    )
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    result = await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert "instance.set_pending_revision" in log.names     # 传播确实走到了 instance
    assert log.args_for(DELETE) == []                       # 定义写链不回滚已提交的 revision
    assert result.stateImpact.affectedInstances == 1
    assert result.stateImpact.pendingInstances == 1
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 2   # 指针已提交
    staged = await instance_repo.get("chi_follow")
    assert staged is not None and staged.pendingRevision == 2           # 恢复标记在


@pytest.mark.asyncio
async def test_update_instance_propagation_unlisted_error_reaches_caller_after_commit(monkeypatch):
    """**当前契约**：传播抛**白名单之外**的异常时，调用方看到失败，但定义指针已提交。

    `_propagate_revision` 只捕三种异常；其它（如 `reconcile_instance_revision` 在定义
    缺失时抛的 `CharacterNotFoundError`，instances.py:108）会直接冒泡出 `update`——
    此时第 1、2 次写已成功：revision 2 与指针 durable，调用方却收到「写失败」。
    错误信号与实际已提交的数据不一致（路由层会把这它当 500/503 返回）。
    幸存状态仍可恢复：pending 标记已落库，懒对账会补齐 instance。
    """
    svc, instance_repo = _instance_service()
    character_id = await _create(svc)
    await _seed_follow_instance(svc, instance_repo, character_id)
    log = _WriteLog()
    log.spy(
        monkeypatch, instance_repo, "cas_apply_revision", "instance.cas_apply_revision",
        raises=CharacterNotFoundError("chi_follow"),
    )

    with pytest.raises(CharacterNotFoundError):
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert log.args_for("instance.cas_apply_revision") != []
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 2   # 已提交，非回滚
    assert await svc.rev_repo.get_revision(character_id, 2) is not None
    staged = await instance_repo.get("chi_follow")
    assert staged is not None and staged.pendingRevision == 2


# --------------------------------------------------------------------------- archive


async def _make_default(svc: CharacterService, store: UserIdentityStore) -> str:
    """建一个 custom 角色并设为默认角色（手法同 test_archive_consistency.py）。"""
    store.bootstrap("妖梦")
    character_id = await _create(svc)
    outcome = await UserIdentityStore.update_if_revision(
        store, 1, {"defaultCharacterId": character_id, "initialized": True}
    )
    assert outcome.identity is not None
    return character_id


@pytest.mark.asyncio
async def test_archive_repo_failure_keeps_partial_default_retarget(monkeypatch):
    """**当前契约**：archive 是两集合写但**没有补偿**——profile 已改指而归档失败时半程写入保留。

    definitions.py:215-216 的顺序是「先改 defaultCharacterId → 再 archive 角色」，
    `repo.archive` 抛错时异常照原样冒泡、**不**回滚 profile：角色仍 active，但默认角色
    已经变成 `chr_builtin_elysia`。方向是安全的（不会留下指向已归档角色的悬空引用），
    但确实是一次部分写入——`test_archive_consistency.py` 只钉了反方向（profile 失败 →
    角色保持 active），本用例补上「profile 成功 + archive 失败」这一向。
    """
    store = UserIdentityStore()
    svc = _service(store=store)
    character_id = await _make_default(svc, store)
    log = _WriteLog()
    log.spy(monkeypatch, store, "update_if_revision", IDENTITY)
    log.spy(
        monkeypatch, svc.repo, "archive", ARCHIVE,
        raises=StorageUnavailableError("注入：characters 归档写失败"),
    )
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)

    with pytest.raises(StorageUnavailableError):
        await svc.archive(character_id)

    assert log.names == [IDENTITY, ARCHIVE]        # 没有回滚/补偿调用
    assert len(log.args_for(IDENTITY)) == 1        # profile 只写了一次，没有被改回
    assert log.args_for(IDENTITY)[0][1] == {"defaultCharacterId": BUILTIN_FALLBACK_CHARACTER_ID}
    assert log.args_for(DELETE) == []
    profile = store.get()
    assert profile is not None
    assert profile.defaultCharacterId == BUILTIN_FALLBACK_CHARACTER_ID   # 半程写入保留
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.status == "active"     # 角色未归档


@pytest.mark.asyncio
async def test_archive_success_writes_only_identity_and_status(monkeypatch):
    """archive 成功路径：只写 profile + characters 状态，不碰 revision 集合、不触发补偿。"""
    store = UserIdentityStore()
    svc = _service(store=store)
    character_id = await _make_default(svc, store)
    log = _WriteLog()
    log.spy(monkeypatch, store, "update_if_revision", IDENTITY)
    log.spy(monkeypatch, svc.repo, "archive", ARCHIVE)
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    log.spy(monkeypatch, svc.repo, "insert_definition", INSERT)

    result = await svc.archive(character_id)

    assert log.names == [IDENTITY, ARCHIVE]
    assert log.args_for(APPEND) == []
    assert log.args_for(DELETE) == []
    assert log.args_for(INSERT) == []
    assert result.character.status.value == "archived"
    assert result.userProfile is not None
    assert result.userProfile.defaultCharacterId == BUILTIN_FALLBACK_CHARACTER_ID
