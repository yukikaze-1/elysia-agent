"""测试 paths.py — 项目路径统一管理。"""

import os
import pytest
from pathlib import Path

# 注入项目根目录到 sys.path（测试文件可能被 pytest 从不同目录运行）
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import ProjectPath, get_project_root, _detect_project_root

# 纯路径解析函数测试——无 mock、无外部服务；monkeypatch 仅改环境变量。
pytestmark = pytest.mark.unit


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

    def test_resolve_with_placeholder(self, monkeypatch):
        """占位符填充。"""
        # 默认路径断言：清掉防线的会话隔离覆盖（见根 conftest 的
        # _isolate_session_pending_jsonl），否则本测试在测试环境下得不到默认值。
        monkeypatch.delenv("ELYSIA_PATH_SESSION_PENDING", raising=False)
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
        """成员集合显式列出——新增/删除 ProjectPath 成员必须同步此处。

        用成员名集合替代总数魔法数：此前硬编码 23 已漂移两次（批次 7 增
        MEMORY_PENDING_DELETES 后未更新；2026-08-10 增 MEMORY_PENDING_SYNCS /
        SESSION_PENDING_CLEANUP 后 CI test-normal 显性失败）。集合精确匹配——
        增/删路径都会强制在此登记，防止静默漂移。
        """
        expected = {
            # 目录（15）
            "DATA", "DATA_UPLOADS", "DATA_UPLOADS_IMAGES", "DATA_UPLOADS_DESKTOP",
            "DATA_SANDBOX", "DATA_CACHE_VISION", "DATA_REF", "DATA_SECRETS",
            "LOG", "STATIC", "LIVE2D_ASSETS", "PROMPTS", "SYSTEM_PROMPT_TEMPLATES",
            "CHARACTERS", "MODEL_CACHE",
            # 文件（13）
            "CONFIG_YAML", "DOTENV", "CUSTOM_PROVIDERS_YAML", "STT_SAMPLE_WAV",
            "TOKEN_PENDING", "SESSION_PENDING", "SESSION_PENDING_CHECK",
            "SCHEDULER_CHECKPOINT", "MEMORY_PENDING_DELETES",
            "MEMORY_PENDING_SYNCS", "SESSION_PENDING_CLEANUP",
            # #41 / #10：提交后清理 journal 与未整合缓冲 journal
            "PENDING_ATTACHMENT_RELEASE", "CONSOLIDATOR_PENDING",
            # #6 硬杀窗口：后台任务终态本地 WAL（同步追加的 JSONL）
            "TASK_RESULT_WAL",
        }
        assert {m.name for m in ProjectPath} == expected

    def test_all_members_have_values(self):
        """所有成员都有非空值。"""
        for member in ProjectPath:
            assert member.value, f"{member.name} 的 value 为空"
