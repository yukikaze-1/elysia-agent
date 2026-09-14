"""CI 信任边界门禁测试（P0-02，marker=unit）。

验证 scripts/check_ci_isolation.py：
- 拒绝旧模式：pull_request 可达 job 用 self-hosted、且 step 引用宿主 /home/.../.env。
- 接受新模式：PR job 用 GitHub-hosted（ubuntu-latest）、无宿主路径；受信 push job
  显式 if 限定 push-only 后可 self-hosted。
- 拒绝无法静态判定的 runs-on（表达式）——拒答而非判绿。
- self-hosted job 的 workspace 清理必须密闭（禁裸 glob）且带两道守卫。
- 真实 workflow 的结构（Frontend push/PR 分流）必须被钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.check_ci_isolation import check_workflow_file

pytestmark = pytest.mark.unit


def _write(tmp_path, workflow_yaml: str):
    """写一个临时 workflow 文件并返回其路径。"""
    p = tmp_path / "ci.yml"
    p.write_text(workflow_yaml, encoding="utf-8")
    return str(p)


# 规范写法：两道守卫 + **显式检查清理退出码**（fail-closed）。
# 四处真实复用点（ci backend/frontend、nightly full/external）都是这个形状。
_GUARDED_CLEANUP = (
    '          WS="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE 未设置——拒绝清理}"\n'
    '          if [ "$WS" = "/" ]; then\n'
    '            echo "FATAL: GITHUB_WORKSPACE 解析为 / ——拒绝清理"\n'
    '            exit 1\n'
    '          fi\n'
    '          if ! find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; then\n'
    '            echo "FATAL: workspace 清理失败"\n'
    '            exit 1\n'
    '          fi\n')


_OLD_INSECURE = """\
name: CI
on:
  push: {branches: [main]}
  pull_request: {branches: [main]}
jobs:
  quick:
    name: Quick
    runs-on: self-hosted
    steps:
      - name: Load .env
        run: cp /home/yomu/elysia-agent/.env .env
      - name: Test
        run: make test-quick
"""

_NEW_SECURE = """\
name: CI
on:
  push: {branches: [main]}
  pull_request: {branches: [main]}
jobs:
  pr-gate:
    name: PR Gate
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {persist-credentials: false}
      - name: Test
        run: python -m pytest -q
  quick:
    name: Quick (self-hosted, push only)
    if: github.event_name == 'push'
    runs-on: self-hosted
    steps:
      - name: Load .env
        run: cp /home/yomu/elysia-agent/.env .env
      - name: Test
        run: make test-quick
"""

_NEW_SECURE_NO_HOST_ENV_PUSH = """\
name: CI
on:
  push: {branches: [main]}
  pull_request: {branches: [main]}
jobs:
  pr-gate:
    name: PR Gate
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - name: Test
        run: python -m pytest -q
  quick:
    name: Quick
    if: github.event_name == 'push'
    runs-on: self-hosted
    steps:
      - name: Test
        run: make test-quick
"""


class TestRejectInsecure:
    """旧模式必须被拒绝（P0-02：PR 可达 self-hosted + 宿主 .env）。"""

    def test_self_hosted_pr_job_and_host_env_rejected(self, tmp_path):
        violations = check_workflow_file(_write(tmp_path, _OLD_INSECURE))
        joined = "\n".join(violations)
        assert any("self-hosted" in v for v in violations), joined
        assert any("/home/" in v and ".env" in v for v in violations), joined


class TestAcceptSecure:
    """新模式必须通过：PR 用 GitHub-hosted 无宿主路径；push-only self-hosted 受限。"""

    def test_pr_github_hosted_push_self_hosted_accepted(self, tmp_path):
        violations = check_workflow_file(_write(tmp_path, _NEW_SECURE))
        assert violations == []

    def test_push_self_hosted_without_host_env_accepted(self, tmp_path):
        violations = check_workflow_file(_write(tmp_path, _NEW_SECURE_NO_HOST_ENV_PUSH))
        assert violations == []

    def test_pr_self_hosted_alone_still_rejected(self, tmp_path):
        """PR 可达 self-hosted（即使不引用 .env）仍违规。"""
        yaml_text = """\
name: CI
on:
  pull_request: {branches: [main]}
jobs:
  quick:
    name: Quick
    runs-on: self-hosted
    steps:
      - name: Test
        run: make test-quick
