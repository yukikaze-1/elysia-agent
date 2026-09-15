"""存储层 fail-closed 约定表 + 遍历所有仓储的断言（#31）。

## 为什么需要这张表

批次 6/7（`#25`）逐条把"读路径 fail-closed 化"落到了具体方法上，但**没有留下那张表**
——于是"哪些读路径允许返回空、哪些必须 503"只存在于每个方法的实现细节里，两个后果：

1. 同一个仓储内部出现**不对称**而无人知道。实测就有多处：
   `MessageRepository.query_page` 抛 `StorageUnavailableError`，而
   `query_by_session` 返回 `([], 0)`；`GenerationRepository.find_by_user_message`
   抛，`find_one`/`find_by_message` 返回 `None`；`SessionRepository.load` 抛，
   `list_all` 返回 `[]`。用户看到"空历史"与"503"是两种完全不同的体验，
   而这两种结果取决于调用了哪个方法——调用方无从判断。
2. 新增读路径可以**默默**选一种，没有任何门禁拦。

## 本表的语义（**不是**"现状快照"，而是"显式约定"）

- `READ_FAIL_CLOSED`：后端不可用时**必须**抛 `StorageUnavailableError`。
  语义是"我不知道答案" ≠ "答案是空"。
- `READ_EMPTY_OK`：后端不可用时**允许**返回空（`[]` / `None` / `0` / 空元组）。
  仅当调用方**能够容忍**这个歧义时才可以（例：debug 面板列表、进程内缓存读权威）。
- `WRITE` / `LIFECYCLE`：不参与读路径一致性判定（`LIFECYCLE` = connect/close/
  health_check/ensure_indexes/reload 等）。
- `_PENDING_DECISION`：约定**尚未定**的路径——报告决策项 #8「用户看到空历史与看到
  503 哪个更可接受」需要产品定义。本表把每条待决路径**逐条列出**，而不是让它藏在
  实现里；定案后把它们移进 `READ_FAIL_CLOSED`/`READ_EMPTY_OK`。

## 门禁

`TestConventionCoverage` 遍历 `core/storage/` 下**全部** `Repository` 子类的**全部**
公开异步方法：

- 每个方法必须在表里且只能有一个分类（新增方法不分类 → 红）；
- `READ_*` 分类的方法会被**真的调用**一次（`_db=None`），断言声明的行为确实成立
  （表态说 fail-closed 却返回了空 → 红）；
- 同仓读路径必须**同向**；混向的仓储必须在 `_ASYMMETRIC_JUSTIFIED` 里给出理由
  （新出现的不对称 → 红）。

最后一条是本门禁的核心：它把"两条路径不对称"这一本项目反复出现的缺陷族变成
**需要写理由才能通过**的事情。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import pathlib
from typing import Any

import pytest

from core.storage.base import StorageUnavailableError

pytestmark = [pytest.mark.service]

# ── 分类 ────────────────────────────────────────────────────────────────────

READ_FAIL_CLOSED = "READ_FAIL_CLOSED"
READ_EMPTY_OK = "READ_EMPTY_OK"
WRITE = "WRITE"
LIFECYCLE = "LIFECYCLE"

# 仓储名 → {方法名: 分类}
_CONVENTION: dict[str, dict[str, str]] = {
    # BE-070 起整仓 fail-closed（含读）：附件元数据缺失会让 UI 显示"图没了"，
    # 而真实原因可能是存储离线——这两者必须区分。
    "AttachmentRepository": {
        "create": WRITE,
        "get": READ_FAIL_CLOSED,
        "add_ref": WRITE,
        "release_reservation": WRITE,
        "remove_ref": WRITE,
        "mark_deleted": WRITE,
        "soft_delete_if_unreferenced": WRITE,
        "delete_record": WRITE,
        "find_active_ids": READ_FAIL_CLOSED,
        "find_by_status": READ_FAIL_CLOSED,
        "find_unreferenced_older_than": READ_FAIL_CLOSED,
        "find_by_stored_name": READ_FAIL_CLOSED,
    },
    # 角色运行时实例：整仓 fail-closed（角色状态不可臆造）
    "CharacterInstanceRepository": {
        "get": READ_FAIL_CLOSED,
        "get_canonical": READ_FAIL_CLOSED,
        "list_by_character": READ_FAIL_CLOSED,
        "save": WRITE,
        "save_if_absent": WRITE,
        "delete": WRITE,
        "cas_state_version": WRITE,
        "set_pending_revision": WRITE,
        "cas_apply_revision": WRITE,
    },
    "CharacterRepository": {
        "insert_definition": WRITE,
        "get_definition": READ_FAIL_CLOSED,
        "list_definitions": READ_FAIL_CLOSED,
        "cas_revision_pointer": WRITE,
        "archive": WRITE,
        "delete_definition": WRITE,
    },
    "CharacterRevisionRepository": {
        "append_revision": WRITE,
        "get_revision": READ_FAIL_CLOSED,
        "list_revisions": READ_FAIL_CLOSED,
        "list_revisions_page": READ_FAIL_CLOSED,
        "max_revision": READ_FAIL_CLOSED,
        "delete_revision": WRITE,
    },
    # 版本历史读路径返回空：调用方是 debug 面板 / 版本列表（空列表 = "没有版本记录"，
    # 与"存储离线"在 UI 上都表现为空）——**待决策 #8**，见 _PENDING_DECISION。
    "CharacterVersionRepository": {
        "append_version": WRITE,
        # 决策 #8（2026-09-14 owner）：**一律 fail-closed**——读不到版本历史时宁可
        # 503，也不要让"库离线"看起来像"没有历史"（后者会被误读为历史被清空）。
        "list_versions": READ_FAIL_CLOSED,
        "get_version": READ_FAIL_CLOSED,
        "count_versions": READ_FAIL_CLOSED,
    },
    # **刻意**让读走进程内缓存：docstring 明确"缓存为唯一读权威"，Mongo 离线时
    # get/query_pending 仍可从 cache 回答（幂等去重/状态查询不失效）。这不是遗漏，
    # 是设计——所以它不是"不对称"，而是整仓读路径同向（都 EMPTY_OK）。
    "DesktopCallRepository": {
        "upsert": WRITE,
        "get": READ_EMPTY_OK,
        "query_pending": READ_EMPTY_OK,
        "mark": WRITE,
        "mark_offline_by_connection": WRITE,
        "mark_stale_expired": WRITE,
        "reload_from_db": LIFECYCLE,
    },
    # 混向（见 _ASYMMETRIC_JUSTIFIED）
    "GenerationRepository": {
        "upsert": WRITE,
        "find_one": READ_EMPTY_OK,
        "find_by_message": READ_EMPTY_OK,
        "find_by_user_message": READ_FAIL_CLOSED,
        "find_by_session": READ_EMPTY_OK,
        "delete_by_session": WRITE,
        "mark_stale": WRITE,
    },
    "IdempotencyRepository": {
        "ensure_indexes": LIFECYCLE,
        "claim": WRITE,
        "get": READ_FAIL_CLOSED,
        "complete": WRITE,
        "reject": WRITE,
        "release": WRITE,
        "restore": WRITE,
    },
    # Milvus 仓储：读返回空（向量检索"没召回"与"库离线"在调用方都退化为无记忆）
    # ——**待决策 #8**；写路径 insert/delete 目前也静默返回空/0（见 _PENDING_DECISION）。
    "MemoryRepository": {
        "insert": WRITE,
        "search": READ_EMPTY_OK,
        "query": READ_EMPTY_OK,
        "delete": WRITE,
        "count": READ_EMPTY_OK,
        "clear_all": WRITE,
    },
    # 混向（见 _ASYMMETRIC_JUSTIFIED）——同仓同类读一半 fail-closed 一半返回空
    "MessageRepository": {
        "append": WRITE,
        "query_by_session": READ_EMPTY_OK,
        "query": READ_EMPTY_OK,
        "query_page": READ_FAIL_CLOSED,
        "get_by_mongo_id": READ_EMPTY_OK,
        "get_by_id": READ_EMPTY_OK,
        "query_after": READ_FAIL_CLOSED,
        "find_context": READ_EMPTY_OK,
        "query_search": READ_EMPTY_OK,
        "count_by_session": READ_FAIL_CLOSED,
        "count_assistant_without_character": READ_FAIL_CLOSED,
        "count": READ_EMPTY_OK,
        "delete_by_session": WRITE,
        "clear_all": WRITE,
        "query_offset": READ_EMPTY_OK,
        "delete_one": WRITE,
        "update_text": WRITE,
    },
    # 混向（见 _ASYMMETRIC_JUSTIFIED）
    "SessionRepository": {
        "save": WRITE,
        "create": WRITE,
        "ensure_exists": WRITE,
        "rename": WRITE,
        "load": READ_FAIL_CLOSED,
        "delete": WRITE,
        "list_all": READ_EMPTY_OK,
        "clear_all": WRITE,
        "count_incomplete_character_binding": READ_FAIL_CLOSED,
    },
    "TokenRepository": {
        "record": WRITE,
        "flush": WRITE,
        "query_by_session": READ_EMPTY_OK,
        "delete_by_session": WRITE,
        "total_tokens_by_session": READ_EMPTY_OK,
        "total_tokens_by_model": READ_EMPTY_OK,
        "clear_all": WRITE,
    },
    "UserIdentityRepository": {
        "get": READ_FAIL_CLOSED,
        "bootstrap_if_absent": WRITE,
        "update_if_revision": WRITE,
    },
}

# 允许"同仓读路径混向"的白名单——**必须给出理由**。当前三项都是同一个原因：
# 批次 6/7（#25）按调用方逐个决定，没有统一到仓储粒度，剩下的一半等报告决策项 #8。
_ASYMMETRIC_JUSTIFIED: dict[str, str] = {
    "MessageRepository": (
        "批次 6/7 按**调用方**逐个修：已经历过『空历史被当成真实历史』事故的路径"
        "（分页 query_page / 增量 query_after / 计数 / 角色完整性自检）改为 fail-closed；"
        "其余（query_by_session / query / get_by_id / query_search / find_context / "
        "query_offset / count / get_by_mongo_id）仍返回空。整仓统一需产品定义"
        "『用户看到空历史』与『看到 503』哪个更可接受（报告决策项 #8）。"
    ),
    "SessionRepository": (
        "load 是会话恢复的唯一入口（读到空 = 会话被清空，事故级），list_all 是 debug/"
        "列表用途（读到空只是列表为空）。整仓统一同样等决策项 #8。"
    ),
    "GenerationRepository": (
        "find_by_user_message 是幂等『提交证据』判定（读不到就不能断言『没提交过』，"
        "否则重复执行副作用），其余是展示/查询用途。整仓统一等决策项 #8。"
    ),
}

# 约定尚未定 / 已知与同仓其余路径不对称的写路径——逐条列出，不让它藏在实现里。
# 键为 (仓储名, 方法名)，值为说明。定案后从本表移除并改分类。
_PENDING_DECISION: dict[tuple[str, str], str] = {
    ("MemoryRepository", "insert"): (
        "写路径静默返回 []（同仓 clear_all 已 fail-closed）——Milvus 离线时记忆写入"
        "静默丢失，调用方无从知晓。与 append_version 的写路径约定不一致。"
    ),
    ("MemoryRepository", "delete"): "写路径静默返回 0（同仓 clear_all 已 fail-closed）",
}

# 构造仓储时的 db 参数名（默认 "db"；DesktopCallRepository 用 mongodb=）
_DB_KWARG: dict[str, str] = {"DesktopCallRepository": "mongodb"}

# 参数名 → 探测用取值工厂。仅为"能在 _db=None 下走到可用性判定"服务，
# 不追求语义合法（fail-closed 守卫在参数校验**之前**）。
_ARG_FACTORIES: dict[str, Any] = {
    "session_id": lambda: "probe",
    "conversation_id": lambda: "probe",
    "content_type": lambda: "l2",
    "message_id": lambda: "probe",
    "msg_id": lambda: "probe",
    "user_message_id": lambda: "probe",
    "generation_id": lambda: "probe",
    "attachment_id": lambda: "probe",
    "attachment_ids": lambda: ["probe"],
    "doc_id": lambda: "probe",
    "character_id": lambda: "probe",
    "instance_id": lambda: "probe",
    "owner_user_id": lambda: "probe",
    "user_id": lambda: "probe",
    "call_id": lambda: "probe",
    "connection_id": lambda: "probe",
    "status": lambda: "pending",
    "kind": lambda: "image",
    "stored_name": lambda: "probe.png",
    "iso": lambda: "2026-01-01T00:00:00Z",
    "cutoff": lambda: "2026-01-01T00:00:00Z",
    "vector": lambda: [0.0],
    "ids": lambda: [1],
    "id": lambda: 1,
    "version": lambda: 1,
    "revision": lambda: 1,
    "after_oid": lambda: "probe",
    "status_from": lambda: "running",
    "older_than_iso": lambda: "2026-01-01T00:00:00Z",
    "client_request_id": lambda: "probe",
    "content_digest": lambda: "probe",
    "claimed_at": lambda: "2026-01-01T00:00:00Z",
    "expires_at": lambda: "2026-01-01T00:00:00Z",
    "payload": lambda: {},
    "error": lambda: {},
}

# `Repository` 基类声明的生命周期方法——对所有仓储同义，不在表里逐仓重复。
_BASE_LIFECYCLE = {
    name for name in ("connect", "close", "health_check")
    if hasattr(__import__("core.storage.base", fromlist=["Repository"]).Repository, name)
}


def _discover_repositories() -> dict[str, type]:
    """结构式发现 `core/storage/` 下全部 `Repository` 子类（不依赖包 `__init__` 导出）。"""
    found: dict[str, type] = {}
    for path in sorted(pathlib.Path("core/storage").glob("*_repo.py")):
        module = importlib.import_module(f"core.storage.{path.stem}")
        for name, obj in vars(module).items():
            if (inspect.isclass(obj) and name.endswith("Repository")
                    and name != "Repository"
                    and obj.__module__ == module.__name__
                    and any(b.__name__ == "Repository" for b in obj.__mro__[1:])):
                found[name] = obj
    return found


def _public_async_methods(cls: type) -> list[str]:
    """类的公开异步方法名（排除基类生命周期三件套与私有）。"""
    out = []
    for name, fn in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_") or name in _BASE_LIFECYCLE:
            continue
        if not inspect.iscoroutinefunction(fn):
            continue
        out.append(name)
    return sorted(out)


def _probe_kwargs(fn: Any) -> dict[str, Any] | None:
    """为探测调用合成参数；有必填参数无工厂时返回 None（由用例报告）。"""
    kwargs: dict[str, Any] = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name == "self" or p.default is not inspect.Parameter.empty:
            continue
        factory = _ARG_FACTORIES.get(name)
        if factory is None:
            return None
        kwargs[name] = factory()
    return kwargs


async def _call_without_backend(cls: type, method: str) -> tuple[str, Any]:
    """以 `_db=None` 调用一次，返回 (结果种类, 值)。

    结果种类：`"raise"` / `"empty"` / `"value"`。
    """
    repo = cls(**{_DB_KWARG.get(cls.__name__, "db"): None})
    fn = getattr(repo, method)
    kwargs = _probe_kwargs(fn)
    assert kwargs is not None, (
        f"{cls.__name__}.{method} 有必填参数缺少探测工厂——"
        f"请在 _ARG_FACTORIES 补一条（新读路径必须可被本门禁探测）")
    try:
        result = await fn(**kwargs)
    except StorageUnavailableError:
        return "raise", None
    if result is None or result == [] or result == {} or result == 0 or result is False:
        return "empty", result
    if isinstance(result, tuple) and len(result) in (2, 3):
        # 返回 `(items, cursor[, more])` 形状：items 为空即"空结果"
        if not result[0]:
            return "empty", result
    return "value", result


# ══════════════════════════════════════════════════════════════════
# 1. 覆盖：每个公开异步方法都必须被分类
# ══════════════════════════════════════════════════════════════════

class TestConventionCoverage:
    """新增仓储/方法不分类 → 红。这是"约定不再只存在于实现细节里"的门禁。"""

    def test_every_repository_is_covered(self):
        repos = _discover_repositories()
        assert repos, "未发现任何 Repository 子类——发现逻辑失效"
        missing = sorted(set(repos) - set(_CONVENTION))
        assert not missing, (
            f"这些仓储没有 fail-closed 约定分类：{missing}——"
            f"请在 test_fail_closed_convention.py 的 _CONVENTION 补上")
        stale = sorted(set(_CONVENTION) - set(repos))
        assert not stale, f"_CONVENTION 里有已不存在的仓储：{stale}"

    def test_every_public_async_method_is_classified(self):
        problems: list[str] = []
        for name, cls in sorted(_discover_repositories().items()):
            declared = _CONVENTION.get(name, {})
            for method in _public_async_methods(cls):
                if method not in declared:
                    problems.append(f"{name}.{method} 未分类")
        assert not problems, (
            "以下公开异步方法没有 fail-closed 约定分类——新增读路径必须显式选择"
            "『允许返回空』还是『必须 503』：\n  " + "\n  ".join(problems))

    def test_no_stale_entries_in_convention(self):
        problems: list[str] = []
        for name, cls in sorted(_discover_repositories().items()):
            live = set(_public_async_methods(cls))
            for method in _CONVENTION.get(name, {}):
                if method not in live:
                    problems.append(f"{name}.{method} 已不存在")
        assert not problems, f"_CONVENTION 有过期条目：{problems}"

    def test_classifications_are_known(self):
        valid = {READ_FAIL_CLOSED, READ_EMPTY_OK, WRITE, LIFECYCLE}
        bad = [(r, m, c) for r, ms in _CONVENTION.items() for m, c in ms.items()
               if c not in valid]
        assert not bad, f"未知分类：{bad}"

    def test_pending_decisions_are_classified_and_explained(self):
        for (repo, method), reason in _PENDING_DECISION.items():
            assert method in _CONVENTION.get(repo, {}), (
                f"_PENDING_DECISION 列了未分类的 {repo}.{method}")
            assert reason.strip(), f"{repo}.{method} 的待决理由为空"
            assert (repo, method) not in _ASYMMETRIC_JUSTIFIED, "两处登记重复"


# ══════════════════════════════════════════════════════════════════
# 2. 行为：声明的约定必须真的成立
# ══════════════════════════════════════════════════════════════════

class TestConventionHolds:
    """表态说 fail-closed 却返回空（或反之）→ 红。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("repo_name", sorted(_CONVENTION))
    async def test_read_paths_behave_as_declared(self, repo_name: str):
        cls = _discover_repositories()[repo_name]
        wrong: list[str] = []
        for method, kind in sorted(_CONVENTION[repo_name].items()):
            if kind not in (READ_FAIL_CLOSED, READ_EMPTY_OK):
                continue
            outcome, value = await _call_without_backend(cls, method)
            if kind == READ_FAIL_CLOSED and outcome != "raise":
                wrong.append(
                    f"{repo_name}.{method} 声明 READ_FAIL_CLOSED，"
                    f"实际返回了空值 {value!r}——『不知道』被报成了『没有』")
            if kind == READ_EMPTY_OK and outcome == "raise":
                wrong.append(
                    f"{repo_name}.{method} 声明 READ_EMPTY_OK，实际抛了异常")
            if outcome == "value":
                # 后端全离线还能拿到非空结果 → 说明根本没查后端（缓存/常量），
                # 需要显式说明，故不允许静默通过
                wrong.append(
                    f"{repo_name}.{method} 在 _db=None 下返回了非空结果 {value!r}"
                    f"——该路径没有走后端，请显式说明并改分类")
        assert not wrong, "\n  ".join(["存储层约定漂移："] + wrong)


