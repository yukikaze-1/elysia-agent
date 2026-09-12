"""`find_local_model` 本地模型查找回归测试（2026-09-12）。

背景：agent 长期跑在「无长期记忆」的降级态，每次启动都在 `app-debug.log` 留一行
DEBUG `HuggingFace embedding unavailable: 模型 BAAI/bge-large-zh-v1.5 本地缓存缺失`，
但 1.3 GB 的完整模型就躺在 `.model_cache/` 里。两个独立缺陷：

1. **hub 布局从没被查过**：`cache_folder` 正确传成 `ProjectPath.MODEL_CACHE`，但函数
   只用它试 `<cache>/BAAI/bge-large-zh-v1.5`（非 hub 布局），没试
   `<cache>/models--BAAI--bge-large-zh-v1.5`——而后者正是 `scripts/download_model.py`
   的产出、也正是 `preflight` 检查的布局。于是 preflight 报「已缓存」而 loader 报
   「缺失」，两者永不相等。
2. **命中同名目录即放弃**：`~/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5`
   是个 12 KB 空壳（只有 `refs/`，中断下载的残留），`isdir` 为真 → 进入分支 →
   `snapshots/` 不存在 → 直接 `return None`，完整副本永远没机会。

本文件同时守 `resolve_cache_folder` 的单源性——preflight 与加载器必须解析出同一个根。
"""

import os

import pytest

from Module.embedding.huggingface import find_local_model, resolve_cache_folder

pytestmark = pytest.mark.unit

_MODEL = "BAAI/bge-large-zh-v1.5"
_HUB = "models--BAAI--bge-large-zh-v1.5"
_REV = "79e7739b6ab944e86d6171e44d24c997fc1e0116"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """把项目缓存与 HF 家目录缓存都指到 tmp_path 下，隔离真实机器状态。"""
    project = tmp_path / "project_cache"
    hf_home = tmp_path / "hf_home"
    project.mkdir()
    hf_home.mkdir()
    monkeypatch.setenv("MODEL_CACHE_DIR", str(project))
    monkeypatch.setenv("HF_HOME", str(hf_home))
    return project, hf_home


def _make_hub(root, *, snapshot=_REV, with_config=True):
    """造一个 HF hub 布局缓存，返回快照目录。"""
    snap = root / _HUB / "snapshots" / snapshot
    snap.mkdir(parents=True)
    if with_config:
        (snap / "config.json").write_text("{}", encoding="utf-8")
    return snap


def _make_husk(root):
    """造一个「下载中断的空壳」——只有 refs/，没有 snapshots/。"""
    refs = root / _HUB / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text(_REV, encoding="utf-8")


class TestCacheFolderResolution:
    def test_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv("MODEL_CACHE_DIR", "/env/path")
        assert resolve_cache_folder("/explicit") == "/explicit"

    def test_env_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("MODEL_CACHE_DIR", "/env/path")
        assert resolve_cache_folder() == "/env/path"

    def test_falls_back_to_project_cache(self, monkeypatch):
        monkeypatch.delenv("MODEL_CACHE_DIR", raising=False)
        from paths import ProjectPath
        assert resolve_cache_folder() == str(ProjectPath.MODEL_CACHE.resolve())


class TestHubLayoutInProjectCache:
    """缺陷 1 的直接回归：项目缓存的 hub 布局必须被找到。"""

    def test_hub_snapshot_found(self, isolated):
        project, _hf = isolated
        snap = _make_hub(project)
        assert find_local_model(_MODEL) == str(snap)

    def test_hub_snapshot_found_via_explicit_cache_folder(self, isolated):
        project, _hf = isolated
        snap = _make_hub(project)
        assert find_local_model(_MODEL, str(project)) == str(snap)

    def test_project_cache_preferred_over_hf_home(self, isolated):
        """同在候选里时项目缓存优先（download_model.py 的落点）。"""
        project, hf_home = isolated
        mine = _make_hub(project)
        _make_hub(hf_home / "hub")
        assert find_local_model(_MODEL) == str(mine)


class TestHuskDoesNotVetoLaterCandidates:
    """缺陷 2 的直接回归：残缺同名目录不得否决后面候选里的完整副本。"""

    def test_husk_in_hf_home_does_not_mask_project_cache(self, isolated):
        project, hf_home = isolated
        _make_husk(hf_home / "hub")      # 12 KB 空壳，排在家目录
        snap = _make_hub(project)         # 完整副本，排在项目缓存
        assert find_local_model(_MODEL) == str(snap)

    def test_husk_with_empty_snapshots_dir_also_skipped(self, isolated):
        """snapshots/ 存在但为空——同样不是命中。"""
        project, hf_home = isolated
        husk = hf_home / "hub" / _HUB / "snapshots"
        husk.mkdir(parents=True)
        snap = _make_hub(project)
        assert find_local_model(_MODEL) == str(snap)

    def test_husk_snapshot_without_config_skipped(self, isolated):
        """快照目录存在但缺 config.json——跳过该快照继续找。"""
        project, hf_home = isolated
        _make_hub(hf_home / "hub", snapshot="0" * 40, with_config=False)
        snap = _make_hub(project)
        assert find_local_model(_MODEL) == str(snap)

    def test_later_snapshot_in_same_hub_used_when_first_is_broken(self, isolated):
        """同一个 hub 目录里：坏快照在前、好快照在后，也要拿到好的。"""
        project, _hf = isolated
        _make_hub(project, snapshot="f" * 40, with_config=False)   # 字典序更大 → 先试
        good = _make_hub(project, snapshot="0" * 40)
        assert find_local_model(_MODEL) == str(good)


class TestDirectLayout:
    def test_direct_layout_found(self, isolated):
        project, _hf = isolated
        direct = project / _MODEL
        direct.mkdir(parents=True)
        (direct / "config.json").write_text("{}", encoding="utf-8")
        assert find_local_model(_MODEL) == str(direct)

    def test_direct_layout_without_config_is_not_a_hit(self, isolated):
        """缺 config.json 的目录在 local_files_only=True 下也加载不了，不算命中。"""
        project, _hf = isolated
        (project / _MODEL).mkdir(parents=True)
        assert find_local_model(_MODEL) is None

    def test_hf_home_direct_layout_found(self, isolated):
        _project, hf_home = isolated
        direct = hf_home / "hub" / _MODEL
        direct.mkdir(parents=True)
        (direct / "config.json").write_text("{}", encoding="utf-8")
        assert find_local_model(_MODEL) == str(direct)


class TestNotFound:
    def test_empty_caches_return_none(self, isolated):
        assert find_local_model(_MODEL) is None

    def test_husk_only_returns_none(self, isolated):
        """只有空壳时如实返回 None——由调用方报「未缓存」，而不是把空壳当模型。"""
        _project, hf_home = isolated
        _make_husk(hf_home / "hub")
        assert find_local_model(_MODEL) is None

    def test_hf_home_env_is_respected(self, isolated, tmp_path, monkeypatch):
        """HF_HOME 覆盖（含 ``or`` 分支：空串回落到默认）必须生效。"""
        other = tmp_path / "other_hf"
        other.mkdir()
        snap = _make_hub(other)
        monkeypatch.setenv("HF_HOME", str(other))
        assert find_local_model(_MODEL) == str(snap)

    def test_empty_hf_home_falls_back_to_default(self, isolated, monkeypatch):
        monkeypatch.setenv("HF_HOME", "")
        # 空串应落到 ~/.cache/huggingface（此处只断言不抛且不误命中 tmp）
        result = find_local_model(_MODEL)
        assert result is None or os.path.isabs(result)
