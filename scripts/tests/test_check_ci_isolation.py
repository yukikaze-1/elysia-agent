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