"""
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("self-hosted" in v for v in violations)


class TestDeadJobRule:
    """死 job 判定（2026-09-14 CI 性能专项）——`if` 守卫引用未在 `on:` 声明的事件。

    存在理由：ci.yml 曾有一条 `if: github.event_name == 'workflow_dispatch'` 的
    external job，而该 workflow 的 `on:` 只有 push/pull_request——45 分钟的真实
    LLM/DB E2E 配置从未执行过一次，且因为 `_is_push_only` 会把它当受信 job
    **跳过全部检查**，这个写法同时是一条绕过本门禁的静默通道。
    每条规则都要红得起来，否则「注释不是机制」反过来也成立。
    """

    def test_dead_job_is_rejected(self, tmp_path):
        yaml_text = """\
name: CI
on:
  push: {branches: [main]}
jobs:
  external:
    if: github.event_name == 'workflow_dispatch'
    runs-on: self-hosted
    steps:
      - name: Test
        run: make test-external
"""
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("死 job" in v for v in violations), violations

    def test_declared_event_guard_is_accepted(self, tmp_path):
        """`on:` 里声明了该事件 → 不是死 job（这次修复后的 nightly.yml 形态）。"""
        yaml_text = """\
name: Manual
on:
  workflow_dispatch:
    inputs:
      suite: {type: choice}
jobs:
  full:
    if: github.event_name == 'workflow_dispatch' && inputs.suite == 'full'
    runs-on: self-hosted
    steps:
      - name: Test
        run: make test-full
"""
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []

    def test_reusable_workflow_guard_is_not_a_dead_job(self, tmp_path):
        """`on: workflow_call` 的 `github.event_name` 取自调用方 → 不判死 job。

        不豁免的话，本仓加第一个 reusable workflow 就会让 CI 假红。
        注意：这只关掉死 job 判定——无 guard 的 workflow_call job 依旧算 PR 可达。
        """
        yaml_text = """\
name: Reusable
on:
  workflow_call:
jobs:
  work:
    if: github.event_name == 'push'
    runs-on: self-hosted
    steps:
      - name: Test
        run: make test-quick
"""
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []

    def test_pull_request_only_workflow_cannot_fake_a_push_guard(self, tmp_path):
        """反向：PR-only workflow 里写 `if: … == 'push'` 想蹭免检 → 必须报死 job。

        这条同时守住「受信判定不能被自证」：`_is_push_only` 会因该守卫把 job
        当受信并跳过其余检查，所以死 job 判定必须**先于**那条 continue。
        """
        yaml_text = """\
name: CI
on:
  pull_request: {branches: [main]}
jobs:
  sneaky:
    if: github.event_name == 'push'
    runs-on: self-hosted
    steps:
      - name: Test
        run: cp /home/yomu/elysia-agent/.env .env
"""
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("死 job" in v for v in violations), violations


class TestQuoteInsensitivity:
    """`== 'push'` 与 `== \"push\"` 语义相同——只认单引号会把正确加了 guard 的
    受信 job 误判成不受信（假红），而那会逼人去删 guard，正好是反方向。"""

    def test_double_quoted_push_guard_is_treated_as_trusted(self, tmp_path):
        yaml_text = """\
name: CI
on:
  push: {branches: [main]}
  pull_request: {branches: [main]}
jobs:
  backend:
    if: github.event_name == "push"
    runs-on: self-hosted
    steps:
      - name: Load .env
        run: cp /home/yomu/elysia-agent/.env .env
"""
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []


class TestCliDefaultScan:
    """`main()` 默认扫描**全部** workflow——手写文件名清单时新增第四个文件没人看。"""

    def test_main_scans_every_workflow_and_refuses_when_none(self, tmp_path, monkeypatch):
        from scripts import check_ci_isolation

        # 空目录 → 拒答（退出码 2），不得报绿
        monkeypatch.setattr(check_ci_isolation, "_WORKFLOWS_DIR", str(tmp_path))
        assert check_ci_isolation.main([]) == 2

        # 目录里有一个死 job 的 workflow → 扫到并红
        (tmp_path / "a.yml").write_text(
            "name: A\non:\n  push: {branches: [main]}\njobs:\n"
            "  x:\n    if: github.event_name == 'schedule'\n"
            "    runs-on: self-hosted\n    steps:\n      - run: echo hi\n",
            encoding="utf-8")
        assert check_ci_isolation.main([]) == 1

        # 换成合法文件 → 绿
        (tmp_path / "a.yml").write_text(
            "name: A\non:\n  push: {branches: [main]}\njobs:\n"
            "  x:\n    if: github.event_name == 'push'\n"
            "    runs-on: self-hosted\n    steps:\n      - run: echo hi\n",
            encoding="utf-8")
        assert check_ci_isolation.main([]) == 0


class TestUnresolvableRunsOnRule:
    """PR 可达 job 的 `runs-on` 必须可静态判定为 GitHub-hosted（拒答规则）。

    存在理由：旧判据只查「`self-hosted` 这个子串在不在 `runs-on` 里」。那么
    `runs-on: ${{ vars.CI_RUNNER }}`（仓库变量指向自建机器）一个特征子串都不含，
    直接判绿——而 job 实际落在哪类 runner 上完全取决于仓库设置。这是「门禁的
    三种假绿」之③：判据本身无从成立时，必须拒答，不能默认安全。
    """

    def test_expression_runs_on_is_refused(self, tmp_path):
        yaml_text = """\
