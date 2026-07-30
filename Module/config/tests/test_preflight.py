"""preflight 模块单元测试。不依赖真实 API/服务。"""

import os
import sys
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture
def mock_config():
    """构造一个最小 AppConfig。"""
    cfg = MagicMock()
    cfg.llm.model = "deepseek/deepseek-chat"
    cfg.tts.enabled = False
    cfg.stt.enabled = False
    cfg.agent.character = "character/elysia/config.yaml"
    return cfg


def _fresh_module():
    """在干净状态下重新导入 preflight。"""
    import importlib
    import Module.config.preflight
    importlib.reload(Module.config.preflight)
    return Module.config.preflight


def _reset_counts(mod):
    mod._COUNTS.__init__()


class TestCheckPython:
    def test_py311_or_later_passes(self, capsys):
        if sys.version_info < (3, 11):
            pytest.skip("test requires Python >= 3.11")
        m = _fresh_module()
        _reset_counts(m)
        m._check_python()
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert m._COUNTS.failed == 0

    def test_py310_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "version_info", (3, 10, 0, "final", 0))
        # sys.version_info 在 Python 3.13 是 named tuple，monkeypatch 后仍是普通 tuple。
        # _check_python 用 .major/.minor/.micro，普通 tuple 也支持（named tuple 行为兼容）。
        # 但 3.10 < 3.11，应 FAIL。
        m = _fresh_module()
        _reset_counts(m)
        m._check_python()
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "3.11" in captured.out
        assert m._COUNTS.failed == 1


class TestCheckDotenv:
    def test_dotenv_exists_passes(self, tmp_path, monkeypatch, capsys):
        dotenv = tmp_path / ".env"
        dotenv.write_text("DEEPSEEK_API_KEY=test")
        monkeypatch.chdir(tmp_path)
        m = _fresh_module()
        _reset_counts(m)
        m._check_dotenv()
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_dotenv_missing_fails(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        m = _fresh_module()
        _reset_counts(m)
        m._check_dotenv()
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "cp .env.example .env" in captured.out
        assert m._COUNTS.failed == 1


class TestCheckLLMEnv:
    def test_deepseek_key_missing_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        m._check_llm_env(cfg)
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "DEEPSEEK_API_KEY" in captured.out

    def test_deepseek_key_present_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_URL", "https://api.deepseek.com")
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("MILVUS_URI", "http://localhost:19530")
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        m._check_llm_env(cfg)
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_ollama_skips(self, capsys):
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.llm.model = "ollama/llama3"
        m._check_llm_env(cfg)
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "ollama" in captured.out


class TestCheckPipDeps:
    def test_all_present_passes(self, capsys):
        m = _fresh_module()
        _reset_counts(m)
        m._check_pip_deps()
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_missing_reported(self, monkeypatch, capsys):
        import importlib
        orig = importlib.import_module
        def fake(name):
            if name == "aiohttp":
                raise ImportError("no aiohttp")
            return orig(name)
        monkeypatch.setattr(importlib, "import_module", fake)
        m = _fresh_module()
        _reset_counts(m)
        m._check_pip_deps()
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "aiohttp" in captured.out
        assert m._COUNTS.failed == 1


class TestCheckCharacterFile:
    def test_exists_passes(self, capsys):
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.agent.character = "character/elysia/config.yaml"
        m._check_character_file(cfg)
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_missing_fails(self, capsys):
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.agent.character = "character/nonexistent.yaml"
        m._check_character_file(cfg)
        captured = capsys.readouterr()
        assert "FAIL" in captured.out


class TestCheckTTSEnv:
    def test_tts_disabled_skips(self, capsys):
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.tts.enabled = False
        m._check_tts_env(cfg)
        captured = capsys.readouterr()
        assert "跳过" in captured.out

    def test_minimax_key_missing_warns(self, monkeypatch, capsys):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.tts.enabled = True
        cfg.tts.model = "minimax/speech-2.8-hd"
        m._check_tts_env(cfg)
        captured = capsys.readouterr()
        assert "WARN" in captured.out
        assert m._COUNTS.warned == 1

    def test_minimax_key_present_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.tts.enabled = True
        cfg.tts.model = "minimax/speech-2.8-hd"
        m._check_tts_env(cfg)
        captured = capsys.readouterr()
        assert "PASS" in captured.out


class TestCheckSTTEnv:
    def test_stt_disabled_skips(self, capsys):
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.stt.enabled = False
        m._check_stt_env(cfg)
        captured = capsys.readouterr()
        assert "跳过" in captured.out

    def test_sensevoice_key_missing_warns(self, monkeypatch, capsys):
        monkeypatch.delenv("STT_API_URL", raising=False)
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.stt.enabled = True
        cfg.stt.model = "sensevoice/sense-voice"
        m._check_stt_env(cfg)
        captured = capsys.readouterr()
        assert "WARN" in captured.out
        assert m._COUNTS.warned == 1

    def test_openai_stt_stub_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        m = _fresh_module()
        _reset_counts(m)
        cfg = MagicMock()
        cfg.stt.enabled = True
        cfg.stt.model = "openai/whisper-1"
        m._check_stt_env(cfg)
        captured = capsys.readouterr()
        assert "WARN" in captured.out
        assert "暂未实现" in captured.out


class TestRun:
    def test_run_returns_false_on_failure(self, tmp_path, monkeypatch):
        """无 .env 时 run() 应返回 False。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_URL", "https://api.deepseek.com")
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("MILVUS_URI", "http://localhost:19530")
        m = _fresh_module()
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        cfg.tts.enabled = False
        cfg.stt.enabled = False
        cfg.agent.character = "character/elysia/config.yaml"
        ok = m.run(cfg, full=False)
        assert ok is False

    def test_run_returns_true_when_all_ok(self, tmp_path, monkeypatch):
        """配置齐全时 run() 应返回 True。"""
        dotenv = tmp_path / ".env"
        dotenv.write_text("DEEPSEEK_API_KEY=sk-test")
        # 在 tmp_path 中创建虚拟 character 文件
        char_dir = tmp_path / "character" / "elysia"
        char_dir.mkdir(parents=True)
        (char_dir / "config.yaml").write_text("name: Elysia")
        # 创建虚拟嵌入模型缓存
        model_cache = tmp_path / ".model_cache/models--BAAI--bge-large-zh-v1.5/snapshots"
        model_cache.mkdir(parents=True)
        (model_cache / "config.json").write_text("{}")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_URL", "https://api.deepseek.com")
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("MILVUS_URI", "http://localhost:19530")
        monkeypatch.setenv("ELYSIA_PATH_MODEL_CACHE", str(tmp_path / ".model_cache"))
        monkeypatch.setenv("ELYSIA_PATH_LOG", str(tmp_path / "log"))
        monkeypatch.setenv("ELYSIA_PATH_DATA", str(tmp_path / "data"))
        m = _fresh_module()
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        cfg.tts.enabled = False
        cfg.stt.enabled = False
        cfg.agent.character = "character/elysia/config.yaml"
        ok = m.run(cfg, full=False)
        assert ok is True
