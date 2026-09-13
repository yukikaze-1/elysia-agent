# 测试文件：core/storage/tests/test_duplicate_key_non_fatal.py
"""良性并发冲突不得被判成存储故障（S2-B2）。

`Repository._call` 对未命中 `non_fatal` 的**任何**异常都 `_mark_unavailable()`
（置 `_db = None`）+ 包装成 `StorageUnavailableError`。而 `DuplicateKeyError` 是
**并发竞争的正常信号**（唯一索引在正确工作）——被判成存储故障后：

- 本应返回 409 的竞争变成 503；
- **该仓储此后所有读写都 raise，直到 60s 健康轮重连**（附件/身份仓储影响全站）。

三个角色/身份仓储此前漏加 `non_fatal`（`idempotency_repo` 早已有）。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.storage.base import StorageUnavailableError, is_duplicate_key

pytestmark = pytest.mark.unit


class DuplicateKeyError(Exception):   # noqa: N818
    """**类名必须与驱动一致**——`is_duplicate_key` 按类名匹配（驱动无关）。

    这里刻意不加下划线前缀：谓词查的是 `type(exc).__name__ == "DuplicateKeyError"`，
    改成 `DuplicateKeyError` 就匹配不上（实现期真实踩到过）。
    """


def test_is_duplicate_key_matches_direct_and_cause():
    assert is_duplicate_key(DuplicateKeyError()) is True

    class _Wrapped(Exception):
        pass

    exc = _Wrapped("wrap")
    exc.__cause__ = DuplicateKeyError()
    assert is_duplicate_key(exc) is True, "包装层也要能识别（__cause__ 链）"
    assert is_duplicate_key(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_non_fatal_does_not_kill_repository():
    """命中 `non_fatal` → 不判死仓储、不包装，裸抛原始异常。"""
    from core.storage.base import Repository

    class _Repo(Repository):
        async def connect(self) -> bool:
            return True

        async def close(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

    repo = _Repo()
    db = MagicMock()
    repo._db = db                                    # noqa: SLF001
    repo._injected_db = False                        # noqa: SLF001

    with pytest.raises(DuplicateKeyError):
        await repo._call(AsyncMock(side_effect=DuplicateKeyError("E11000")),  # noqa: SLF001
                         reason="写入失败", non_fatal=is_duplicate_key)

    assert repo.backend is db, "良性冲突不得让仓储失去连接（否则 60s 内全不可用）"


@pytest.mark.asyncio
async def test_without_non_fatal_marks_unavailable():
    """对照：不加 `non_fatal` 时确实是判死语义——这正是三个仓储此前的问题。"""
    from core.storage.base import Repository

    class _Repo(Repository):
        async def connect(self) -> bool:
            return True

        async def close(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

    repo = _Repo()
    repo._db = MagicMock()                           # noqa: SLF001
    repo._injected_db = False                        # noqa: SLF001

    with pytest.raises(StorageUnavailableError):
        await repo._call(AsyncMock(side_effect=DuplicateKeyError("E11000")),  # noqa: SLF001
                         reason="写入失败")

    assert repo.backend is None, "未登记 non_fatal → 判死（S2-B2 的原行为）"


@pytest.mark.asyncio
async def test_character_revision_repo_treats_conflict_as_benign(caplog):
    """接线：角色 revision 仓储真的把 DuplicateKey 当良性冲突（不判死）。"""
    import logging

    from core.storage.character_revision_repo import CharacterRevisionRepository

    repo = CharacterRevisionRepository(db=MagicMock())
    # 注入型 db 会跳过 `_mark_unavailable`（测试容错），故显式关掉——
    # 生产仓储是**非注入**路径，判死语义只在那条路径上存在。
    repo._injected_db = False                        # noqa: SLF001
    db = MagicMock()                                 # 捕获原引用（判死后 _db 会变 None）
    repo._db = db                                    # noqa: SLF001
    db.insert_one = AsyncMock(side_effect=DuplicateKeyError("E11000"))
    revision = MagicMock()
    revision.characterId = "chr_x"
    revision.revision = 2

    with caplog.at_level(logging.INFO):
        with pytest.raises(DuplicateKeyError):
            await repo.append_revision(revision)

    assert repo.backend is db, "良性冲突不得判死仓储"
    assert any("并发冲突（良性）" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_character_repo_treats_conflict_as_benign(caplog):
    """接线：角色定义仓储并发创建同 id → 记 info 后照常返回（不 503）。"""
    import logging

    from core.storage.character_repo import CharacterRepository

    repo = CharacterRepository(db=MagicMock())
    # 注入型 db 会跳过 `_mark_unavailable`（测试容错），故显式关掉——
    # 生产仓储是**非注入**路径，判死语义只在那条路径上存在。
    repo._injected_db = False                        # noqa: SLF001
    # **先捕获原引用**：判死时 `_db` 会被置 None，若断言写成
    # `repo.backend is repo._db` 就成了 `None is None` —— 恒真、什么都测不到。
    db = MagicMock()
    repo._db = db                                    # noqa: SLF001
    db.insert_one = AsyncMock(side_effect=DuplicateKeyError("E11000"))
    definition = MagicMock()
    definition.characterId = "chr_x"

    with caplog.at_level(logging.INFO):
        await repo.insert_definition(definition)     # 不抛

    # **关键断言**：仓储不得被判死。注意 `is_duplicate_key` 会查 `__cause__` 链，
    # 所以即使去掉 non_fatal、`_call` 把它包成 StorageUnavailableError，上面那句
    # `is_duplicate_key(exc)` 仍然是 True、方法仍然不抛——只断言"不抛"测不到任何
    # 东西（反证第一次就是这么假绿的）。真正的损害是 `_mark_unavailable()`：
    # 该仓储此后所有读写都 raise，直到 60s 健康轮重连。
    assert repo.backend is db, "良性冲突不得判死仓储"


@pytest.mark.asyncio
async def test_user_identity_repo_bootstrap_survives_conflict():
    """接线：并发 bootstrap 撞唯一索引 → 不判死（身份仓储不可用会让 /ready 503）。"""
    from core.storage.user_identity_repo import UserIdentityRepository

    repo = UserIdentityRepository(db=MagicMock())
    # 注入型 db 会跳过 `_mark_unavailable`（测试容错），故显式关掉——
    # 生产仓储是**非注入**路径，判死语义只在那条路径上存在。
    repo._injected_db = False                        # noqa: SLF001
    db = MagicMock()                                 # 捕获原引用（判死后 _db 会变 None）
    repo._db = db                                    # noqa: SLF001
    db.update_one = AsyncMock(side_effect=DuplicateKeyError("E11000"))
    repo.get = AsyncMock(return_value={"user_id": "usr_x", "revision": 1})

    async def _get(uid):
        return {"user_id": uid, "revision": 1}

    repo.get = _get                                 # type: ignore[assignment]
    identity = MagicMock()
    identity.userId = "usr_x"

    result = await repo.bootstrap_if_absent(identity)
    assert result is not None, "并发 loser 必须能读回 winner"
    assert repo.backend is db, "良性冲突不得判死身份仓储"
