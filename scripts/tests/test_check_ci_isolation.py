"""CI 信任边界门禁测试（P0-02，marker=unit）。

验证 scripts/check_ci_isolation.py：
- 拒绝旧模式：pull_request 可达 job 用 self-hosted、且 step 引用宿主 /home/.../.env。
- 接受新模式：PR job 用 GitHub-hosted（ubuntu-latest）、无宿主路径；受信 push job
  显式 if 限定 push-only 后可 self-hosted。
"""

from __future__ import annotations

import pytest

from scripts.check_ci_isolation import check_workflow_file

pytestmark = pytest.mark.unit


def _write(tmp_path, workflow_yaml: str):
    """写一个临时 workflow 文件并返回其路径。"""
    p = tmp_path / "ci.yml"
    p.write_text(workflow_yaml, encoding="utf-8")
    return str(p)


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