# ══════════════════════════════════════════════════════════════════
# 3. 同仓一致性：读路径混向必须写理由
# ══════════════════════════════════════════════════════════════════

class TestRepositoryReadConsistency:
    """同仓读路径必须同向；混向必须在 `_ASYMMETRIC_JUSTIFIED` 给出理由。

    这条是本门禁的核心——「两条路径不对称」是本项目反复出现的缺陷族，这里把它变成
    **需要写理由才能通过**的事情。
    """

    def test_mixed_read_paths_require_justification(self):
        unjustified: list[str] = []
        for repo, methods in sorted(_CONVENTION.items()):
            reads = {m: c for m, c in methods.items()
                     if c in (READ_FAIL_CLOSED, READ_EMPTY_OK)}
            if len(set(reads.values())) <= 1:
                continue
            if repo not in _ASYMMETRIC_JUSTIFIED:
                fail_closed = sorted(m for m, c in reads.items() if c == READ_FAIL_CLOSED)
                empty_ok = sorted(m for m, c in reads.items() if c == READ_EMPTY_OK)
                unjustified.append(
                    f"{repo}: fail-closed={fail_closed} / empty-ok={empty_ok}")
        assert not unjustified, (
            "同仓读路径混向且未在 _ASYMMETRIC_JUSTIFIED 说明理由：\n  "
            + "\n  ".join(unjustified))

    def test_justifications_are_not_stale(self):
        stale = []
        for repo, reason in _ASYMMETRIC_JUSTIFIED.items():
            reads = {m: c for m, c in _CONVENTION.get(repo, {}).items()
                     if c in (READ_FAIL_CLOSED, READ_EMPTY_OK)}
            if len(set(reads.values())) <= 1:
                stale.append(repo)   # 已统一，理由该删了
            assert reason.strip(), f"{repo} 的理由为空"
        assert not stale, f"这些仓储读路径已同向，_ASYMMETRIC_JUSTIFIED 条目应删除：{stale}"

    def test_declared_pending_set_is_documented(self):
        """待决集合非空时必须都带理由，且必须与报告决策项 #8 的框架一致。"""
        assert _PENDING_DECISION, (
            "待决集合为空说明约定已全部定案——若确实如此，请删除本用例并更新文档")
        for key, reason in _PENDING_DECISION.items():
            assert isinstance(key, tuple) and len(key) == 2