name: CI
on:
  pull_request: {branches: [main]}
jobs:
  gate:
    name: Gate
    runs-on: ${{ vars.CI_RUNNER }}
    steps:
      - name: Test
        run: python -m pytest -q
"""
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("无法静态判定" in v for v in violations), violations

    def test_unknown_runner_label_is_refused(self, tmp_path):
        """既不是已知托管镜像名、也不是 self-hosted → 同样无从判定，拒答。"""
        yaml_text = """\
name: CI
on:
  pull_request: {branches: [main]}
jobs:
  gate:
    name: Gate
    runs-on: my-custom-pool
    steps:
      - name: Test
        run: python -m pytest -q
"""
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("无法静态判定" in v for v in violations), violations

    def test_matrix_expression_is_refused(self, tmp_path):
        yaml_text = """\
name: CI
on:
  pull_request: {branches: [main]}
jobs:
  gate:
    name: Gate
    strategy:
      matrix: {os: [ubuntu-latest]}
    runs-on: ${{ matrix.os }}
    steps:
      - name: Test
        run: python -m pytest -q
"""
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("无法静态判定" in v for v in violations), violations

    def test_known_hosted_images_are_accepted(self, tmp_path):
        """控制组：所有已知托管镜像名都要放行，否则规则会逼人往错方向改。"""
        for image in ("ubuntu-latest", "ubuntu-24.04", "windows-2022", "macos-14"):
            yaml_text = (
                "name: CI\non:\n  pull_request: {branches: [main]}\njobs:\n"
                "  gate:\n    name: Gate\n"
                f"    runs-on: {image}\n"
                "    steps:\n      - name: Test\n        run: echo hi\n"
            )
            assert check_workflow_file(_write(tmp_path, yaml_text)) == [], image


class TestSelfHostedCleanupRule:
    """受信 self-hosted job 的清理必须密闭且受守卫。

    存在理由（实测）：旧写法 `rm -rf ./* ./.git 2>/dev/null || true` 在
    `/home/yomu/actions-runner/_work/elysia-agent/elysia-agent` 留下 `.pytest_cache`
    /`.ruff_cache`/`.env` 等点文件跨 job 残留——shell glob `*` 不匹配前导点。
    清理是**承重**的：backend job 的 `Load .env` 明确依赖「上一步已把 workspace
    清空」，漏了就能让 nightly external job 的宿主 .env 泄漏给下一个 job。
    """

    _HEAD = """\
name: CI
on:
  push: {branches: [main]}
jobs:
  backend:
    name: Backend
    if: github.event_name == 'push'
    runs-on: self-hosted
    steps:
      - name: Checkout
        shell: bash
        run: |
"""

    def test_bare_glob_cleanup_is_rejected(self, tmp_path):
        yaml_text = self._HEAD + "          rm -rf ./* ./.git 2>/dev/null || true\n"
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("裸 glob" in v for v in violations), violations

    def test_bare_glob_cleanup_also_needs_guards(self, tmp_path):
        """glob 违规与守卫缺失是两件事，各自都要报（只报一条会让人只修一半）。"""
        yaml_text = self._HEAD + "          rm -rf *\n"
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("裸 glob" in v for v in violations), violations
        assert any("GITHUB_WORKSPACE" in v for v in violations), violations

    def test_find_form_without_guards_is_rejected(self, tmp_path):
        """密闭写法本身不够——两道守卫是两条独立要求。"""
        yaml_text = self._HEAD + (
            '          find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +\n')
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("缺少守卫" in v for v in violations), violations
        assert not any("裸 glob" in v for v in violations), violations

    def test_find_form_with_both_guards_is_accepted(self, tmp_path):
        yaml_text = self._HEAD + _GUARDED_CLEANUP
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []

    def test_comment_mentioning_the_old_pattern_is_not_a_violation(self, tmp_path):
        """注释里**引用**旧写法当反面教材不该判红。

        这条是实测出来的：本仓 backend job 的注释里就写着
        `rm -rf ./* ./.git 2>/dev/null || true` 作为反面教材。若不剥整行注释，
        门禁会在「记住了教训」的地方判红——而假红会逼人删掉那段说明，正好是
        反方向。
        """
        yaml_text = self._HEAD + (
            "          # 旧写法 `rm -rf ./* ./.git 2>/dev/null || true` 有两个缺陷\n"
            + _GUARDED_CLEANUP)
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []

    def test_deleting_a_subdirectory_is_not_a_workspace_wipe(self, tmp_path):
        """`rm -rf node_modules` 的目标不是 workspace 根 → 不需要守卫（防假红）。"""
        yaml_text = self._HEAD + (
            "          rm -rf node_modules\n          npm ci\n")
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []

    def test_rule_not_applied_to_github_hosted_jobs(self, tmp_path):
        """GitHub-hosted job 是一次性机器，裸 glob 清理无害 → 不该判红。"""
        yaml_text = """\
