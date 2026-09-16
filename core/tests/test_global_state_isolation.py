# 测试文件：core/tests/test_global_state_isolation.py
"""Batch 1.5 —— **测试自身**不得写坏进程级状态（marker=service）。

与 `core/tests/test_process_global_state.py` 同一族：那里钉的是**有意保留**的进程级
单例契约（`idempotency._store` / personality scope），本文件钉的是另一半——测试代码
**意外**留下的进程级脏状态。两者互补，不重复。

## 实测事实（本文件落地前，commit `0bb4c281`）

| 被测出来的事实 | 证据 |
|---|---|
| `core/tests/test_phase3.py` 21 处测试体内裸写 `os.environ.setdefault("DEEPSEEK_API_KEY","test")` | 探针插件（打桩 `os._Environ.__setitem__` 追踪写入点）逐条列出 |
| `core/tools/tests/test_filesystem.py` / `test_write_file.py` 就地改**类级** `SafetyProfile` | 见下「顺序翻转」 |
| 同一份代码，**只换文件顺序**就翻红 | `pytest core/tools/tests/test_filesystem.py Module/tool/tests/test_safety_read_isolation.py` → **1 failed**；反序 → 30 passed；各自单跑 → 全绿；分两个进程 → 全绿 |
| `Module/database/vectordb/tests/conftest.py` 的 session autouse fixture 裸写 `MILVUS_URI` | `pytest core/tests/test_clean_db.py`（无 .env）→ **2 failed**；`pytest Module/database/vectordb/tests core/tests/test_clean_db.py` → 95 passed |

`--dist loadfile -n 8` 下「哪两个文件进同一个 worker」由调度决定，所以这些红是**偶发**的：
同一命令连跑两次可以一红一绿。这也正是它们能长期藏在 CI 里的原因。

## 五条守卫

1. **静态**：`**/tests/**`、`test_*.py`、`conftest.py` 里不得裸写 `os.environ`
   （`os.environ[k] = v` / `setdefault` / `update` / `pop` / `clear` / `putenv` / `del`），
   豁免须逐项登记理由——登记项**陈旧**（文件里已没有写入）同样报红。
2. **行为**：跑完「曾经泄漏」的模块集后，`os.environ` 必须与 session 起点**逐键一致**。
3. **行为**：插件类级 `SafetyProfile` 不得被「给实例配沙盒」的写法改写——测试 helper 与
   **生产路径** `ToolManager.load_all` 各一条。
4. **行为**：把上面那对文件按**红过的顺序**（A→B）放进**同一个进程**跑，必须全绿；
   反序同样全绿。顺序扰动是本批次的主题，这条是它的可执行形态。
5. **反证**：rebind 出来的实例**确实**用上了新沙盒——防止"修法"退化成什么都不做。
"""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.service

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ── 守卫 1 的豁免表 ────────────────────────────────────────────────────
_ENV_WRITE_ALLOWLIST: dict[str, str] = {
    "Module/workflow_engine/tests/helpers/__init__.py":
        "external e2e 辅助 `_ensure_env()`：按需把宿主 .env 读进进程 env，供真实 LLM "
        "e2e 使用。属 external 层（`make test-external`，默认任何档位都不跑），"
        "本批次不改——改它属 external 层的独立议题。",
    "Module/workflow_engine/tests/integration/conftest.py":
        "同上（integration 目录级 conftest，为真实 LLM 集成测试备环境）。",
    "core/cognitive/tests/test_delegation_preparer.py":
        "import 期解析 .env 写 os.environ（14 键）。**已与根 conftest 的 pytest 入口引导"
        "重复**（后者在收集前就把 .env 注入完毕，故此处实际是 no-op），但仍留着裸写。"
        "本批次不可改 `core/cognitive/**`（路径所有权），待 owner 收口。",
    "core/cognitive/tests/test_v2_generation.py":
        "`_make_agent_with()` 里裸写 `os.environ.setdefault(\"DEEPSEEK_API_KEY\",\"test\")`。"
        "同上：属 `core/cognitive/**`，本批次不可改，待 owner 收口（修法：module 级 "
        "autouse + monkeypatch，缺失才设）。",
}

