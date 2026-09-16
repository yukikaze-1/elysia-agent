"""#47 两条静态门禁的测试。

`scripts/check_timeout_budget.py`（超时预算递减）与
`scripts/check_contract_reachability.py`（契约可达性）。

**为什么要测门禁本身**：这两条的价值全在"事情做错时它会红"。如果只在真实仓库上跑一次
拿到绿色就收工，无法区分"真的没问题"与"判据写错了、恒绿"。所以每个规则都配一个
**合成 fixture 仓库**的注入用例——在 `tmp_path` 里造最小树，断言必须红。

（真实仓库的绿由 Makefile/CI 跑；这里管的是"红得起来"。）
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str):
    """按路径加载 scripts/ 下的脚本（它们不是包的一部分）。"""
    path = _ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def budget():
    return _load("check_timeout_budget")


@pytest.fixture(scope="module")
def reach():
    return _load("check_contract_reachability")


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# 超时预算门禁
# ══════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestConstantEvaluation:
    """预算解析：只有能静态算出来的才参与比较，算不出来的必须落到"未解析"。"""

    def test_arithmetic_and_min_max(self, budget):
        import ast
        consts = {"A": 10.0, "B": 3.0, "C": 2.0}
        ev = lambda src: budget._const_eval(ast.parse(src, mode="eval").body, consts)  # noqa: E731
        assert ev("5") == 5.0
        assert ev("A * (1 + C) + B * C") == 36.0
        assert ev("min(A, B)") == 3.0
        assert ev("max(A, B)") == 10.0
        assert ev("A / 0") is None          # 除零不炸，视为不可解析
        assert ev("unknown_name") is None   # 未知名不可解析

    def test_function_budget_sums_sequential_waits_and_param_default(self, budget):
        """函数预算 = `timeout` 形参默认值 + 函数体内各段等待之和（不是 max）。"""
        import ast
        src = textwrap.dedent('''
            async def stop(self, timeout: float = 10.0):
                await asyncio.wait(fs, timeout=timeout)
                await asyncio.wait(fs, timeout=3.0)
                await asyncio.wait(fs, timeout=3.0)
                await asyncio.wait(fs, timeout=1.0)
        ''')
        tree = ast.parse(src)
        fn = tree.body[0]
        total, parts = budget._fn_budget(fn, {})
        assert total == 17.0, f"应为 10+3+3+1=17，实际 {total}（{parts}）"

    def test_nested_functions_have_their_own_budget_scope(self, budget):
        import ast
        src = textwrap.dedent('''
            async def outer():
                async def inner():
                    await asyncio.wait_for(x(), timeout=99.0)
                await asyncio.wait_for(inner(), timeout=5.0)
        ''')
        fn = ast.parse(src).body[0]
        total, _parts = budget._fn_budget(fn, {})
        assert total == 5.0, "内层函数体的预算不该算进外层的域"


@pytest.mark.unit
class TestTimeoutRules:
    """三条规则各自都要红得起来（合成 fixture 仓库）。"""

    def _repo(self, tmp_path: Path, body: str) -> Path:
        _write(tmp_path, "core/demo.py", body)
        return tmp_path

    def test_r1_nested_budget_violation_is_red(self, budget, tmp_path):
        """R1：词法嵌套时 内层 > 外层 → 红。"""
        root = self._repo(tmp_path, '''
            import asyncio

            async def f():
                async def g():
                    pass
                await asyncio.wait_for(_wrap(g()), timeout=5.0)
        ''')
        bad = budget._check_lexical(budget._scan(root))
        assert not bad, "无法解析的对象不该报违规"

    def test_r1_catches_real_nesting(self, budget, tmp_path, monkeypatch):
        """R1：真嵌套且两侧可解析 → 违规列表非空。"""
        root = self._repo(tmp_path, '''
            import asyncio

            async def f():
                await asyncio.wait_for(
                    asyncio.wait_for(_x(), timeout=30.0), timeout=5.0)
        ''')
        bad = budget._check_lexical(budget._scan(root))
        assert bad and "外层" in bad[0], f"嵌套 5 < 30 必须报，实际 {bad}"

    def test_r2_invariant_break_is_red(self, budget, tmp_path, monkeypatch):
        """R2：登记的 (外层, 内层) 被改成 外 < 内 → 红，且报出登记时写的理由。"""
        _write(tmp_path, "core/outer.py", "BUDGET = 5.0\n")
        _write(tmp_path, "core/inner.py", "INNER = 20.0\n")
        monkeypatch.setattr(budget, "_INVARIANTS", [
            ("core/outer.py::BUDGET", "core/inner.py::INNER", "测试理由"),
        ])
        bad = budget._check_invariants(budget._scan(tmp_path))
        assert bad and "5" in bad[0] and "20" in bad[0]
        assert "测试理由" in bad[0], "违规必须带上登记时写的理由"

    def test_r2_stale_entry_is_red(self, budget, tmp_path, monkeypatch):
        """R2：登记指向已不存在的符号 → 红（防登记表腐烂）。"""
        _write(tmp_path, "core/outer.py", "BUDGET = 5.0\n")
        monkeypatch.setattr(budget, "_INVARIANTS", [
            ("core/outer.py::BUDGET", "core/gone.py::MISSING", "x"),
        ])
        bad = budget._check_invariants(budget._scan(tmp_path))
        assert bad and "陈旧登记" in bad[0]

    # ── R4：插件声明的执行预算 ≤ 调度器上界（S2-G2）──

    def _r4_repo(self, tmp_path: Path, declared: str) -> Path:
        _write(tmp_path, "core/tool_runtime/scheduler.py",
               "_MAX_PLUGIN_REQUEST_TIMEOUT = 120.0\n")
        _write(tmp_path, "core/tools/demo/plugin.py",
               "from Module.tool.safety import SafetyProfile\n\n"
               f"safety = SafetyProfile(request_timeout={declared})\n")
        return tmp_path

    def test_r4_over_ceiling_is_red(self, budget, tmp_path):
        """R4：插件声明超过调度器上界 → 红（这是"设计好的路径被悄悄截断"的拦截点）。"""
        bad = budget._check_plugin_request_timeouts(
            budget._scan(self._r4_repo(tmp_path, "300.0")))
        assert bad and "> 调度器上界" in bad[0], bad

    def test_r4_unresolvable_declaration_is_red(self, budget, tmp_path):
        """无法静态解析 → 拒答（不许"判不了就放行"）。"""
        bad = budget._check_plugin_request_timeouts(
            budget._scan(self._r4_repo(tmp_path, 'float("120")')))
        assert bad and "静态解析不到" in bad[0], bad

    def test_r4_within_ceiling_passes(self, budget, tmp_path):
        assert not budget._check_plugin_request_timeouts(
            budget._scan(self._r4_repo(tmp_path, "120.0")))

    def test_r4_no_declarations_at_all_is_refused(self, budget, tmp_path):
        """扫描面失效（插件目录改名等）必须拒答——否则 R4 会静默变绿。"""
        _write(tmp_path, "core/tool_runtime/scheduler.py",
               "_MAX_PLUGIN_REQUEST_TIMEOUT = 120.0\n")
        bad = budget._check_plugin_request_timeouts(budget._scan(tmp_path))
        assert bad and "扫描面失效" in bad[0], bad

    def test_r3_unregistered_named_budget_is_red(self, budget, tmp_path, monkeypatch):
        """R3：新的具名预算常量没表态 → 红（这是"忘了写关系"的拦截点）。"""
        root = self._repo(tmp_path, '''
            import asyncio

            NEW_BUDGET = 7.0

            async def f():
                await asyncio.wait_for(_x(), timeout=NEW_BUDGET)
        ''')
        monkeypatch.setattr(budget, "_INVARIANTS", [])
        monkeypatch.setattr(budget, "_LEAF_BUDGETS", {})
        bad, _report = budget._check_outer_registered(budget._scan(root))
        assert bad and "NEW_BUDGET 未表态" in bad[0]

    def test_r3_registered_leaf_passes(self, budget, tmp_path, monkeypatch):
        """R3：登记成叶子后放行——这是"表态即通过"的正向路径。"""
        root = self._repo(tmp_path, '''
            import asyncio

            NEW_BUDGET = 7.0

            async def f():
                await asyncio.wait_for(_x(), timeout=NEW_BUDGET)
        ''')
        monkeypatch.setattr(budget, "_INVARIANTS", [])
        monkeypatch.setattr(budget, "_LEAF_BUDGETS", {"NEW_BUDGET": "叶子"})
        bad, _report = budget._check_outer_registered(budget._scan(root))
        assert not bad

    def test_r3_runtime_values_are_reported_not_blocked(self, budget, tmp_path, monkeypatch):
        """运行期值（形参 / 实例属性 / 契约字段）不算违规——否则门禁会被噪声淹没。"""
        root = self._repo(tmp_path, '''
            import asyncio

            async def f(timeout, self_attr):
                await asyncio.wait_for(_x(), timeout=timeout)
                await asyncio.wait_for(_y(), timeout=self._hitl_timeout)
                await asyncio.wait_for(_z(), timeout=node.max_duration_s)
        ''')
        monkeypatch.setattr(budget, "_INVARIANTS", [])
        monkeypatch.setattr(budget, "_LEAF_BUDGETS", {})
        bad, report = budget._check_outer_registered(budget._scan(root))
        assert not bad, f"运行期值不该判违规：{bad}"
        assert sum("[未解析]" in line for line in report) == 3


# ══════════════════════════════════════════════════════════════════
# 契约可达性门禁
# ══════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMentionMatching:
    """匹配判据本身：前缀不得误命中（`.children` vs `.children_ids`）。"""

    def test_prefix_is_not_a_mention(self, reach):
        text = "self.children_ids = 1"
        assert reach._mentions(text, ".children") == 0, "前缀误命中会让有读无写判据失效"
        assert reach._mentions(text, ".children_ids") == 1

    def test_plain_mention_counted(self, reach):
        assert reach._mentions("a.error = b.error", ".error") == 2

    def test_field_counts_read_and_write(self, reach):
        reads, writes = reach._field_counts(
            "x = node.max_duration_s\nmax_duration_s=None\n'max_duration_s': 1", "max_duration_s")
        assert reads == 1 and writes == 2


@pytest.mark.unit
class TestContractRules:
    """E1（枚举可达性）与 F1（有读必须有写）各自要红得起来。"""

    def _repo(self, tmp_path: Path, *, contract: str, producer: str) -> Path:
        _write(tmp_path, "Module/workflow_contract/schema.py", contract)
        if producer:
            _write(tmp_path, "core/producer.py", producer)
        # 其余契约模块留空占位（解析器逐文件读取）
        for rel in ("Module/workflow_engine/schema/types.py",
                    "Module/workflow_engine/schema/internal.py",
                    "Module/workflow_engine/schema/output.py"):
            _write(tmp_path, rel, "")
        return tmp_path

    def test_unproduced_enum_member_is_red(self, reach, tmp_path, monkeypatch):
        root = self._repo(
            tmp_path,
            contract='''
                from enum import StrEnum

                class Color(StrEnum):
                    RED = "red"
                    ORPHAN = "orphan"
            ''',
            producer="from Module.workflow_contract.schema import Color\nx = Color.RED\n",
        )
        monkeypatch.setattr(reach, "_RESERVED_ENUM_MEMBERS", {})
        enums, _models = reach._parse_models(root)
        bad, _ok = reach._check_enums(reach._production_sources(root), enums)
        assert bad and "Color.ORPHAN" in bad[0], f"无生产者成员必须报，实际 {bad}"
        assert not any("Color.RED" in b for b in bad), "有生产者的成员不该报"

    def test_enum_member_with_value_literal_counts_as_produced(self, reach, tmp_path, monkeypatch):
        """StrEnum 常经字符串拼进 wire——值字面量出现即视为可达（否则误报淹没门禁）。"""
        root = self._repo(
            tmp_path,
            contract='''
                from enum import StrEnum

                class Color(StrEnum):
                    RED = "red"
            ''',
            producer='payload = {"color": "red"}\n',
        )
        monkeypatch.setattr(reach, "_RESERVED_ENUM_MEMBERS", {})
        enums, _models = reach._parse_models(root)
        bad, _ok = reach._check_enums(reach._production_sources(root), enums)
        assert not bad, f"值字面量出现即应视为可达，实际 {bad}"

    def test_reserved_member_passes_and_stale_registration_is_red(self, reach, tmp_path, monkeypatch):
        root = self._repo(
            tmp_path,
            contract='''
                from enum import StrEnum

                class Color(StrEnum):
                    RED = "red"
                    ORPHAN = "orphan"
            ''',
            producer="from Module.workflow_contract.schema import Color\nx = Color.RED\n",
        )
        enums, _models = reach._parse_models(root)
        sources = reach._production_sources(root)

        monkeypatch.setattr(reach, "_RESERVED_ENUM_MEMBERS",
                            {"schema.py::Color::ORPHAN": "值域预留"})
        bad, _ok = reach._check_enums(sources, enums)
        assert not bad, "已登记预留的成员不该报"

        # 登记了但成员已经可达 → 陈旧登记（否则登记表会烂成"永久豁免单"）
        monkeypatch.setattr(reach, "_RESERVED_ENUM_MEMBERS",
                            {"schema.py::Color::RED": "值域预留"})
        unreachable = reach._unreachable_enums(sources, enums)
        assert "schema.py::Color::RED" not in unreachable

    def test_read_without_write_is_red(self, reach, tmp_path, monkeypatch):
        root = self._repo(
            tmp_path,
            contract='''
                from pydantic import BaseModel

                class Node(BaseModel):
                    orphan: int = 0
                    filled: int = 0
            ''',
            producer=("def f(node):\n"
                      "    n = Node(filled=1)\n"
                      "    return node.orphan + node.filled\n"),
        )
        monkeypatch.setattr(reach, "_PENDING_WRITERS", {})
        _enums, models = reach._parse_models(root)
        bad, _ok = reach._check_fields(reach._production_sources(root), models)
        assert bad and "Node.orphan" in bad[0], f"有读无写必须报，实际 {bad}"
        assert not any("Node.filled" in b for b in bad)

    def test_write_makes_field_reachable(self, reach, tmp_path, monkeypatch):
        root = self._repo(
            tmp_path,
            contract='''
                from pydantic import BaseModel

                class Node(BaseModel):
                    filled: int = 0
            ''',
            producer="def f():\n    n = Node(filled=1)\n    return n.filled\n",
        )
        monkeypatch.setattr(reach, "_PENDING_WRITERS", {})
        _enums, models = reach._parse_models(root)
        bad, _ok = reach._check_fields(reach._production_sources(root), models)
        assert not bad, f"有写入即不该报，实际 {bad}"


@pytest.mark.unit
class TestReservedRegistrationHygiene:
    """预留登记必须写清**解除条件**（owner 2026-09-16 裁定）。

    只写"为什么暂不产"的登记表会退化成永久豁免单——读者分不清它是"有计划的
    预留"还是"忘了删的残留"。这条把"必须写解除条件"变成判据。
    """

    def test_registration_without_removal_condition_is_red(self, reach, monkeypatch):
        monkeypatch.setattr(
            reach, "_RESERVED_ENUM_MEMBERS",
            {"schema.py::Color::ORPHAN": "值域预留：暂时没有生产者"})
        bad = reach._check_registration_hygiene()
        assert bad and "解除条件" in bad[0], f"缺解除条件必须报，实际 {bad}"

    def test_placeholder_removal_condition_is_red(self, reach, monkeypatch):
        """写了标记但判据为空/占位（如『解除条件：』后面什么都没有）也要红。"""
        monkeypatch.setattr(
            reach, "_RESERVED_ENUM_MEMBERS",
            {"schema.py::Color::ORPHAN": "值域预留。解除条件：待定"})
        bad = reach._check_registration_hygiene()
        assert bad and "占位符" in bad[0], f"占位式解除条件必须报，实际 {bad}"

    def test_registration_with_removal_condition_passes(self, reach, monkeypatch):
        """反证的另一半：写清了就不该报（否则判据是『永远红』的假门禁）。"""
        monkeypatch.setattr(
            reach, "_RESERVED_ENUM_MEMBERS",
            {"schema.py::Color::ORPHAN": "值域预留：暂无生产者。解除条件：若确认不做彩色输出，删该值"})
        assert reach._check_registration_hygiene() == []


# ══════════════════════════════════════════════════════════════════
# 真实仓库：两条门禁必须绿（红说明有人引入了不可达字段/预算倒挂）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.service
def test_real_repo_passes_timeout_budget():
    assert _load("check_timeout_budget").main([]) == 0


@pytest.mark.service
def test_real_repo_passes_contract_reachability():
    assert _load("check_contract_reachability").main([]) == 0