name: CI
on:
  pull_request: {branches: [main]}
jobs:
  gate:
    name: Gate
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        run: rm -rf ./*
"""
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []


class TestCleanupFailClosedRule:
    """清理失败必须终止 job（docstring 第五条 c）。

    存在理由（本批次修的真实缺陷）：checkout 那段脚本刻意不用 `set -e`（retry loop
    里 `git remote add` 撞已有 origin、`git fetch` 网络抖动都是「允许失败」的命令），
    于是 `find … -exec rm -rf -- {} +` 失败会被**静默忽略**，脚本继续在**未清空的
    持久 workspace** 上 fetch+checkout。而 backend 的 `Load .env` 明确依赖
    「上一步已把 workspace 清空，所以这里的 .env 一定是本 job 从宿主重新取的」
    ——清理没做完，上一个 job 留下的宿主 .env 就会跨 job 泄漏。
    """

    _HEAD = TestSelfHostedCleanupRule._HEAD

    def test_unguarded_cleanup_is_rejected(self, tmp_path):
        """守卫齐全但清理没检查退出码 —— 必须红（这正是修复前的形状）。"""
        yaml_text = self._HEAD + (
            '          WS="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE 未设置——拒绝清理}"\n'
            '          if [ "$WS" = "/" ]; then exit 1; fi\n'
            '          find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +\n')
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("没有显式检查清理的退出码" in v for v in violations), violations
        # 守卫子句不该顺带报红——两条判据各自独立，报错要能定位
        assert not any("缺少守卫" in v for v in violations), violations

    def test_guard_without_exit_is_rejected(self, tmp_path):
        """`if ! find …; then echo …; fi` —— 包了反条件却没退出，等于没 fail-closed。"""
        yaml_text = self._HEAD + (
            '          WS="${GITHUB_WORKSPACE:?未设置即失败}"\n'
            '          if [ "$WS" = "/" ]; then exit 1; fi\n'
            '          if ! find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; then\n'
            '            echo "cleanup failed (只记录，不终止)"\n'
            '          fi\n')
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("没有显式检查清理的退出码" in v for v in violations), violations

    def test_exit_outside_the_guarded_block_is_rejected(self, tmp_path):
        """反条件里空操作、`exit 1` 落在 `fi` **之后** —— 不算 fail-closed。

        这条钉住判据的「同一个 if 块内」约束：否则任何后续 `exit`（例如 `/` 守卫
        的残留、或步骤末尾别的退出）都会把空壳守卫伪装成合规。
        """
        yaml_text = self._HEAD + (
            '          WS="${GITHUB_WORKSPACE:?未设置即失败}"\n'
            '          if [ "$WS" = "/" ]; then exit 1; fi\n'
            '          if ! find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; then\n'
            '            :\n'
            '          fi\n'
            '          exit 1\n')
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("没有显式检查清理的退出码" in v for v in violations), violations

    def test_set_e_alone_does_not_satisfy_the_rule(self, tmp_path):
        """`set -e` **不算**满足：它会改变 retry loop 的控制流（要求写显式检查）。"""
        yaml_text = self._HEAD + (
            '          set -e\n'
            '          WS="${GITHUB_WORKSPACE:?未设置即失败}"\n'
            '          if [ "$WS" = "/" ]; then exit 1; fi\n'
            '          find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +\n')
        violations = check_workflow_file(_write(tmp_path, yaml_text))
        assert any("没有显式检查清理的退出码" in v for v in violations), violations

    def test_canonical_fail_closed_form_is_accepted(self, tmp_path):
        assert check_workflow_file(_write(tmp_path, self._HEAD + _GUARDED_CLEANUP)) == []

    def test_rule_only_applies_to_steps_that_wipe_the_workspace(self, tmp_path):
        """不清理 workspace 的步骤不该被这条规则牵连（防假红）。"""
        yaml_text = self._HEAD + (
            '          WS="${GITHUB_WORKSPACE:?未设置即失败}"\n'
            '          rm -rf node_modules\n'
            '          npm ci\n')
        assert check_workflow_file(_write(tmp_path, yaml_text)) == []


class TestRealRepoWorkflows:
    """守卫必须装在**真实**文件上（"反证后全绿≠守卫强"）。

    Frontend push/PR 分流是 2026-09-14 迁移批次的核心结构：push 侧自建、PR 侧托管。
    这层断言把它钉住——顺手删掉 `if:`、或把 PR 侧改成 self-hosted，都会先在这里红。
    """

    _ROOT = Path(__file__).resolve().parents[2]

    def _jobs(self, name: str) -> dict:
        path = self._ROOT / ".github" / "workflows" / name
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs", {})

    def test_all_real_workflows_pass_the_gate(self):
        for path in sorted((self._ROOT / ".github" / "workflows").glob("*.yml")):
            assert check_workflow_file(path) == [], path

    def test_frontend_is_split_into_push_self_hosted_and_pr_hosted(self):
        jobs = self._jobs("ci.yml")
        push_job, pr_job = jobs["frontend"], jobs["frontend-pr"]
        assert push_job["if"] == "github.event_name == 'push'"
        assert "self-hosted" in push_job["runs-on"]
        # 平台 label 显式化：将来加 Windows / PR 隔离 / GPU runner 时不会被误接
        assert {"Linux", "X64"} <= set(push_job["runs-on"])
        assert pr_job["if"] == "github.event_name == 'pull_request'"
        assert pr_job["runs-on"] == "ubuntu-latest"

    def test_pr_frontend_keeps_the_historical_check_name(self):
        """PR 侧必须沿用历史 job 名。

        分支保护的 required status check 按 job 名匹配、且**只在 PR 上评估**；
        把名字挪到 push 侧会让已配的 required check 永远等不到上报，PR 卡死。
        """
        assert self._jobs("ci.yml")["frontend-pr"]["name"] == "Frontend (vue-tsc + build)"

    def test_frontend_jobs_keep_equivalent_build_semantics(self):
        """两侧做同一件事：`npm ci` + `npm run build`（=`vue-tsc --noEmit && vite build`）。

        迁移不允许"因为换成 self-hosted 就"改成 `npm install` / 跳过 lockfile /
        跳过 typecheck / 复用上一轮 node_modules——这条把它钉住。
        """
        jobs = self._jobs("ci.yml")
        for job_id in ("frontend", "frontend-pr"):
            joined = "\n".join(str(s.get("run", "")) for s in jobs[job_id]["steps"])
            assert "cd frontend && npm ci" in joined, job_id
            assert "cd frontend && npm run build" in joined, job_id
            assert "npm install" not in joined, job_id

    def test_self_hosted_frontend_touches_no_host_credentials(self):
        """前端构建不需要任何凭据——本 job 刻意不加载 .env（少一条外泄路径）。"""
        joined = "\n".join(
            str(s.get("run", "")) for s in self._jobs("ci.yml")["frontend"]["steps"])
        assert ".env" not in joined
        assert "cp /home/" not in joined

    def test_every_self_hosted_cleanup_site_is_fail_closed(self):
        """**全部 4 处**复用同一 cleanup 模式的地方都必须 fail-closed。

        ci.yml backend / ci.yml frontend / nightly.yml full / nightly.yml external ——
        一处漏掉就是一条跨 job 泄漏路径。条数也钉住：新增或删除清理点必须同步改这里，
        否则「守卫没装在改动上」。
        """
        sites = []
        for name in ("ci.yml", "nightly.yml"):
            for job_id, job in self._jobs(name).items():
                for step in job.get("steps", []) or []:
                    run = str(step.get("run", ""))
                    if "-mindepth 1 -maxdepth 1 -exec rm" not in run:
                        continue
                    sites.append(f"{name}:{job_id}")
                    assert 'if ! find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; then' in run, (
                        f"{name}:{job_id} 的 cleanup 没有显式检查退出码（fail-closed）")
                    assert "FATAL: workspace 清理失败" in run, f"{name}:{job_id} 失败时没报 FATAL"
                    assert "${GITHUB_WORKSPACE:?" in run, f"{name}:{job_id} 缺「未设置即失败」守卫"
                    assert '= "/"' in run, f"{name}:{job_id} 缺「拒绝 /」守卫"
        assert sorted(sites) == [
            "ci.yml:backend", "ci.yml:frontend",
            "nightly.yml:external", "nightly.yml:full",
        ], sites