_FORBIDDEN_WRITE_ATTRS = frozenset({"setdefault", "update", "pop", "clear", "popitem"})


def _scan_env_writes() -> dict[str, list[tuple[int, str]]]:
    """扫描测试代码里的裸 `os.environ` 写入点，返回 {相对路径: [(行号, 形式)]}。

    `os.environ.setdefault(...)` / `.update(...)` 等经 `MutableMapping` 落到
    `__setitem__`，与 `os.environ[k] = v` 同属一类进程级写入，一并扫。

    Returns:
        dict: 相对仓库根的路径 → [(行号, 写入形式)]；无写入的文件不出现。
    """
    skip = {".git", "__pycache__", "node_modules", "tmp", "frontend", "static", "data"}
    found: dict[str, list[tuple[int, str]]] = {}

    def _is_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in ("os", "_os")
        )

    for path in sorted(_REPO_ROOT.rglob("*.py")):
        parts = set(path.relative_to(_REPO_ROOT).parts)
        if parts & skip:
            continue
        is_test_code = (
            "tests" in parts or path.name.startswith("test_") or path.name == "conftest.py"
        )
        if not is_test_code:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue

        hits: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            form: str | None = None
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and _is_environ(t.value) for t in node.targets
            ):
                form = "os.environ[k] = v"
            elif isinstance(node, ast.Delete) and any(
                isinstance(t, ast.Subscript) and _is_environ(t.value) for t in node.targets
            ):
                form = "del os.environ[k]"
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if _is_environ(node.func.value) and node.func.attr in _FORBIDDEN_WRITE_ATTRS:
                    form = f"os.environ.{node.func.attr}()"
                elif node.func.attr == "putenv":
                    form = "os.putenv()"
            if form:
                hits.append((node.lineno, form))
        if hits:
            found[str(path.relative_to(_REPO_ROOT))] = sorted(hits)
    return found


class TestNoBareEnvironWrites:
    """守卫 1：进程级 env 写入必须走 `monkeypatch`（自动还原）或登记豁免。"""

    def test_test_code_does_not_write_os_environ(self):
        """全仓测试代码不得裸写 `os.environ`（豁免见 `_ENV_WRITE_ALLOWLIST`）。"""
        found = _scan_env_writes()
        unregistered = {rel: hits for rel, hits in found.items()
                        if rel not in _ENV_WRITE_ALLOWLIST}
        assert not unregistered, (
            "测试代码出现未登记的裸 os.environ 写入——用 monkeypatch.setenv/delenv"
            "（用例结束自动还原）替代；确需保留的登记进 _ENV_WRITE_ALLOWLIST 并写理由。\n"
            + "\n".join(f"  {rel}:{ln} {form}" for rel, hits in unregistered.items()
                        for ln, form in hits))

    def test_allowlist_has_no_stale_entries(self):
        """豁免表不得留陈旧项：文件里已无裸写却仍挂着豁免 → 红（与门禁同纪律）。"""
        found = _scan_env_writes()
        stale = sorted(rel for rel in _ENV_WRITE_ALLOWLIST if rel not in found)
        assert not stale, (
            "以下豁免项已不再需要（文件里没有裸 os.environ 写入了），请从 "
            f"_ENV_WRITE_ALLOWLIST 删除：{stale}")


