"""CI 信任边界静态门禁（P0-02，2026-08-10 全项目 review）。

防止不受信 pull_request 代码在持久化 self-hosted runner 上接触宿主凭据。
拒绝两类组合：

1. 可在 pull_request 上触发的 job 使用 `runs-on: self-hosted` —— 恶意 PR 的
   Makefile/pytest/Python/`npm ci` 会在持久 runner 上以宿主权限执行任意代码。
2. 任何 PR 可达 job 的步骤引用宿主绝对路径凭据（`/home/.../.env`），或
   `cp`/`source` 宿主 `.env` —— 直接把宿主密钥暴露给 PR 代码。

判定规则：
- job 的 `if` 若显式限定 push-only（字符串含 `github.event_name == 'push'`
  且不含 `pull_request`），视为受信 push job，允许 self-hosted + 宿主 .env；
  否则视为 PR 可达，必须 GitHub-hosted、不得触碰宿主路径。
- 步骤 `run`/`name` 中出现 `/home/<...>/.env`、或以 `/home/` 开头的绝对路径、
  `cp <宿主绝对路径> .env`、`source <宿主绝对路径>.env` 均视为访问宿主凭据。

用法：
    python scripts/check_ci_isolation.py [workflow.yml]   # 默认 .github/workflows/ci.yml
                                                          # 违规打印并退出码 1
    python -c "from scripts.check_ci_isolation import check_workflow_file; ..."
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_WORKFLOW = ".github/workflows/ci.yml"

# 宿主绝对路径凭据特征（仓库不应引用任何 /home/... 或宿主 .env）
_HOST_PATH_MARKERS = (
    "/home/",
)
_HOST_ENV_MARKERS = (
    "cp /home/",
    "source /home/",
    ". /home/",
)


def _is_push_only(job: dict[str, Any]) -> bool:
    """job 是否显式限定只跑受信触发（push / workflow_dispatch / schedule）。

    非 PR 触发（push 到受信 ref、受信管理员手工 workflow_dispatch、定时 schedule）
    视为受信——不受信 pull_request 永不触发它们。if 表达式含任一受信 guard 且不含
    pull_request → 受信 job（允许 self-hosted + 宿主凭据，F1 external E2E 用）。
    """
    cond = str(job.get("if", ""))
    trusted_guard = any(g in cond for g in (
        "== 'push'", "== 'workflow_dispatch'", "== 'schedule'"))
    has_pr_ref = "pull_request" in cond
    return trusted_guard and not has_pr_ref


def _step_text(step: dict[str, Any]) -> str:
    """步骤的 run + name 拼接文本（用于路径/凭据特征扫描）。"""
    return f"{step.get('name', '')}\n{step.get('run', '')}"


def check_workflow_file(path: str | Path) -> list[str]:
    """检查一个 workflow 文件的 PR 信任边界，返回违规列表（空 = 通过）。

    Args:
        path: workflow YAML 文件路径。

    Returns:
        违规描述列表；为空表示无违规。
    """
    violations: list[str] = []
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    jobs: dict[str, Any] = (data or {}).get("jobs", {}) or {}
    for name, job in jobs.items():
        runs_on = str(job.get("runs-on", ""))
        steps: list[dict[str, Any]] = job.get("steps", []) or []
        if _is_push_only(job):
            # 受信 push job：允许 self-hosted + 宿主 .env（受信 ref，仅维护者可触发）。
            # 这里不扫描宿主路径——push-only guard 本身即信任边界（门禁脚本负责
            # 验证 PR 可达 job 永不 self-hosted、永不引用宿主路径）。
            continue
        # PR 可达 job：禁止 self-hosted、禁止宿主凭据路径
        if "self-hosted" in runs_on:
            violations.append(
                f"job '{name}' 可在 pull_request 上运行却使用 runs-on: {runs_on} —— "
                "不受信 PR 会在持久化 self-hosted runner 上执行任意代码")
        for step in steps:
            text = _step_text(step)
            for marker in _HOST_PATH_MARKERS:
                if marker in text:
                    violations.append(
                        f"job '{name}'（PR 可达）步骤引用宿主绝对路径 '{marker}'")
            for marker in _HOST_ENV_MARKERS:
                if marker in text:
                    violations.append(
                        f"job '{name}'（PR 可达）步骤 source/cp 宿主 .env '{marker}'")
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：检查 workflow 文件，违规退出码 1。

    Args:
        argv: 命令行参数（可选 workflow 路径）。

    Returns:
        0 表示通过，1 表示违规。
    """
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else _DEFAULT_WORKFLOW
    violations = check_workflow_file(path)
    if violations:
        print(f"CI 信任边界检查失败：{path}")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"CI 信任边界检查通过：{path}（无 PR 可达 self-hosted job，无宿主凭据路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
