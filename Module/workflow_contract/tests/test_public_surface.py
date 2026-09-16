"""D2/D3：`Module.workflow_contract` 公共导入面冻结 + 真实 openai 路径仍可用（Issue D）。

`Module/workflow_contract/__init__.py` 是**契约层唯一装配点**，其 re-export 是刻意的
公共 API。Issue D 的修复若把重名（如 `AgentInfra`）从装配点移除或改成懒加载，
下面两张网必须同时挡住：

- **D2** 冻结公共面：`__all__`、逐名 `from ... import`、`import *`、以及
  「重导出对象与定义模块内**同一对象**」——懒加载接错模块 / 漏名都会红。
- **D3** 反向证明修复没把真消费者切断：`AgentInfra` 仍能装配**真的 `AsyncOpenAI`**
  实例并被消费方（workflow_engine 装配链）导入——satisfies「真需要 openai 的路径仍可用」。

D3 的引擎导入放子进程：`Module/workflow_engine` 会把 openai/httpx 一并拉进来，
在本进程（pytest 会话）里做会污染 D1 所依赖的「干净导入图」前提（子进程探针不受影响，
但保持本进程轻量对同会话其它用例更友好）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "conftest.py").is_file() and (p / "core").is_dir() and (p / "Module").is_dir()
)
"""仓库根——显式 cwd，使 `python -c` 的 sys.path[0] 含 `core` / `Module`（不猜层级）。"""

_PROBE_TIMEOUT_S = 120

_EXPECTED_PUBLIC: frozenset[str] = frozenset(
    {
        # 跨边运行时注入容器
        "AgentInfra",
        # 纯函数工具
        "format_tools",
        "parse_iso",
        "strip_markdown_fence",
        # 标识符别名
        "AgentId",
        "ApprovalId",
        "NodeId",
        "SessionId",
        "WorkflowId",
        # 基础枚举
        "SubmitRejectionCode",
        "WorkflowStatus",
        # 约束
        "ConstraintConfig",
        "WorkflowConstraints",
        # 委派协议
        "DelegationContext",
        "DelegationResult",
        "DelegationStatus",
        "ReturnMode",
        "SubmitError",
        # HITL 协议
        "HITLData",
        "HITLNotifier",
        # 事件
        "WorkflowEvent",
        "WorkflowEventType",
    }
)
"""契约层公共面清单（冻结自 `Module/workflow_contract/__init__.py::__all__`，22 名）。

**只增不减**：任何名字要退出公共面，等于对全仓消费方做破坏性变更，
必须单独走评审——本清单就是让那件事无法静默发生。
"""

_STAR_IMPORT_PROBE = r"""
import json
ns = {}
exec("from Module.workflow_contract import *", ns)
print(json.dumps(sorted(n for n in ns if not n.startswith("__"))))
"""

_ENGINE_PATH_PROBE = r"""
import json, sys

# 真消费者装配链：core 侧注入容器 → engine 侧消费
from openai import AsyncOpenAI

from Module.workflow_contract import AgentInfra, ConstraintConfig

client = AsyncOpenAI(api_key="probe-key", base_url="http://127.0.0.1:1/v1")
infra = AgentInfra(
    llm=client,
    milvus=None,
    http=None,
    tool_registry=object(),
    constraints=ConstraintConfig(),
)

import Module.workflow_engine  # 真消费者：引擎包整体仍可导入

print(json.dumps({
    "openai_root_loaded": "openai" in sys.modules,
    "llm_is_async_openai": isinstance(infra.llm, AsyncOpenAI),
    "max_dag_nodes": infra.constraints.max_dag_nodes,
    "engine_loaded": "Module.workflow_engine" in sys.modules,
}))
"""


_DATACLASS_PROBE = r"""
import dataclasses, json, sys

from Module.workflow_contract import AgentInfra, ConstraintConfig

# 关键：走到这里 openai 都还没被导入。AgentInfra 的字段注解必须是**字符串**
# （PEP 563 `from __future__ import annotations`）——否则上面 import 的那一刻
# class body 就会对 AsyncOpenAI 求值 → NameError。这正是「仅类型层导入」成立的前提。
infra = AgentInfra(
    llm=object(),
    milvus=None,
    http=None,
    tool_registry=object(),
    constraints=ConstraintConfig(),
)
fields = {f.name: f.type for f in dataclasses.fields(AgentInfra)}