class TestPluginSafetyProfileIsolation:
    """守卫 3：`plugin.safety` 是**类属性**，给实例配沙盒必须 rebind 而非就地改。

    `core/tools/filesystem/plugin.py` 里 `ReadFilePlugin.safety = SafetyProfile(...)`
    是**类级**对象；`ToolPlugin.safety: Any = None` 也没有 per-instance 拷贝。
    于是 `plugin.safety.read_dirs = [...]` 改的是**全进程共享的那一份类默认值**，
    且用例结束不还原——下一批测试读到的「类默认」已经是上一个测试的 `tmp_path`。
    """

    @staticmethod
    def _class_defaults() -> dict[str, dict]:
        from core.tools.filesystem.plugin import (
            ListDirPlugin,
            ReadFilePlugin,
            WriteFilePlugin,
        )

        return {
            cls.__name__: copy.deepcopy(getattr(cls.safety, "__dict__", {}))
            for cls in (ReadFilePlugin, ListDirPlugin, WriteFilePlugin)
        }

    def test_repo_plugin_helpers_do_not_mutate_class_defaults(self, tmp_path):
        """仓库自己的两个「给实例配沙盒」helper 跑完，类默认值必须原样。

        helper 用 `dataclasses.replace` 生成实例专属副本（修法）；此前是就地改，
        本用例在那一刻是红的——实测：
        `pytest core/tools/tests/test_filesystem.py Module/tool/tests/test_safety_read_isolation.py`
        → 后者 1 failed。
        """
        from core.tools.tests.test_filesystem import TestReadFilePlugin, _list_plugin

        before = self._class_defaults()

        TestReadFilePlugin._plugin(tmp_path)
        _list_plugin(tmp_path)

        after = self._class_defaults()
        changed = {name: (before[name], after[name])
                   for name in before if before[name] != after[name]}
        assert not changed, (
            "插件类级 SafetyProfile 被就地改写——实例配置必须 rebind"
            f"（`dataclasses.replace`），否则跨测试/跨实例共享同一份可变默认值：{changed}")

    def test_instance_rebind_is_visible_only_to_that_instance(self, tmp_path):
        """反证守卫：rebind 后的实例**确实**用上了新沙盒（修法不是"什么都不做"）。"""
        from core.tools.filesystem.plugin import ReadFilePlugin
        from dataclasses import replace

        plugin = ReadFilePlugin()
        plugin.safety = replace(
            plugin.safety, read_dirs=[str(tmp_path)], write_dir=str(tmp_path))

        assert plugin.safety.read_dirs == [str(tmp_path)]
        assert ReadFilePlugin.safety.read_dirs != [str(tmp_path)]

    @pytest.mark.asyncio
    async def test_tool_manager_config_load_does_not_mutate_class_defaults(self):
        """**生产路径**同样不得改写类默认值：`ToolManager.load_all` 按 config.yaml 配实例。

        `Module/tool/manager.py::_load_safety_config` 原先就地 `setattr(instance.safety, …)`，
        而 `instance.safety is cls.safety` ⇒ 加载一次 config.yaml 就把**进程级共享**的类默认
        值永久改掉，之后新建的实例（别的会话/别的测试）全部继承。实测一次
        `-m "unit or service"`：`WebFetchPlugin.safety.max_concurrency` 0→3，6/8 个 xdist
        worker 的类默认值被改写。修法是按实例 `dataclasses.replace`（本用例在修前是红的）。
        """
        from Module.tool.manager import ToolManager
        from Module.tool.protocol import PluginContext

        from core.tools import ALL_PLUGINS

        before = self._class_defaults()

        tm = ToolManager()
        await tm.load_all(list(ALL_PLUGINS), PluginContext(session_id="test"))
        try:
            after = self._class_defaults()
        finally:
            await tm.shutdown()

        changed = {name: (before[name], after[name])
                   for name in before if before.get(name) != after.get(name)}
        assert not changed, (
            "`ToolManager.load_all` 按 config.yaml 配实例时改写了插件**类级** "
            f"SafetyProfile（进程级共享默认值）：{changed}")


# ── 守卫 2 / 4：行为守卫（子进程内跑真实 pytest） ──────────────────────

_ENV_DIFF_PLUGIN = '''\
"""pytest plugin：把 session 起止的 os.environ 差异落盘（守卫 2 的观测点）。"""
import json
import os

_BASELINE: dict[str, str] = {}


def pytest_sessionstart(session):
    _BASELINE.update(os.environ)


def pytest_sessionfinish(session, exitstatus):
    added = sorted(k for k in os.environ if k not in _BASELINE)
    changed = sorted(k for k in _BASELINE if os.environ.get(k) != _BASELINE[k])
    removed = sorted(k for k in _BASELINE if k not in os.environ)
    out = os.environ.get("B15_ENV_DIFF_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"added": added, "changed": changed, "removed": removed}, fh)
'''

