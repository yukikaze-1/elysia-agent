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

标注「**当前契约**」的用例钉的是**未修**行为（孤儿 revision、补偿自身失败时的异常
顶替、archive 半程写入、409 里的过期 currentRevision）。它们变红 = 行为被改动，
应连同本文件一起更新，而不是把它们当成期望值。
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
async def test_create_compensation_failure_masks_original_error_with_orphan(monkeypatch):
    """**当前契约**：补偿调用没有保护——它自己失败时顶替原始异常，孤儿 revision 留下。

    definitions.py:108-110 的补偿只有 `await self.rev_repo.delete_revision(...)` 一行，
    没有 try/except：补偿抛 `StorageUnavailableError` 时
      ① 调用方看到的是**补偿失败**（原始成因「insert_definition 失败」被掩盖，
         异常链上也没有 `__cause__` 指向它）；
      ② revision (cid, 1) 留在库里，而 characters 里没有对应 definition → 孤儿行。
    """
    svc = _service()
    log = _WriteLog()
    original_error = StorageUnavailableError("注入：characters 写失败")
    compensation_error = StorageUnavailableError("注入：补偿删除也失败")
    log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.repo, "insert_definition", INSERT, raises=original_error)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE, raises=compensation_error)

    with pytest.raises(StorageUnavailableError) as excinfo:
        await svc.create(_draft(), owner_user_id="usr_local_default")

    assert excinfo.value is compensation_error           # 补偿异常顶替原始异常
    assert excinfo.value is not original_error
    character_id = log.args_for(APPEND)[0][0].characterId
    assert await svc.rev_repo.get_revision(character_id, 1) is not None   # 孤儿行
    assert await svc.repo.get_definition(character_id) is None
    assert await svc.get(character_id) is None


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
async def test_update_cas_loss_compensates_appended_revision(monkeypatch):
    """append 成功、CAS 落败（并发写者先提交）→ **有补偿**：删掉本次 append 的 revision。

    幸存状态必须一致：并发写者的 revision 3 生效可读，我们未发布的 revision 2 被清掉。
    """
    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    append_original = log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    await _install_cas_loser(svc, monkeypatch, append_impl=append_original)

    with pytest.raises(RevisionConflictError):
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert log.names == [APPEND, DELETE]
    assert log.args_for(DELETE) == [(character_id, 2)]
    assert await svc.rev_repo.get_revision(character_id, 2) is None
    assert await svc.rev_repo.get_revision(character_id, 3) is not None
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 3
    got = await svc.get(character_id)
    assert got is not None and got.summary.displayName == "并发对手"


@pytest.mark.asyncio
async def test_update_cas_loss_reports_stale_current_revision(monkeypatch):
    """**当前契约（真实缺陷，未修）**：CAS 落败时 409 里的 currentRevision 是**过期值**。

    definitions.py:151 用 `definition.currentRevision`（本次调用开头读到的内存值）构造
    `RevisionConflictError`——而 CAS 落败恰恰意味着库里已经**不是**这个值。路由层把它
    原样放进 409 body（core/input/character_routes.py:84-91
    `{"currentRevision": exc.current_revision}`），于是调用方被告知「当前版本 = 1」，
    而仓库里其实是 3（最小复现：并发写者把指针推到 3 后，本调用的 409 仍报 1）。
    """
    svc = _service()
    character_id = await _create(svc)
    await _install_cas_loser(svc, monkeypatch)

    with pytest.raises(RevisionConflictError) as excinfo:
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 3   # 真实当前版本
    assert excinfo.value.current_revision == 1                          # 报告的是过期值


@pytest.mark.asyncio
async def test_update_cas_storage_failure_leaves_orphan_revision(monkeypatch):
    """**当前契约（数据完整性隐患，未修）**：CAS **抛异常**时没有补偿。

    definitions.py:146-151 只在 `ok is False` 分支补偿；`cas_revision_pointer` 抛出的
    路径绕过它（真实现：character_repo.py:163-164 未连接即抛 StorageUnavailableError，
    `Repository._call` 也会把驱动异常包装后抛，core/storage/base.py:227）。结果：
    revision 2 已 durable 而 Definition 指针仍指向 1 → 孤儿 revision 永驻：

      - `resolve_runtime_source(cid, 2)` 能解析到一个从未发布过的版本；
      - `list_revisions` 把它当历史版本展示（用户可见的、没发布过的版本）；
      - 下一次 update 从 max_revision()+1=3 继续（跳号，不覆盖，故不会静默丢数据）。
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

    assert log.names == [APPEND, CAS]                  # 没有补偿调用
    assert log.args_for(DELETE) == []
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 1    # 指针未动
    assert await svc.rev_repo.max_revision(character_id) == 2            # 孤儿 revision 2
    orphan = await svc.rev_repo.get_revision(character_id, 2)
    assert orphan is not None and orphan.displayName == "v2"
    revisions = await svc.list_revisions(character_id)
    assert [item.revision for item in revisions.items] == [2, 1]         # 未发布却可见


@pytest.mark.asyncio
async def test_update_compensation_failure_masks_conflict_and_leaves_revision(monkeypatch):
    """**当前契约**：update 的补偿同样没有保护——它失败时 409 被顶替、revision 留在库里。"""
    svc = _service()
    character_id = await _create(svc)
    log = _WriteLog()
    append_original = log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    compensation_error = StorageUnavailableError("注入：补偿删除失败")
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE, raises=compensation_error)
    await _install_cas_loser(svc, monkeypatch, append_impl=append_original)

    with pytest.raises(StorageUnavailableError) as excinfo:
        await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)

    assert excinfo.value is compensation_error                    # RevisionConflictError 被顶替
    assert log.args_for(DELETE) == [(character_id, 2)]
    assert await svc.rev_repo.get_revision(character_id, 2) is not None   # 补偿未生效


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
async def test_rollback_cas_loss_compensates_appended_revision(monkeypatch):
    """rollback 的 CAS 落败 → **有补偿**：删掉本次 append 的 revision 3，409 冒泡。"""
    svc = _service()
    character_id = await _create(svc)
    await svc.update(character_id, _draft(displayName="v2"), expected_revision=1)
    log = _WriteLog()
    append_original = log.spy(monkeypatch, svc.rev_repo, "append_revision", APPEND)
    log.spy(monkeypatch, svc.rev_repo, "delete_revision", DELETE)
    await _install_cas_loser(svc, monkeypatch, append_impl=append_original)

    with pytest.raises(RevisionConflictError):
        await svc.rollback(character_id, target_revision=1, expected_revision=2)

    assert log.names == [APPEND, DELETE]
    assert log.args_for(DELETE) == [(character_id, 3)]
    assert await svc.rev_repo.get_revision(character_id, 3) is None
    assert await svc.rev_repo.get_revision(character_id, 4) is not None
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 4
    assert definition.currentRevision != 2            # 指针没有被回滚重写的旧值覆盖


@pytest.mark.asyncio
async def test_rollback_cas_storage_failure_leaves_orphan_revision(monkeypatch):
    """**当前契约（同 update）**：CAS 抛异常 → 无补偿，孤儿 revision 3 永驻。"""
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

    assert log.names == [APPEND, CAS]
    assert log.args_for(DELETE) == []
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.currentRevision == 2
    assert await svc.rev_repo.max_revision(character_id) == 3
    revisions = await svc.list_revisions(character_id)
    assert [item.revision for item in revisions.items] == [3, 2, 1]


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
    updated = await UserIdentityStore.update_if_revision(
        store, 1, {"defaultCharacterId": character_id, "initialized": True}
    )
    assert updated is not None
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