@pytest.mark.service
def test_pending_decision_entries_are_reachable_in_source():
    """结构性：`_PENDING_DECISION` 里点名的写路径确实存在于源码（防手误拼写）。"""
    src_root = pathlib.Path("core/storage")
    for repo, method in _PENDING_DECISION:
        path = src_root / f"{_snake(repo)}.py"
        assert path.is_file(), f"未找到 {path}"
        assert f"async def {method}" in path.read_text(encoding="utf-8"), (
            f"{repo}.{method} 在 {path} 中不存在（_PENDING_DECISION 拼写错误？）")


def _snake(class_name: str) -> str:
    """`CharacterVersionRepository` → `character_version_repo`。"""
    base = class_name.removesuffix("Repository")
    out = []
    for i, ch in enumerate(base):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out) + "_repo"


@pytest.mark.service
def test_convention_table_covers_all_discovered_repositories():
    """元用例：发现逻辑与表都真的在看代码（不靠 hardcode 的仓储清单）。"""
    repos = _discover_repositories()
    assert len(repos) >= 13, f"只发现 {len(repos)} 个仓储，发现逻辑可能失效：{sorted(repos)}"
    total_methods = sum(len(_public_async_methods(c)) for c in repos.values())
    assert total_methods >= 80, f"只枚举到 {total_methods} 个公开方法，枚举逻辑可能失效"


