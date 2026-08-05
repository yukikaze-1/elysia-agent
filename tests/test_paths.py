"""测试 paths.py — 项目路径统一管理。"""

import os
import pytest
from pathlib import Path

# 注入项目根目录到 sys.path（测试文件可能被 pytest 从不同目录运行）
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import ProjectPath, get_project_root, _detect_project_root


class TestDetectProjectRoot:
    """项目根目录检测。"""

    def test_env_var_takes_priority(self, monkeypatch):
        """ELYSIA_ROOT 环境变量优先。"""
        monkeypatch.setenv("ELYSIA_ROOT", "/custom/project")
        assert _detect_project_root() == Path("/custom/project").resolve()

    def test_fallback_to_paths_py_location(self, monkeypatch):
        """无环境变量时 fallback 到 paths.py 所在目录。"""
        monkeypatch.delenv("ELYSIA_ROOT", raising=False)
        root = _detect_project_root()
        assert (root / "paths.py").exists()
        assert (root / "config.yaml").exists()


class TestProjectPathResolve:
    """resolve() 方法。"""

    def test_resolve_directory(self):
        """基础目录解析。"""
        result = ProjectPath.LOG.resolve()
        assert isinstance(result, Path)
        assert result.is_absolute()
        assert result.name == "log"

    def test_resolve_file(self):
        """基础文件解析。"""
        result = ProjectPath.CONFIG_YAML.resolve()
        assert result.name == "config.yaml"

    def test_resolve_with_placeholder(self):
        """占位符填充。"""
        result = ProjectPath.SESSION_PENDING.resolve(session_id="test123")
        assert result.name == "session_test123.json"
        assert str(result).endswith("data/session_test123.json")

    def test_resolve_placeholder_in_middle(self):
        """占位符在路径中间。"""
        result = ProjectPath.DATA_SANDBOX.resolve(session_id="abc")
        assert str(result).endswith("data/sandbox/abc")

    def test_resolve_without_placeholder(self):
        """无占位符时不需要 kwargs。"""
        result = ProjectPath.TOKEN_PENDING.resolve()
        assert result.name == "token_usage_pending.jsonl"

    def test_resolve_returns_path(self):
        """返回值是 pathlib.Path。"""
        result = ProjectPath.STATIC.resolve()
        assert isinstance(result, Path)

    def test_env_override(self, monkeypatch):
        """ELYSIA_PATH_{NAME} 环境变量覆盖。"""
        monkeypatch.setenv("ELYSIA_PATH_LOG", "/var/log/elysia")
        result = ProjectPath.LOG.resolve()
        assert result == Path("/var/log/elysia")

    def test_env_override_not_set_no_effect(self, monkeypatch):
        """未设环境变量时行为不变。"""
        monkeypatch.delenv("ELYSIA_PATH_LOG", raising=False)
        result = ProjectPath.LOG.resolve()
        root = get_project_root()
        assert result == root / "log"


class TestGetProjectRoot:
    """get_project_root() 函数。"""

    def test_returns_path(self):
        """返回 pathlib.Path。"""
        root = get_project_root()
        assert isinstance(root, Path)
        assert root.is_absolute()
        assert (root / "config.yaml").exists()


class TestEnumMetadata:
    """枚举元数据。"""

    def test_is_string_enum(self):
        """ProjectPath 是 str 子类，可直接作为字符串使用。"""
        assert isinstance(ProjectPath.LOG, str)
        assert ProjectPath.LOG == "log"

    def test_count(self):
        """成员数量正确 — 14 目录 + 8 文件 + 1（批次 4 增 DATA_UPLOADS_DESKTOP、批次 5 增 DATA_SECRETS）= 23。"""
        members = list(ProjectPath)
        assert len(members) == 23

    def test_all_members_have_values(self):
        """所有成员都有非空值。"""
        for member in ProjectPath:
            assert member.value, f"{member.name} 的 value 为空"