print(json.dumps({
    "openai_loaded": "openai" in sys.modules,
    "httpx_loaded": "httpx" in sys.modules,
    "field_names": sorted(fields),
    "llm_annotation_is_str": isinstance(fields["llm"], str),
    "llm_annotation": fields["llm"] if isinstance(fields["llm"], str) else repr(fields["llm"]),
    "usable_max_dag_nodes": infra.constraints.max_dag_nodes,
}))
"""


def _run_probe(source: str) -> dict | list:
    proc = subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
    )
    assert proc.returncode == 0, (
        f"子进程探针失败（exit {proc.returncode}）\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── D2：公共导入面 ──────────────────────────────────────────────


def test_d2_public_surface_matches_frozen_manifest():
    """D2：`__all__` 与冻结清单逐名一致（不多不少）。"""
    import Module.workflow_contract as contract

    assert set(contract.__all__) == _EXPECTED_PUBLIC, (
        "契约层公共面漂移："
        f"新增 {sorted(set(contract.__all__) - _EXPECTED_PUBLIC)}；"
        f"丢失 {sorted(_EXPECTED_PUBLIC - set(contract.__all__))}"
    )


def test_d2_every_name_importable_from_package():
    """D2：每个公共名都能 `from Module.workflow_contract import <name>` 拿到。"""
    import Module.workflow_contract as contract

    missing = [n for n in sorted(_EXPECTED_PUBLIC) if not hasattr(contract, n)]
    assert missing == [], f"装配点丢失公共名：{missing}"


def test_d2_reexports_are_identical_objects():
    """D2：重导出必须是定义模块里的**同一对象**（懒加载接错模块会在此处红）。"""
    import Module.workflow_contract as contract
    from Module.workflow_contract import infra, schema, utils

    assert contract.AgentInfra is infra.AgentInfra
    assert contract.ConstraintConfig is schema.ConstraintConfig
    assert contract.WorkflowEvent is schema.WorkflowEvent
    assert contract.HITLNotifier is schema.HITLNotifier
    assert contract.SessionId is schema.SessionId
    assert contract.format_tools is utils.format_tools
    assert contract.parse_iso is utils.parse_iso
    assert contract.strip_markdown_fence is utils.strip_markdown_fence


def test_d2_star_import_keeps_every_public_name():
    """D2：`import *` 不得静默丢名（PEP 562 懒加载 + `__all__` 的经典陷阱）。"""
    names = set(_run_probe(_STAR_IMPORT_PROBE))
    assert names == _EXPECTED_PUBLIC, (
        f"`from Module.workflow_contract import *` 缺失："
        f"{sorted(_EXPECTED_PUBLIC - names)}；多出：{sorted(names - _EXPECTED_PUBLIC)}"
    )


def test_d2_agent_infra_constructible_in_process():
    """D2：`AgentInfra` dataclass 仍可就地构造（字段默认值/类型未因修复变形）。"""
    from unittest.mock import MagicMock

    from Module.workflow_contract import AgentInfra, ConstraintConfig

    infra = AgentInfra(
        llm=MagicMock(),
        milvus=None,
        http=None,
        tool_registry=object(),
        constraints=ConstraintConfig(),
    )
    assert infra.constraints.max_dag_nodes == 20
    assert infra.orphan_tools == set()
    assert infra.background_task_results is None
    assert infra.terminal_result_sink is None
    assert isinstance(infra.llm_model, str)  # env TASK_SYSTEM_MODEL 或默认值


# ── D3：真需要 openai 的路径 ────────────────────────────────────


def test_d3_real_openai_consumer_path_still_works():
    """D3：装配真 `AsyncOpenAI` + 导入引擎包——懒化/类型化后真消费者不受影响。"""
    report = _run_probe(_ENGINE_PATH_PROBE)
    assert isinstance(report, dict)
    assert report["openai_root_loaded"] is True
    assert report["llm_is_async_openai"] is True
    assert report["engine_loaded"] is True
    assert report["max_dag_nodes"] == 20


def test_d3_agent_infra_dataclass_works_without_openai_loaded():
    """D3（承重假设）：不导入 openai 也能造 `AgentInfra`——字段注解必须是字符串。

    这是「`openai` 收进 TYPE_CHECKING」成立的前提：一旦有人删掉 `infra.py` 的
    `from __future__ import annotations`（或把注解改成运行时求值），字段注解会在
    class body 求值 → `Module.workflow_contract` 直接 ImportError。本用例把那条
    隐式耦合变成显式断言（同时钉住字段名——dataclass 形状不得静默变化）。
    """
    report = _run_probe(_DATACLASS_PROBE)
    assert isinstance(report, dict)
    assert report["openai_loaded"] is False
    assert report["httpx_loaded"] is False
    assert report["llm_annotation_is_str"] is True
    assert report["llm_annotation"] == "AsyncOpenAI"
    assert report["field_names"] == [
        "background_task_results",
        "constraints",
        "http",
        "llm",
        "llm_model",
        "milvus",
        "orphan_tools",
        "terminal_result_sink",
        "tool_registry",
    ]
    assert report["usable_max_dag_nodes"] == 20
