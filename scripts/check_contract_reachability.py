#!/usr/bin/env python3
"""契约可达性门禁（#47 ②）——"声明了但到不了"不再可能悄悄存在。

## 为什么需要它

报告 RC-12：「**门禁只校验形状不校验语义可达性**（字段有读无写、枚举无生产者…）」。
pyright 会告诉你 `X.error` 类型正确，`check-types` 会告诉你生成的 TS 与模型一致——
但**没有一条**门禁会问："这个字段/枚举值，真的有人生产吗？"

实测（本脚本当前基线）在 `workflow_contract` + `workflow_engine/schema` 上：

- **6 个模型字段有读无写**：`ConstraintConfig.max_context_tokens`、
  `ConstraintConfig.max_retries_per_node`、`HITLData.rbac_level`、
  `WorkflowState.token_entries`、`DAGNode.max_duration_s`、`DBTNode.max_duration_s`
  ——读方按"它总是有值"写代码，而**没有任何生产者**，于是运行期恒为默认值。
- **23 个枚举成员无生产者**：`WorkflowErrorCode.MUTATION_LIMIT_REACHED`、
  `ResumeErrorCode.*`、`DBTNodeType.SELECTOR`…——值域声明了，引擎永远发不出来；
  前端/消费者按契约实现分支，永远进不去。

逐条修这 29 个点价值有限（下一次同样的错误仍会发生）。本门禁把它们变成
**必须表态**：新出现的不可达字段/枚举成员 → 红。

## 两条规则

| 规则 | 判据 | 未达标时 |
|---|---|---|
| E1 枚举可达性 | 成员被引用为 `EnumClass.MEMBER`，**或**其字符串值作为字面量出现在生产代码里（StrEnum 常常经 `f"..."` 拼进 wire，只查属性引用会大量误报） | 必须登记进 `_RESERVED_ENUM_MEMBERS` 并写明理由 |
| F1 字段有读必须有写 | 字段名在生产代码里被**读取**（`.field`）却从未被**写入**（`field=` 或 `"field":`） | 必须登记进 `_PENDING_WRITERS` 并写明理由 |

## 判据的边界（诚实声明）

- **名字级**匹配，不做类型级数据流：同名不同模型的字段会互相抵消。这是**回归网**而
  非完备审计——它拦得住"新加了一个没人生产的字段"，拦不住"某个同名字段碰巧有写入"。
- 匹配 = **子串预筛 + 一次性前瞻正则**（`needle(?![A-Za-z0-9_])`）：纯正则版 32s 太慢，
  纯子串版会把 `.children` 命中 `.children_ids`（实测把 `DBTNode.children` 误报成有读取）。
  写入侧仍是子串（`field=` / `"field":` / `'field':`），前缀不会误命中。
- "读取"只认属性访问 `.field` 与 `["field"]`；`getattr(obj, "field")` 扫不到。
- 生产者扫描范围 = `core/` + `Module/` 生产代码，**排除**契约模块自身与 tests。
  契约模块里 `field: int = 3` 的默认值不算生产者——那是声明，不是生产。

用法:

    python scripts/check_contract_reachability.py           # 违规 → 退出码 1
    python scripts/check_contract_reachability.py --list    # 列出全部登记项与统计
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 被检查的"数据模型/契约"模块。两份都算：跨边契约（workflow_contract）与
# 引擎内部数据模型（workflow_engine/schema）——报告点名的 6 个字段与 6 个
# `WorkflowErrorCode` 分别落在两边。
_CONTRACT_MODULES = (
    "Module/workflow_contract/schema.py",
    "Module/workflow_engine/schema/types.py",
    "Module/workflow_engine/schema/internal.py",
    "Module/workflow_engine/schema/output.py",
)

_SCAN_DIRS = ("core", "Module")

# ── E1：无生产者的枚举成员（登记 = 承认"值域预留"，并在注释里写清用途）────────
# 键 = "模块文件名::枚举类::成员"。值 = 为什么后端暂时不生产它。
_RESERVED_ENUM_MEMBERS: dict[str, str] = {
    "schema.py::SubmitRejectionCode::PLAN_TOO_LARGE": "拒绝码值域预留：计划规模上限尚未在引擎侧实现",
    "schema.py::SubmitRejectionCode::CONTEXT_WINDOW_EXCEEDED": "拒绝码值域预留：上下文窗口预检尚未实现",
    "schema.py::SubmitRejectionCode::TOKEN_BUDGET_EXCEEDED": "拒绝码值域预留：token 预算预检尚未实现",
    "schema.py::ReturnMode::STRUCTURED": "返回模式预留：结构化返回尚未接入（当前只用 summary/raw）",
    "types.py::WorkflowErrorCode::MUTATION_LIMIT_REACHED": "错误码值域预留：max_mutations 硬约束未实现（#45）",
    "types.py::WorkflowErrorCode::CANCELLED_BY_USER": "错误码值域预留：用户取消当前走 WorkflowStatus.CANCELLED 通道，不走 error_code",
    "types.py::ResumeErrorCode::CHECKPOINT_CORRUPTED": "恢复错误码预留：checkpoint 损坏目前记 warning 后丢弃，不产出该码",
    "types.py::ResumeErrorCode::CHECKPOINT_VERSION_MISMATCH": "恢复错误码预留：版本不匹配同上（隔离不恢复）",
    "types.py::ResumeErrorCode::PENDING_APPROVALS_FAILED": "恢复错误码预留：待审批恢复失败尚未细分到该码",
    "types.py::ResumeErrorCode::NODE_RECOVERY_FAILED": "恢复错误码预留：节点恢复失败尚未细分到该码",
    "internal.py::ValidationIssueType::INVALID_INPUT": "校验问题类型预留：input_bindings 校验目前只报 UNKNOWN_TOOL/MISSING_CONFIRMATION",
    "internal.py::ValidationIssueType::MISSING_CONFIRMATION": "校验问题类型预留：确认缺失校验尚未产出该类型",
    "internal.py::DBTNodeType::SEQUENCE": "DBT 节点类型预留：当前规划器只产 ACTION 类节点",
    "internal.py::DBTNodeType::SELECTOR": "DBT 节点类型预留（同上）",
    "internal.py::DBTNodeType::PARALLEL": "DBT 节点类型预留（同上）",
    "internal.py::DBTNodeType::CONDITION": "DBT 节点类型预留（同上）",
    "internal.py::ExperienceStatus::UNVERIFIED": "经验状态预留：经验库尚未实现验证流水线",
    "internal.py::ExperienceStatus::VERIFIED": "经验状态预留（同上）",
    "internal.py::ExperienceStatus::DEPRECATED": "经验状态预留（同上）",
    "output.py::AgentType::WORKER": "子智能体类型预留：当前只产 orchestrator/planner/executor 之外的默认类型",
    "output.py::TokenComponentType::SYNTHESIZER": "token 分项预留：分项统计尚未细分到该组件",
    "output.py::TokenComponentType::REPLANNER": "token 分项预留（同上）",
    "output.py::TokenComponentType::REDUCER": "token 分项预留（同上）",
}

# ── F1：有读无写的字段（登记 = 已知缺口，写清"该谁补"或"为什么允许"）─────────
_PENDING_WRITERS: dict[str, str] = {
    "ConstraintConfig.max_context_tokens": "约束未实现（#45：实现硬约束或修文档承诺）",
    "ConstraintConfig.max_retries_per_node": "约束未实现（#45 同族）",
    "HITLData.rbac_level": "RBAC 分级尚未落地：字段在契约里，生产者不填",
    "WorkflowState.token_entries": "token 分项账本尚无写入点（与 TokenComponentType 预留同源）",
    "DAGNode.max_duration_s": "节点级超时未接线：字段被 execution.py 读取做 wait_for，但规划器不填",
    "DBTNode.max_duration_s": "节点级超时未接线（同上，DBT 变体）",
}


def _production_sources(root: Path | None = None) -> list[tuple[Path, str]]:
    """扫描生产代码。`root` 可指向合成 fixture 仓库（供单测用）。"""
    base_root = root or _ROOT
    out: list[tuple[Path, str]] = []
    contracts = {(base_root / m).resolve() for m in _CONTRACT_MODULES}
    for base in _SCAN_DIRS:
        for path in sorted((base_root / base).rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            if path.resolve() in contracts:
                continue
            out.append((path, path.read_text(encoding="utf-8")))
    return out


def _parse_models(root: Path | None = None) -> tuple[dict[tuple[str, str], dict[str, str | None]],
                                                    dict[tuple[str, str], list[str]]]:
    """返回 ({（文件名, 枚举类）: {成员: 值}}, {（文件名, 模型类）: [字段名]}）。"""
    base_root = root or _ROOT
    enums: dict[tuple[str, str], dict[str, str | None]] = {}
    models: dict[tuple[str, str], list[str]] = {}
    for rel in _CONTRACT_MODULES:
        tree = ast.parse((base_root / rel).read_text(encoding="utf-8"))
        fname = Path(rel).name
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(b) for b in node.bases]
            if any("Enum" in b for b in bases):
                members: dict[str, str | None] = {}
                for item in node.body:
                    if (isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)
                            and item.targets[0].id.isupper()):
                        try:
                            value = ast.literal_eval(item.value)
                        except (ValueError, SyntaxError):
                            value = None
                        members[item.targets[0].id] = value if isinstance(value, str) else None
                enums[(fname, node.name)] = members
            if any("BaseModel" in b for b in bases):
                models[(fname, node.name)] = [
                    item.target.id for item in node.body
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                ]
    return enums, models


_MENTION_CACHE: dict[str, "re.Pattern[str]"] = {}


def _mentions(text: str, needle: str) -> int:
    """`needle` 作为**完整标识片段**出现的次数。

    先用 C 级子串判断是否可能出现（绝大多数文件在这里就短路了），再用一次
    `(?![A-Za-z0-9_])` 前瞻排掉前缀误命中——纯正则版实测 32s，纯子串版会把
    `.children` 判成命中 `.children_ids`（实测把 `DBTNode.children` 误报成"有读"）。
    两者结合：<3s 且无前缀误判。
    """
    if needle not in text:
        return 0
    pattern = _MENTION_CACHE.get(needle)
    if pattern is None:
        pattern = re.compile(re.escape(needle) + r"(?![A-Za-z0-9_])")
        _MENTION_CACHE[needle] = pattern
    return len(pattern.findall(text))


def _enum_produced(text: str, cls: str, member: str, value: str | None) -> bool:
    """成员是否可达：`EnumClass.MEMBER` 出现，或其字符串值作为字面量出现。"""
    if _mentions(text, f"{cls}.{member}"):
        return True
    return bool(value) and (f'"{value}"' in text or f"'{value}'" in text)


def _field_counts(text: str, field: str) -> tuple[int, int]:
    """字段在单文件里的（读, 写）次数——读 = `.field`；写 = `field=` / `"field":`。"""
    reads = _mentions(text, f".{field}")
    writes = (text.count(f"{field}=") + text.count(f"{field} =")
              + text.count(f'"{field}":') + text.count(f"'{field}':"))
    return reads, writes


def _unreachable_enums(sources: list[tuple[Path, str]],
                       enums: dict[tuple[str, str], dict[str, str | None]]) -> set[str]:
    """当前无生产者的枚举成员集合（键 = 文件名::类::成员）。"""
    out: set[str] = set()
    for (fname, cls), members in enums.items():
        for member, value in members.items():
            if any(_enum_produced(text, cls, member, value) for _p, text in sources):
                continue
            out.add(f"{fname}::{cls}::{member}")
    return out


def _read_without_write(sources: list[tuple[Path, str]],
                        models: dict[tuple[str, str], list[str]]) -> set[str]:
    """当前"有读无写"的字段集合（键 = 模型类.字段）。"""
    out: set[str] = set()
    for (_fname, cls), fields in models.items():
        for field in set(fields):
            reads = writes = 0
            for _p, text in sources:
                r, w = _field_counts(text, field)
                reads += r
                writes += w
            if reads > 0 and writes == 0:
                out.add(f"{cls}.{field}")
    return out


def _check_enums(sources: list[tuple[Path, str]],
                 enums: dict[tuple[str, str], dict[str, str | None]]) -> tuple[list[str], list[str]]:
    bad: list[str] = []
    ok: list[str] = []
    for (fname, cls), members in sorted(enums.items()):
        for member, value in sorted(members.items()):
            ref = f"{cls}.{member}"
            produced = any(_enum_produced(text, cls, member, value) for _p, text in sources)
            key = f"{fname}::{cls}::{member}"
            if produced:
                ok.append(f"  [可达] {ref}")
            elif key in _RESERVED_ENUM_MEMBERS:
                ok.append(f"  [预留] {ref} —— {_RESERVED_ENUM_MEMBERS[key]}")
            else:
                bad.append(
                    f"{fname}::{ref} 无生产者——后端没有任何地方产出它"
                    f"（值 {value!r} 也未作为字面量出现）。\n"
                    f"     要么接上生产者，要么登记进 _RESERVED_ENUM_MEMBERS 写明"
                    f"『这是值域预留、为什么暂时不产』")
    return bad, ok


def _check_fields(sources: list[tuple[Path, str]],
                  models: dict[tuple[str, str], list[str]]) -> tuple[list[str], list[str]]:
    bad: list[str] = []
    ok: list[str] = []
    for (_fname, cls), fields in sorted(models.items()):
        for field in sorted(set(fields)):
            reads = writes = 0
            for _p, text in sources:
                r, w = _field_counts(text, field)
                reads += r
                writes += w
            key = f"{cls}.{field}"
            if reads == 0 or writes > 0:
                ok.append(f"  [正常] {key}（读 {reads} / 写 {writes}）")
            elif key in _PENDING_WRITERS:
                ok.append(f"  [已知缺口] {key}（读 {reads} / 写 0）—— {_PENDING_WRITERS[key]}")
            else:
                bad.append(
                    f"{cls}.{field} 有读无写（读 {reads} 处 / 写 0 处）——"
                    f"读方按『它总是有值』写代码，而没有任何生产者，运行期恒为默认值。\n"
                    f"     要么补生产者，要么登记进 _PENDING_WRITERS 写明该谁补/为何允许")
    return bad, ok


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：违规 → 退出码 1。"""
    parser = argparse.ArgumentParser(description="契约可达性门禁（#47 ②）")
    parser.add_argument("--list", action="store_true", help="列出全部条目与登记项")
    parser.add_argument("--root", default=None, help="仓库根（默认脚本所在仓库；单测用合成树）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else None
    sources = _production_sources(root)
    enums, models = _parse_models(root)
    enum_bad, enum_ok = _check_enums(sources, enums)
    field_bad, field_ok = _check_fields(sources, models)
    bad = enum_bad + field_bad

    # 陈旧登记：登记项对应的"缺口"已经不存在了（有人补上了生产者）→ 该删掉登记，
    # 否则登记表会越长越像"已接受的技术债清单"，而它本该是"当前真实的缺口清单"。
    # 只算一次（每个 key 各算一遍会让本脚本从 1s 变成 2 分钟——O(n) 扫描 × 登记项数）
    unreachable = _unreachable_enums(sources, enums)
    read_no_write = _read_without_write(sources, models)
    seen_enums = {f"{fname}::{cls}::{m}" for (fname, cls), ms in enums.items() for m in ms}
    seen_fields = {f"{cls}.{f}" for (_fn, cls), fs in models.items() for f in fs}
    for key in sorted(_RESERVED_ENUM_MEMBERS):
        if key not in seen_enums:
            bad.append(f"陈旧登记：_RESERVED_ENUM_MEMBERS 的 {key} 已不存在（枚举成员被删/改名？）")
        elif key not in unreachable:
            bad.append(f"陈旧登记：{key} 已被生产（不再是预留）——请从 _RESERVED_ENUM_MEMBERS 删除")
    for key in sorted(_PENDING_WRITERS):
        if key not in seen_fields:
            bad.append(f"陈旧登记：_PENDING_WRITERS 的 {key} 字段已不存在")
        elif key not in read_no_write:
            bad.append(f"陈旧登记：{key} 已补上写入方（或已无读取方）——请从 _PENDING_WRITERS 删除")

    if args.list:
        print(f"枚举可达性（{sum(len(m) for m in enums.values())} 个成员）：")
        for line in enum_ok:
            print(line)
        print(f"\n字段读写（{sum(len(f) for f in models.values())} 个字段）：")
        for line in field_ok:
            print(line)
        print()

    if bad:
        print(f"契约可达性违规 {len(bad)} 项：")
        for item in bad:
            print(f"  - {item}")
        print("\n背景（RC-12）：形状门禁（pyright / check-types）不会问『这个字段/枚举值"
              "真的有人生产吗』，于是声明了却到不了的值域长期无人发现。")
        return 1

    reserved = len(_RESERVED_ENUM_MEMBERS)
    pending = len(_PENDING_WRITERS)
    print(f"契约可达性门禁通过：枚举成员全部可达或在册预留（{reserved} 个预留）；"
          f"字段无新增『有读无写』（{pending} 个已知缺口在册）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