_LEAKY_MODULES = [
    # A：曾经就地改 ReadFilePlugin/ListDirPlugin 类级 safety 的模块。
    "core/tools/tests/test_filesystem.py",
    # B：断言「类默认必须是会话模板」的受害者——A→B 时曾红。
    "Module/tool/tests/test_safety_read_isolation.py",
    # C：在无 .env 的 checkout 里单跑曾 2 failed——它的 Milvus 用例一度只靠
    #    D 目录泄漏的 MILVUS_URI 才绿。
    "core/tests/test_clean_db.py",
    # D：目录级 conftest 的 session autouse fixture 曾把 MILVUS_URI 永久写进进程 env。
    #    **C 在前、D 在后**——正是修前翻红的那个顺序。
    "Module/database/vectordb/tests",
]

_ENV_DIFF_IGNORE = frozenset({
    # pytest 自己按用例设置/清理的记账变量，不是被测代码留下的全局状态。
    "PYTEST_CURRENT_TEST",
})


def _run_pytest_in_subprocess(targets: list[str], tmp_path: Path) -> tuple[str, dict]:
    """在子进程里跑目标模块，返回 (stdout 尾部, env 差异)。

    子进程用 `sys.executable`（**不是** `conda run`）——本进程已在 Elysia env 内，
    省掉每轮 ~4s 的 conda 包装开销。

    **环境被刻意收窄**：`MILVUS_URI` 从子进程环境里删掉，否则「依赖 ambient env」
    的假绿会掩盖被测的顺序依赖（在带 `.env` 的 checkout 里，root conftest 的入口
    引导本来就会注入它）。删掉后 `Module/database/vectordb/tests` 只能靠自己的
    autouse fixture 拿到 URI——正是这条fixture 要守住的东西。
    """
    plugin_dir = tmp_path / "b15_plugins"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "b15_env_diff.py").write_text(_ENV_DIFF_PLUGIN, encoding="utf-8")
    diff_out = tmp_path / "env_diff.json"

    env = {k: v for k, v in os.environ.items() if k != "MILVUS_URI"}
    env["B15_ENV_DIFF_OUT"] = str(diff_out)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), env.get("PYTHONPATH", "")]).strip(os.pathsep)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *targets,
         "-p", "b15_env_diff", "-p", "no:cacheprovider", "-q", "--no-header"],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    diff = json.loads(diff_out.read_text(encoding="utf-8")) if diff_out.exists() else {}
    diff = {k: [x for x in v if x not in _ENV_DIFF_IGNORE]
            for k, v in diff.items() if isinstance(v, list)}
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-25:])
    return tail, diff


def _assert_green_and_env_clean(tail: str, diff: dict, label: str) -> None:
    """两条判据都算完再断言——一条失败不该掩盖另一条（诊断信息要完整）。"""
    problems: list[str] = []
    if "failed" in tail or "error" in tail.lower():
        problems.append(f"出现失败——顺序依赖/环境依赖：\n{tail}")
    if any(diff.get(k) for k in ("added", "changed", "removed")):
        problems.append(f"跑完留下 os.environ 差异（进程级泄漏）：{diff}\n{tail}")
    assert not problems, f"[{label}] " + "\n".join(problems)


class TestOrderPerturbationAndEnvLeak:
    """守卫 2 + 4：顺序不得翻转结果，跑完不得留下 env 差异。"""

    def test_forward_order_is_green_and_leaves_environ_clean(self, tmp_path):
        """A→B→C→D 单进程：必须全绿，且 session 前后 `os.environ` 逐键一致。

        修前实测（`0bb4c281`）：这一串里 `test_safety_read_isolation` 因
        `ReadFilePlugin.safety` 被前序测试改成 `tmp_path` 而 1 failed；`test_clean_db`
        的两条 Milvus 用例则因 D 的 `MILVUS_URI` 还没被写下而 2 failed（C 在 D 前）。
        """
        tail, diff = _run_pytest_in_subprocess(_LEAKY_MODULES, tmp_path)
        _assert_green_and_env_clean(tail, diff, "A→B→C→D")

    def test_reverse_order_is_green_and_leaves_environ_clean(self, tmp_path):
        """反序同为绿并同样干净——顺序扰动两个方向都要钉住。"""
        tail, diff = _run_pytest_in_subprocess(list(reversed(_LEAKY_MODULES)), tmp_path)
        _assert_green_and_env_clean(tail, diff, "D→C→B→A")

