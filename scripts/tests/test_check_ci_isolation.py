"""CI 信任边界门禁测试（P0-02，marker=unit）。

验证 scripts/check_ci_isolation.py：
- 拒绝旧模式：pull_request 可达 job 用 self-hosted、且 step 引用宿主 /home/.../.env。
- 接受新模式：PR job 用 GitHub-hosted（ubuntu-latest）、无宿主路径；受信 push job
  显式 if 限定 push-only 后可 self-hosted。
- 拒绝无法静态判定的 runs-on（表达式）——拒答而非判绿。
- self-hosted job 的 workspace 清理必须密闭（禁裸 glob）且带两道守卫。
- 真实 workflow 的结构（push-only 拓扑 + 5 个清理点 + 历史 required check 名）必须被钉住。
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
# 五处真实复用点（ci backend/real-mongo/frontend、nightly full/external）都是这个形状。
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

    2026-09-16 起本仓**不开放 PR**（owner 决定，见 `docs/ci.md`）：`ci.yml` 与
    `contract-frames.yml` 只监听 push，`nightly.yml` 只有受信手工 dispatch。

    旧结构（Frontend push/PR 分流）的三条断言随之**改写**而非删除——删断言不等于
    改了契约，只等于把契约交给下一个人重新猜。这里钉的是新拓扑，外加**恢复 PR 时
    必须满足的机械约束**（名字一丢，分支保护的 required check 就永远等不到上报）。
    """

    _ROOT = Path(__file__).resolve().parents[2]
    # 分支保护的 required status check 按 **job 名**匹配、且**只在 PR 上评估**。
    # 这两个名字是历史上 PR 侧配置过的：恢复 PR 时对不上 ⇒ 每个 PR 永久 pending。
    _HISTORICAL_CHECK_NAMES = (
        "Frontend (vue-tsc + build)",
        "Real MongoDB (storage layer)",
    )
    # 每个门禁的"接线证据" = `make <目标>` 或直接调 `scripts/<脚本>.py`（两种写法都算）。
    _DECLARED_GATES = (
        "lint", "pyright", "check-types", "check-compose", "check-env-lock",
        "check-arch", "check-timeouts", "check-contract", "check-debug-auth",
        "check-secrets", "check-test-sets", "check-ci-isolation", "audit",
    )
    # 声明了但**当前没有**接进 CI 的门禁——每一项必须写明理由与解除条件。
    _GATES_NOT_IN_CI = {
        "audit": (
            "`make audit` 在自托管 env 上**当前为红**：Elysia env 的 pip 26.1.2 自身命中 "
            "PYSEC-2026-3721（修复版 26.2），而 environment.lock.yml 也 pin 着 26.1.2。"
            "接线会让 main 因与本仓提交无关的原因变红。解除条件：升级 env 的 pip + "
            "重生成 environment.lock.yml（owner 动作，会改动锁文件），之后接进 backend job "
            "并删除本条。**不要用 --ignore-vuln 让它变绿**——那是把安全门禁挖空。"
        ),
    }

    def _invoked(self, gate: str, workflows: str) -> bool:
        """门禁是否被某个 workflow 执行（`make <目标>` 或直接跑脚本）。"""
        if f"make {gate}\n" in workflows:
            return True
        script = "scripts/" + gate.replace("check-", "check_").replace("-", "_") + ".py"
        return script in workflows

    def _workflows(self) -> dict:
        """{文件名: workflow 数据}。"""
        out = {}
        for path in sorted((self._ROOT / ".github" / "workflows").glob("*.yml")):
            out[path.name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return out

    def _triggers(self, data: dict) -> dict:
        """取 `on:` 块。

        PyYAML 实现 YAML 1.1，裸 `on:` 被解析成**布尔 True**（不是字符串 "on"）——
        两种键都试，免得测试因为解析器口味差异而静默看到空的触发器集合（假绿③）。
        """
        return data.get("on") or data.get(True) or {}

    def _jobs(self, name: str) -> dict:
        path = self._ROOT / ".github" / "workflows" / name
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs", {})

    def test_all_real_workflows_pass_the_gate(self):
        for path in sorted((self._ROOT / ".github" / "workflows").glob("*.yml")):
            assert check_workflow_file(path) == [], path

    def test_push_workflows_are_exactly_the_two_that_must_run_on_main(self):
        """push 触发器集合钉死：少一个 = 一道门禁在 main 上**永不执行**。

        `nightly.yml` 刻意只有 `workflow_dispatch`（受信手工入口，不等价于 push）。
        判据若因解析不到 `on:` 而恒为空集，那条"一个 push workflow 都没有"的退化
        也会被这里拦住——空集不是通过。
        """
        push = sorted(
            name for name, data in self._workflows().items()
            if "push" in self._triggers(data)
        )
        assert push == ["ci.yml", "contract-frames.yml"], push

    def test_no_workflow_is_reachable_from_pull_requests(self):
        """本仓当前不开放 PR —— 任何 `pull_request` 触发器都是**未走恢复清单**的改动。

        红了**不要删断言**：先读 `docs/ci.md`「开放 PR 的恢复清单」（两个待恢复 job、
        PR 侧必须 GitHub-hosted 的安全理由、分支保护 required check 名、恢复后要跑的门禁）。
        """
        reachable = sorted(
            name for name, data in self._workflows().items()
            if "pull_request" in self._triggers(data)
        )
        assert reachable == [], (
            f"{reachable} 声明了 pull_request，但本仓当前不开放 PR；"
            "恢复清单见 docs/ci.md——别只把这几行加回来"
        )

    def test_historical_pr_check_names_are_accounted_for(self):
        """两个历史 required check 名必须**落了地**：要么有承载 job（已恢复 PR），
        要么留在恢复清单里（当前不开放 PR）。

        这条替代了旧的 `frontend-pr` 结构断言。名字一旦从代码与文档里同时消失，
        恢复 PR 时没人会想起来它——而 GitHub 的失败方式是"每个 PR 永远挂着"，
        不是报错。两个分支都**非空转**：任一分支的判据都真的会红。
        """
        pr_reachable = {
            job.get("name")
            for data in self._workflows().values()
            if "pull_request" in self._triggers(data)
            for job in (data.get("jobs") or {}).values()
        }
        if pr_reachable:
            missing = sorted(n for n in self._HISTORICAL_CHECK_NAMES if n not in pr_reachable)
            assert missing == [], (
                f"已声明 pull_request，却没有任何 job 名等于 {missing} —— "
                "required check 只在 PR 上评估，名字对不上 ⇒ 每个 PR 永久 pending"
            )
            return
        checklist = (self._ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
        missing = sorted(n for n in self._HISTORICAL_CHECK_NAMES if n not in checklist)
        assert missing == [], (
            f"不开放 PR 时 {missing} 必须留在 docs/ci.md 的恢复清单里——"
            "否则恢复 PR 时没人知道 required check 该叫什么名"
        )

    def test_frontend_job_keeps_build_semantics(self):
        """前端 job 仍是 `npm ci` + `npm run build`（=`vue-tsc --noEmit && vite build`）。

        迁移/重构不允许"因为换成 self-hosted 就"改成 `npm install` / 跳过 lockfile /
        跳过 typecheck / 复用上一轮 node_modules——这条把它钉住。
        """
        joined = "\n".join(
            str(s.get("run", "")) for s in self._jobs("ci.yml")["frontend"]["steps"])
        assert "cd frontend && npm ci" in joined
        assert "cd frontend && npm run build" in joined
        assert "npm install" not in joined

    def test_self_hosted_frontend_touches_no_host_credentials(self):
        """前端构建不需要任何凭据——本 job 刻意不加载 .env（少一条外泄路径）。"""
        joined = "\n".join(
            str(s.get("run", "")) for s in self._jobs("ci.yml")["frontend"]["steps"])
        assert ".env" not in joined
        assert "cp /home/" not in joined

    def test_every_declared_gate_is_wired_to_ci(self):
        """声明的门禁必须真的有执行点——「声明了但从不跑」是本项目反复出现的假绿。

        2026-09-16 实测出三处：`check-compose` **从未**接过 CI（只在本地手跑）；
        `check-env-lock` 与 `audit` 只活在已删除的 PR job 里，删 PR 时被静默带走。
        这条把它们钉住：新门禁必须接线，或进 `_GATES_NOT_IN_CI` 写明理由与解除条件；
        **登记陈旧也会红**（与 `check_contract` 的登记纪律一致）。

        刻意不列入本表的 Makefile 目标：生成器（`gen-types`/`gen-frames`，由
        `check-types` 的漂移检测覆盖）、起服务的预检（`check`/`check-full`，需要运行期
        配置）、以及 `check-secrets-history`——它按设计**当前预期为红**
        （CR-01 待清历史），是判据而非门禁，接线等于让 main 常红。
        """
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((self._ROOT / ".github" / "workflows").glob("*.yml"))
        )
        wired = {g for g in self._DECLARED_GATES if self._invoked(g, workflows)}
        unregistered = sorted(set(self._DECLARED_GATES) - wired - set(self._GATES_NOT_IN_CI))
        assert unregistered == [], (
            f"{unregistered} 在 Makefile 里是门禁，但没有任何 workflow 执行它；"
            "要么接线（`make <目标>`），要么在 _GATES_NOT_IN_CI 写明理由"
        )
        stale = sorted(set(self._GATES_NOT_IN_CI) & wired)
        assert stale == [], (
            f"{stale} 已经接进 CI，请删掉 _GATES_NOT_IN_CI 里的陈旧登记"
            "——留着会让'已豁免'的理由继续冒充事实"
        )

    def test_every_self_hosted_cleanup_site_is_fail_closed(self):
        """**全部 5 处**复用同一 cleanup 模式的地方都必须 fail-closed。

        ci.yml backend / ci.yml real-mongo / ci.yml frontend / nightly.yml full /
        nightly.yml external —— 一处漏掉就是一条跨 job 泄漏路径。条数也钉住：
        新增或删除清理点必须同步改这里，否则「守卫没装在改动上」。
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
            "ci.yml:backend", "ci.yml:frontend", "ci.yml:real-mongo",
            "nightly.yml:external", "nightly.yml:full",
        ], sites
