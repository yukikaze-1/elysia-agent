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
- `_PENDING_DECISION`：约定**尚未定**的路径——本表把每条待决路径**逐条列出**，
  而不是让它藏在实现里；定案后把它们移进 `READ_FAIL_CLOSED`/`READ_EMPTY_OK`。
  **当前为空**：决策 #8（2026-09-16「整仓统一 fail-closed」）已把三个混向仓储改齐，
  Milvus `MemoryRepository` 的降级是**已定案豁免**（记忆检索 best-effort）。

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
    # #6（决策 #6）：后台任务终态结果——整仓 fail-closed。
    # 调用方（mailbox 排空 / 启动补投）**必须能区分**"没有待补投的记录"与"库读不到"：
    # 前者是常态（该干啥干啥），后者要降级为"本次不落库/下轮再补投"并记 warning。
    # 若读路径静默返回空，Mongo 离线就会伪装成"没有待补投的任务"——那正是"终态跨重启
    # 不丢"被悄悄取消的样子。
    "BackgroundTaskResultRepository": {
        "upsert_pending": WRITE,
        "mark_delivered": WRITE,
        "list_pending": READ_FAIL_CLOSED,
        "count_pending": READ_FAIL_CLOSED,
        # #6 硬杀窗口：启动 replay WAL 时按 task_id 判重，**库是权威**。这里必须
        # fail-closed——把"查不到（库离线）"当成"库里没有"会让 WAL 候选把一条
        # 已经投递过（delivered=True）的结果重置回待投，用户被重复告知同一个结果。
        "get": READ_FAIL_CLOSED,
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
    # 决策 #8（2026-09-14 owner「一律 fail-closed」／2026-09-16 明确适用范围为
    # **整仓**）：版本历史读一律 fail-closed——"库离线"不得看起来像"没有历史"。
    "CharacterVersionRepository": {
        "append_version": WRITE,
        # 决策 #8（2026-09-14 owner）：**一律 fail-closed**——读不到版本历史时宁可
        # 503，也不要让"库离线"看起来像"没有历史"（后者会被误读为历史被清空）。
        "list_versions": READ_FAIL_CLOSED,
        "get_version": READ_FAIL_CLOSED,
        "count_versions": READ_FAIL_CLOSED,
        # #21b：会话删除级联（离线抛，不得静默返回 0 被 caller 当成功）
        "delete_by_session": WRITE,
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
    "GenerationRepository": {
        "upsert": WRITE,
        "find_one": READ_FAIL_CLOSED,
        "find_by_message": READ_FAIL_CLOSED,
        "find_by_user_message": READ_FAIL_CLOSED,
        "find_by_session": READ_FAIL_CLOSED,
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
    # Milvus 仓储：读返回空（向量检索"没召回"与"库离线"在调用方都退化为无记忆）。
    # **已定案豁免（决策 #8 适用范围的例外，owner 2026-09-16）**：记忆检索是
    # best-effort 能力——Milvus 抖动/重建索引期间不该把整轮对话打成 503；且
    # "这次没召回"与"库离线"在**调用方语义**上确实同解（都=这轮没有相关记忆）。
    # 写路径 insert/delete 的降级语义收在 LongTermMemory._repo_insert/_repo_delete。
    "MemoryRepository": {
        "insert": WRITE,
        "search": READ_EMPTY_OK,
        "query": READ_EMPTY_OK,
        "delete": WRITE,
        "count": READ_EMPTY_OK,
        "clear_all": WRITE,
    },
    "MessageRepository": {
        "append": WRITE,
        "query_by_session": READ_FAIL_CLOSED,
        "query": READ_FAIL_CLOSED,
        "query_page": READ_FAIL_CLOSED,
        "get_by_mongo_id": READ_FAIL_CLOSED,
        "get_by_id": READ_FAIL_CLOSED,
        "query_after": READ_FAIL_CLOSED,
        "find_context": READ_FAIL_CLOSED,
        "query_search": READ_FAIL_CLOSED,
        "count_by_session": READ_FAIL_CLOSED,
        "count_assistant_without_character": READ_FAIL_CLOSED,
        "count": READ_FAIL_CLOSED,
        "delete_by_session": WRITE,
        "clear_all": WRITE,
        "query_offset": READ_FAIL_CLOSED,
        "delete_one": WRITE,
        "update_text": WRITE,
    },
    "SessionRepository": {
        "save": WRITE,
        "create": WRITE,
        "ensure_exists": WRITE,
        "rename": WRITE,
        "load": READ_FAIL_CLOSED,
        "delete": WRITE,
        "list_all": READ_FAIL_CLOSED,
        # 批次 8 / P2-01r：分页读、单文档摘要读、覆盖层批量读——三条都是读路径，
        # 与同仓其余读同向 fail-closed（"我不知道" ≠ "没有会话"）。
        "list_page": READ_FAIL_CLOSED,
        "get_summary": READ_FAIL_CLOSED,
        "summaries_by_ids": READ_FAIL_CLOSED,
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

# 允许"同仓读路径混向"的白名单——**必须给出理由**。
#
# **2026-09-16 起为空（决策 #8 定案）**：owner 裁定「整仓统一 fail-closed」——
# 三个曾混向的仓储（MessageRepository 8 条 / SessionRepository.list_all /
# GenerationRepository 3 条）已全部转为 READ_FAIL_CLOSED，故本表清空。
# 保留空表 + 上面两条结构性用例（混向未写理由 → 红；理由陈旧 → 红）：下一次
# 真的需要"同仓混向"时，必须在这里写理由，而不是让不对称藏在实现里。
_ASYMMETRIC_JUSTIFIED: dict[str, str] = {}

# 约定尚未定 / 已知与同仓其余路径不对称的写路径——逐条列出，不让它藏在实现里。
# 键为 (仓储名, 方法名)，值为说明。定案后从本表移除并改分类。
#
# **已定案（#31 写路径收口，2026-09-15）**：5 条写路径全部对齐同仓约定——
# `MemoryRepository.insert/delete`（本表原两项，`MemoryRepository` 现在整仓写
# fail-closed，降级语义收到 `LongTermMemory._repo_insert/_repo_delete` 两个收口点）、
# `SessionRepository.rename`、`TokenRepository.flush`、`TokenRepository.clear_all`。
# 本表保持空表也是**有意义的断言**：它证明"没有已知的未定案写路径"。
_PENDING_DECISION: dict[tuple[str, str], str] = {}

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
    "session_ids": lambda: ["probe"],
    "limit": lambda: 10,
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
    # #6：后台任务终态结果按 task_id 检索（jarvis 的 WorkflowId，探测用裸 str）
    "task_id": lambda: "probe",
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

    # 原 `test_declared_pending_set_is_documented` 已按它自己的指示删除：
    # 2026-09-15 起 `_PENDING_DECISION` 为空（5 条写路径全部定案，见该表上方注释）。
    # 表本身保留——它会在**下一条**"约定未定"的写路径出现时重新被使用，
    # 与之配套的结构守卫 `test_pending_decision_entries_are_reachable_in_source`
    # 也随之重新生效（空表下为空转）。


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
    async def test_memory_writes_fail_closed_and_unregistered(self):
        """#31 写路径收口 4–5/5：`MemoryRepository.insert/delete` 离线 raise。

        反证：把 repo 改回 `return []` / `return 0` → 本用例红（这正是 2026-09-14
        登记它们为"待决策"时的样子）；改回 fail-closed 却忘了从 `_PENDING_DECISION`
        移除 → 第二个断言红。
        """
        from core.storage.base import StorageUnavailableError
        from core.storage.memory_repo import MemoryRepository

        repo = MemoryRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            await repo.insert(vectors=[[0.1] * 8], metadata=[{"text": "x"}])
        with pytest.raises(StorageUnavailableError):
            await repo.delete([1])
        assert ("MemoryRepository", "insert") not in _PENDING_DECISION, \
            "已经 fail-closed 了却还登记在待决策表里（陈旧项）"
        assert ("MemoryRepository", "delete") not in _PENDING_DECISION

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