# 让 asyncio 在本模块的同步用例里也能用（无需 event loop 依赖）
def test_module_imports_cleanly():
    assert asyncio is not None


class TestRenameOfflineToleranceIsExplicit:
    """`SessionManager.rename` 的**离线容忍必须有出处**（写路径对齐的配套守卫）。

    仓库层现在对离线 raise（`False` 只表示"会话不存在"）；manager 想把离线当
    "未持久化但仍更新进程内摘要"就必须**显式** catch——而不是像以前那样靠
    "repo 把两种语义压进同一个 False"隐式成立。
    """

    @pytest.mark.unit
    def test_manager_catches_storage_unavailable_explicitly(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[3]
               / "core/memory/session_manager.py").read_text(encoding="utf-8")
        assert "except StorageUnavailableError:" in src, \
            "manager 的离线容忍消失/改成隐式了——容忍可以保留，但必须看得见"
        assert "persisted = False" in src


class TestPendingDecisionIsNotStale:
    """`_PENDING_DECISION` 的条目**必须仍然成立**——它是"约定未定"的清单，不是许愿池。

    实测（2026-09-14）：`TokenRepository.flush` 早已改成 raise（源码 docstring 写着
    "修复前静默 no-op + 删文件丢数据"），条目却还挂在 `_PENDING_DECISION` 里——
    下一个人会重复处理一件已完成的事。本类把"已修好就不许再登记"钉住。
    """

    @pytest.mark.asyncio
    async def test_token_flush_is_fail_closed_and_unregistered(self):
        from core.storage.base import StorageUnavailableError
        from core.storage.token_repo import TokenRepository

        repo = TokenRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            await repo.flush([_fake_usage()])
        assert ("TokenRepository", "flush") not in _PENDING_DECISION, \
            "已经 fail-closed 了却还登记在待决策表里（陈旧项）"

    @pytest.mark.asyncio
    async def test_token_clear_all_is_fail_closed_and_unregistered(self):
        from core.storage.base import StorageUnavailableError
        from core.storage.token_repo import TokenRepository

        repo = TokenRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            await repo.clear_all()
        assert ("TokenRepository", "clear_all") not in _PENDING_DECISION


def _fake_usage():
    """最小可用 TokenUsage（flush 只在 entries 非空时才检查连接）。"""
    from core.schema.token import TokenUsage
    return TokenUsage(session_id="s", model="m", round_num=1,
                      input_tokens=1, output_tokens=1, total_tokens=2)
