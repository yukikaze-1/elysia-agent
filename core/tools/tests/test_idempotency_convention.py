"""插件幂等声明的约定表 + 遍历所有插件的断言（2026-09-16）。

## 为什么需要这张表

修复前：`core/tool_runtime/scheduler.py::_execute_tool` 把**自己合成**的超时
（`asyncio.TimeoutError` 分支）写成 `ErrorType.TIMEOUT`，而 `TIMEOUT.is_retryable`
为真 → 于是**每个**工具（包括 `write_file` / `set_reminder` / `desktop.file.write`
这些有副作用的）在超时后都会被用同一个 `call_id` 再 invoke 一次。插件从未被问过。

修复后：**插件自己返回的错误**是插件的陈述（照旧按 `ErrorType.is_retryable` 重试）；
**调度器自己合成的超时**是调度器的猜测，只有插件声明
`SafetyProfile.idempotent=True` 才重试。默认 `False` 是 fail-safe（忘记声明 →
少重试一次，而不是把副作用做两遍）。

## 本表的语义（**不是**"现状快照"，而是"显式表态"）

每个插件必须回答两件事，写成一行：

- `mutating`：它到底会不会改世界（写文件 / 写记忆 / 设提醒 / 动用户桌面…）——
  按"实际做了什么"判，不是按名字；
- `declared`：它声明的 `SafetyProfile.idempotent`——**必须与代码里的值逐字一致**。

## 门禁（`TestConventionCoverage`）

- `ALL_PLUGINS` 里每个插件必须在表里（新增插件不表态 → 红）；
- 表里的 `declared` 必须等于 `cls.safety.idempotent`（改代码不改表 / 改表不改代码 → 红）；
- 表里不得有已不存在的插件（陈旧条目 → 红）；
- 每条理由必须非空（"为什么"不许留白）；
- `mutating=True` 却声明幂等的，必须在 `_MUTATING_BUT_IDEMPOTENT_JUSTIFIED`
  逐条写理由（当前为空表——空表本身是断言："没有任何声明幂等的写工具"）。

发现方式与 `core/storage/tests/test_fail_closed_convention.py` 同构：表与代码对不上
就红，而不是让"哪个工具会被重试"只存在于 scheduler 的一个表达式里。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from Module.tool import PluginContext, ToolManager
from Module.tool.safety import SafetyProfile
from core.tools import ALL_PLUGINS

pytestmark = [pytest.mark.unit]


@dataclass(frozen=True)
class _Declaration:
    """一个插件的表态：会不会改世界 + 声明的 idempotent + 理由。"""

    mutating: bool
    declared: bool
    reason: str


# 插件运行时名（`cls.name`，即调度器 `declares_idempotent(tool_name)` 的键）→ 表态。
_DECLARATIONS: dict[str, _Declaration] = {
    "search_memory": _Declaration(
        mutating=False, declared=True,
        reason="只读查询长期记忆（Capability.DATABASE_READ）；重跑最多多花一次检索，"
               "不改变任何状态。",
    ),
    "save_memory": _Declaration(
        mutating=True, declared=False,
        reason="写长期记忆（DATABASE_WRITE，且 requires_human_approval）——每次调用 insert "
               "一条新记忆；超时后重试 = 记忆库里出现重复条目（新的向量 id）。",
    ),
    "get_time": _Declaration(
        mutating=False, declared=True,
        reason="纯读当前时间，无任何状态。",
    ),
    "web_search": _Declaration(
        mutating=False, declared=True,
        reason="只读外部检索（NETWORK + WEB_SEARCH）；重跑会多一次外部请求，但不产生"
               "本地或远端的状态变化。",
    ),
    "web_fetch": _Declaration(
        mutating=False, declared=True,
        reason="只读抓取 URL 内容（NETWORK）；同 web_search。",
    ),
    "read_file": _Declaration(
        mutating=False, declared=True,
        reason="沙盒内只读（Capability.FILESYSTEM_READ）。",
    ),
    "list_directory": _Declaration(
        mutating=False, declared=True,
        reason="沙盒内只读（Capability.FILESYSTEM_READ）。",
    ),
    "write_file": _Declaration(
        mutating=True, declared=False,
        reason="写沙盒文件（FILESYSTEM_WRITE + requires_human_approval）；两次调用之间文件"
               "可能已被别的写入改动，重试不等价于第一次的结果。",
    ),
    "set_reminder": _Declaration(
        mutating=True, declared=False,
        reason="每次调用都新建一个定时任务（AsyncTaskScope.spawn）并在到期时投递一条提醒"
               "——重试 = 用户收到两条提醒。",
    ),
    "get_runtime_config": _Declaration(
        mutating=False, declared=True,
        reason="只读进程/服务/工具/后台任务状态快照。",
    ),
    "get_self_state": _Declaration(
        mutating=False, declared=True,
        reason="只读 FSM 状态/会话计数/工具计数快照。",
    ),
    "describe_image": _Declaration(
        mutating=False, declared=True,
        reason="只读用户消息中的图片并调视觉模型描述；写 data/cache/vision 的是**内容寻址"
               "缓存**（键 = 图片字节 + 模型），不是用户可见状态——重跑安全，且它是最慢的"
               "工具（request_timeout=120s），正是最需要超时重试的一个。",
    ),
    "desktop.file.select": _Declaration(
        mutating=True, declared=False,
        reason="每次调用都会在用户桌面弹出一次系统文件选择器——重试 = 第二个对话框"
               "（Desktop 桥的 (tool_name, arguments_hash) 去重键在超时/offline 后即释放，"
               "不会与第一次合并），是用户可见的重复交互。",
    ),
    "desktop.file.read": _Declaration(
        mutating=False, declared=True,
        reason="读用户电脑上的文件内容，只读（委托 Electron 执行，后端不落任何状态）。",
    ),
    "desktop.file.write": _Declaration(
        mutating=True, declared=False,
        reason="写用户电脑上的文件（risk_level=high）。",
    ),
    "desktop.screen.capture": _Declaration(
        mutating=True, declared=False,
        reason="每次调用新截一张图并产生一份附件（内容还会随屏幕变化）；超时重试 = 重复"
               "截图 + 重复附件。",
    ),
}

# 允许"mutating=True 但 declared=True"的例外——必须逐条写理由（例：绝对赋值语义的
# 重入安全写）。当前为空，空表本身是断言。
_MUTATING_BUT_IDEMPOTENT_JUSTIFIED: dict[str, str] = {}


def _plugins_by_name() -> dict[str, type]:
    """运行时名 → 插件类（唯一发现源是 `core.tools.ALL_PLUGINS`）。"""
    return {cls.name: cls for cls in ALL_PLUGINS}


# ══════════════════════════════════════════════════════════════════
# 1. 覆盖：每个插件都必须表态
# ══════════════════════════════════════════════════════════════════

class TestConventionCoverage:
    """新增插件不表态 → 红。这是"哪些工具会被超时重试"不再只由 scheduler 决定的门禁。"""

    def test_every_plugin_is_classified(self):
        missing = sorted(set(_plugins_by_name()) - set(_DECLARATIONS))
        assert not missing, (
            f"这些插件没有幂等声明分类：{missing}——请在 test_idempotency_convention.py 的 "
            f"_DECLARATIONS 里表态（mutating? / SafetyProfile.idempotent? / 为什么）")

    def test_no_stale_entries(self):
        stale = sorted(set(_DECLARATIONS) - set(_plugins_by_name()))
        assert not stale, f"_DECLARATIONS 里有已不存在的插件：{stale}"

    def test_declaration_matches_code(self):
        """表与代码必须逐字一致——两边任一改动而另一边没跟上 → 红。"""
        wrong: list[str] = []
        for name, cls in sorted(_plugins_by_name().items()):
            entry = _DECLARATIONS.get(name)
            if entry is None:
                continue
            actual = cls.safety.idempotent
            if entry.declared != actual:
                wrong.append(
                    f"{name}: 表里 declared={entry.declared}，代码里 "
                    f"SafetyProfile.idempotent={actual}")
        assert not wrong, (
            "幂等声明表与插件代码不一致（改了一边忘了另一边）：\n  " + "\n  ".join(wrong))

    def test_reasons_are_present(self):
        for name, entry in sorted(_DECLARATIONS.items()):
            assert entry.reason.strip(), f"{name} 的声明理由为空——表态必须说明为什么"

    def test_discovery_is_not_broken(self):
        """元用例：发现逻辑与表都真的在看代码（不靠 hardcode 的插件清单）。"""
        plugins = _plugins_by_name()
        assert len(plugins) >= 16, f"只发现 {len(plugins)} 个插件，发现逻辑可能失效"
        assert len(_DECLARATIONS) == len(plugins), (
            f"表 {len(_DECLARATIONS)} 条 vs 插件 {len(plugins)} 个——覆盖测试会分别报红")


# ══════════════════════════════════════════════════════════════════
# 2. 声明本身有效：SafetyProfile 真的带 idempotent，且默认 fail-safe
# ══════════════════════════════════════════════════════════════════

class TestDeclarationIsWellFormed:
    def test_default_is_fail_safe_false(self):
        assert SafetyProfile().idempotent is False, (
            "缺省必须 False：忘记声明 → 少重试一次，而不是把副作用做两遍")

    def test_every_plugin_exposes_bool_idempotent(self):
        bad: list[str] = []
        for name, cls in sorted(_plugins_by_name().items()):
            safety = getattr(cls, "safety", None)
            if not isinstance(safety, SafetyProfile):
                bad.append(f"{name}: safety 不是 SafetyProfile（{safety!r}）")
                continue
            if not isinstance(safety.idempotent, bool):
                bad.append(f"{name}: idempotent={safety.idempotent!r} 不是 bool")
        assert not bad, "\n  ".join(["插件声明面不合法："] + bad)

    def test_mutating_idempotent_entries_require_justification(self):
        unjustified = sorted(
            name for name, e in _DECLARATIONS.items()
            if e.mutating and e.declared and name not in _MUTATING_BUT_IDEMPOTENT_JUSTIFIED
        )
        assert not unjustified, (
            f"这些插件既 mutating 又声明 idempotent，却没有在 "
            f"_MUTATING_BUT_IDEMPOTENT_JUSTIFIED 说明理由：{unjustified}")

    def test_justifications_are_not_stale(self):
        stale = [
            name for name in _MUTATING_BUT_IDEMPOTENT_JUSTIFIED
            if not (_DECLARATIONS.get(name) and _DECLARATIONS[name].mutating
                    and _DECLARATIONS[name].declared)
        ]
        assert not stale, f"这些例外已不成立，理由该删：{stale}"


# ══════════════════════════════════════════════════════════════════
# 3. 委托链：插件声明真的能被调度器问到
# ══════════════════════════════════════════════════════════════════

class TestDelegateChain:
    """`ToolManager` / `ToolAdapter` 的 `declares_idempotent` 与插件声明一致。

    调度器只认 `executor.declares_idempotent(tool_name)`；这条链断在哪一环，门控就
    静默失效（或反向失效）。未知工具 → False（与 `declared_request_timeout` 的
    "查不到就当作没声明"同构，方向取 fail-safe）。
    """

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_manager_reports_plugin_declarations(self):
        from core.tools import ReadFilePlugin, WriteFilePlugin

        tm = ToolManager()
        await tm.load_all([ReadFilePlugin, WriteFilePlugin], PluginContext(session_id="test"))
        assert tm.declares_idempotent("read_file") is True, (
            "只读插件应把 SafetyProfile.idempotent=True 透出来")
        assert tm.declares_idempotent("write_file") is False
        assert tm.declares_idempotent("no_such_tool") is False, (
            "未知工具必须 False（fail-safe：少重试一次，而不是重复副作用）")

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_adapter_passes_through_manager(self):
        from core.adapter import ToolAdapter
        from core.tools import ReadFilePlugin, WriteFilePlugin

        adapter = ToolAdapter([ReadFilePlugin, WriteFilePlugin], session_id="test")
        await adapter.start()
        assert adapter.declares_idempotent("read_file") is True
        assert adapter.declares_idempotent("write_file") is False
        assert adapter.declares_idempotent("no_such_tool") is False

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_every_declared_true_plugin_is_true_through_the_manager(self):
        """表里 `declared=True` 的插件，经真实 manager 查询也必须为 True。

        这条把"表 → 插件类 → ToolManager"三处串起来：任何一环写错（比如 config.yaml
        覆盖成 False）都会红。
        """
        tm = ToolManager()
        await tm.load_all(list(ALL_PLUGINS), PluginContext(session_id="test"))
        wrong: list[str] = []
        for name, entry in sorted(_DECLARATIONS.items()):
            if tm.declares_idempotent(name) != entry.declared:
                wrong.append(f"{name}: 表 declared={entry.declared}，manager 查询="
                             f"{tm.declares_idempotent(name)}")
        assert not wrong, "\n  ".join(["声明经 manager 后失真："] + wrong)
