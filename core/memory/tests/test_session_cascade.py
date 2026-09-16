"""#21b：会话删除的级联范围（`token_usage` / `generations` / `character_versions`）。

缺陷形状：删会话已删 session doc + `message_archive` + 释放附件 refs，但
`token_usage`、`generations`（两个 `delete_by_session` 早已写好、**零生产调用**）
与 `character_versions`（按 session_id 分区，连删除方法都没有）留了下来——这些行
在删除后**不可达**（所有读路径都要 session_id），只会永久占库。

本文件的两道守卫：
1. **覆盖守卫**：生产清单必须包含**所有**带 `delete_by_session` 的仓储——将来新增
   一个会话域集合却忘了接线 → 红（否则"孤儿行"要等下次数据审计才发现）。
2. **行为守卫**：删除会话时清单里每个仓储都真的被调用（含失败重试路径）。
"""

import asyncio
import pathlib

import pytest

from core.memory.session_manager import SessionManager
from core.schema.types import SessionId

pytestmark = pytest.mark.service

from core.memory.tests.test_session_manager import (  # noqa: E402
    FakeMsgRepo,
    FakeSessionRepo,
)


class _FakeCascadeRepo:
    """带 delete_by_session 的会话域仓储 fake。"""

    def __init__(self, label: str, *, fail: bool = False) -> None:
        self.label = label
        self.calls: list[str] = []
        self._fail = fail

    async def delete_by_session(self, sid) -> int:
        self.calls.append(sid)
        if self._fail:
            raise RuntimeError(f"{self.label} 离线")
        return 1


def _manager(*, cascade=None):
    return SessionManager(FakeSessionRepo(), FakeMsgRepo(),
                          cascade_repos=cascade or [])


class TestCascadeCoverageGuard:
    """覆盖守卫：所有带 `delete_by_session` 的仓储都必须在生产清单里。

    判据来源是**代码本身**（AST/反射扫 `core/storage`），不是手写名单——手写名单会
    随新增集合而陈旧，正是本项要消除的那类"忘了接线"。
    """

    @staticmethod
    def _repos_with_delete_by_session() -> set[str]:
        """扫 core/storage 下所有定义了 delete_by_session 的仓储类名。"""
        import ast

        out: set[str] = set()
        for path in sorted(pathlib.Path("core/storage").glob("*_repo.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for item in node.body:
                    if (isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef))
                            and item.name == "delete_by_session"):
                        out.add(node.name)
        return out

    def test_every_session_scoped_repo_is_wired_into_the_cascade(self):
        """判据 = "有没有 delete_by_session"，覆盖两条接线通道。

        两条通道都是**必须**的，故这里都要断言：
        ① `cascade_repos` 清单（token_usage / generations / character_versions）；
        ② `msg_repo` 专用通道（`message_archive` 的删除有自己的语义：它的返回计数
           被 delete 的返回值使用，且排在附件 refs 释放之后）。
        """
        from core.agent import Agent
        from core.config import AppConfig

        agent = Agent(config=AppConfig())
        manager = agent._session_manager  # noqa: SLF001
        wired = {type(repo).__name__ for _label, repo in manager._cascade_repos}  # noqa: SLF001
        wired.add(type(manager._msg_repo).__name__)  # noqa: SLF001

        expected = self._repos_with_delete_by_session()
        missing = sorted(expected - wired)
        assert not missing, (
            f"这些仓储有 delete_by_session 但没接进会话删除级联：{missing}\n"
            f"  已接线：{sorted(wired)}\n"
            f"  新增会话域集合必须接进 `core/agent/agent.py` 的 cascade_repos 清单"
            f"（否则删会话会留下永久不可达的孤儿行）")

    def test_scan_actually_finds_repos(self):
        """反空转：扫描器必须真扫到东西，否则上一条是空转的绿。"""
        found = self._repos_with_delete_by_session()
        assert {"MessageRepository", "TokenRepository",
                "GenerationRepository", "CharacterVersionRepository"} <= found, found

    def test_character_version_repo_exposes_delete_by_session(self):
        """#21b 的原缺口：`character_versions` 此前连删除方法都没有。"""
        from core.storage.character_version_repo import CharacterVersionRepository

        assert hasattr(CharacterVersionRepository, "delete_by_session")


class TestCascadeBehavior:
    """行为守卫：删除会话时清单里每个仓储都被调用（幂等、失败可重试）。"""

    def test_delete_session_calls_every_cascade_repo(self):
        a = _FakeCascadeRepo("token_usage")
        b = _FakeCascadeRepo("generations")
        m = _manager(cascade=[("token_usage", a), ("generations", b)])

        asyncio.run(m.delete(SessionId("sid-1")))

        assert a.calls == ["sid-1"], "第一个级联仓储必须被调用"
        assert b.calls == ["sid-1"], "第二个级联仓储必须被调用"

    def test_failed_cascade_registers_pending_cleanup_for_retry(self):
        """级联失败 → 登记待重试（doc 已删，孤儿行下次补删），不静默丢。"""
        bad = _FakeCascadeRepo("character_versions", fail=True)
        m = _manager(cascade=[("character_versions", bad)])

        asyncio.run(m.delete(SessionId("sid-2")))

        assert "sid-2" in m._pending_cleanup, "级联失败必须进 journal 重试"  # noqa: SLF001

    def test_retry_pending_cleanup_cascades_again(self):
        """重试路径同样级联（否则 journal 里那条永远清不掉）。"""
        bad = _FakeCascadeRepo("character_versions", fail=True)
        m = _manager(cascade=[("character_versions", bad)])
        asyncio.run(m.delete(SessionId("sid-3")))
        assert "sid-3" in m._pending_cleanup  # noqa: SLF001

        bad._fail = False
        asyncio.run(m.retry_pending_cleanup())

        assert bad.calls.count("sid-3") == 2, f"重试应再调一次：{bad.calls}"
        assert "sid-3" not in m._pending_cleanup  # noqa: SLF001

    def test_no_cascade_repos_is_not_an_error(self):
        """反空转的另一半：清单为空时删除照常成功（不是靠清单才跑得通）。"""
        m = _manager()
        asyncio.run(m.delete(SessionId("sid-4")))
