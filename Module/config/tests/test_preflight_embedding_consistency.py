"""preflight 嵌入检查 与 加载器路径查找 的一致性守卫（2026-09-12）。

背景：两者**各自维护一份「模型缓存在哪」的实现**，判据还不同——

- `preflight._check_embedding_model`：候选目录 `exists() and any(iterdir())`
- `huggingface._find_local_model`：快照目录里必须有 `config.json`

于是实测出现「预检 13/13 全绿说已缓存，加载器报本地缓存缺失、拒绝加载」，
agent 全程跑在**无长期记忆**的降级态，`bash ./start.sh` 屏幕上却一切正常。

修法是让 preflight 直接调用 `find_local_model`（单源）。本文件守这个单源：
只要有人把候选表重新抄回 preflight，或两边判据再次分岔，这里就红。
"""

import pytest

from Module.config import preflight
from Module.embedding.huggingface import find_local_model
from Module.embedding.model_manifest import DEFAULT_MODEL_ID

pytestmark = pytest.mark.unit

_HUB = "models--BAAI--bge-large-zh-v1.5"
_REV = "79e7739b6ab944e86d6171e44d24c997fc1e0116"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    project = tmp_path / "project_cache"
    hf_home = tmp_path / "hf_home"
    project.mkdir()
    hf_home.mkdir()
    monkeypatch.setenv("MODEL_CACHE_DIR", str(project))
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.setattr(preflight._COUNTS, "passed", 0)
    monkeypatch.setattr(preflight._COUNTS, "failed", 0)
    monkeypatch.setattr(preflight._COUNTS, "warned", 0)
    return project, hf_home


def _make_hub(root):
    snap = root / _HUB / "snapshots" / _REV
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    return snap


def _make_husk(root):
    refs = root / _HUB / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text(_REV, encoding="utf-8")


class TestPreflightAgreesWithLoader:
    def test_both_agree_on_present(self, isolated):
        project, _hf = isolated
        _make_hub(project)
        preflight._check_embedding_model()
        assert preflight._COUNTS.passed == 1
        assert preflight._COUNTS.failed == 0

    def test_both_agree_on_absent(self, isolated, capsys):
        preflight._check_embedding_model()
        capsys.readouterr()
        assert preflight._COUNTS.failed == 1
        assert preflight._COUNTS.passed == 0

    def test_preflight_fails_on_husk_only(self, isolated, capsys):
        """只有空壳时必须报「未缓存」——旧实现见到非空 snapshots 就 PASS。"""
        _project, hf_home = isolated
        _make_husk(hf_home / "hub")
        preflight._check_embedding_model()
        capsys.readouterr()
        assert preflight._COUNTS.failed == 1, \
            "空壳被当成已缓存——preflight 与加载器判据再次分岔"
        assert preflight._COUNTS.passed == 0

    def test_preflight_fails_on_empty_snapshots_dir(self, isolated, capsys):
        """`snapshots/` 存在但为空，旧判据 `any(iterdir())` 会误判为通过。"""
        project, _hf = isolated
        (project / _HUB / "snapshots").mkdir(parents=True)
        preflight._check_embedding_model()
        capsys.readouterr()
        assert preflight._COUNTS.failed == 1

    def test_husk_does_not_block_full_copy(self, isolated):
        """空壳在家目录、完整副本在项目缓存 → 两边都必须说「有」。"""
        project, hf_home = isolated
        _make_husk(hf_home / "hub")
        snap = _make_hub(project)
        assert find_local_model(DEFAULT_MODEL_ID) == str(snap)
        preflight._check_embedding_model()
        assert preflight._COUNTS.passed == 1

    def test_pass_message_names_the_resolved_path(self, isolated, capsys):
        """通过时必须给出实际解析到的路径——否则「已缓存」永远无法自查。"""
        project, _hf = isolated
        snap = _make_hub(project)
        preflight._check_embedding_model()
        out = capsys.readouterr().out
        assert str(snap) in out, "预检通过但没说是哪个路径，等于又一句无法验证的断言"
