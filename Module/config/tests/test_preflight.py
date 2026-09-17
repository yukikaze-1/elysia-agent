"""preflight 模块单元测试。不依赖真实 API/服务。"""

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
        # 同时清掉 MINIMAX_TTS_URL：判据是"运行期能不能建出后端"，而地址（URL）与
        # 凭据（key）任一层能给值就算可用。工作区的 .env 里**确实**设了
        # MINIMAX_TTS_URL，若只删 key，这条用例就变成"断言环境变量有没有被设"，
        # 与被测逻辑无关（不 hermetically sealed）。
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_TTS_URL", raising=False)
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
        monkeypatch.delenv("SENSEVOICE_URL", raising=False)
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
        # 创建虚拟嵌入模型缓存 —— 必须是**真实 HF hub 布局**：
        # `models--Org--Name/snapshots/<revision>/config.json`。
        # 此前这里把 config.json 直接放在 snapshots/ 下（现实中不存在的布局），只因旧判据
        # 是「snapshots 目录非空」才通过——测试把这个分岔固化了（2026-09-12）。
        snapshot = (tmp_path / ".model_cache/models--BAAI--bge-large-zh-v1.5"
                    / "snapshots" / "79e7739b6ab944e86d6171e44d24c997fc1e0116")
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}")
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


# ══════════════════════════════════════════════════════════════════════
# Batch 3.5 / B3-CB-3：预检判据必须与**运行期权威**一致
#
# 缺陷背景：旧实现把"env 变量是否被设置"当成"配置是否可用"。
#   - LLM URL 只判 `os.getenv(url_key)`，而运行期回退链是
#     `explicit > env > providers.yaml::<provider>.base_url`；
#   - Mongo/Milvus 只判 env，而运行期读的是
#     `config.database.{mongodb_uri,milvus_uri}`（ConfigLoader: CLI>env>YAML>默认）。
# 后果：`app.py --check` 对一个**运行期完全可用**的配置退 1 ⇒
# `scripts/start.sh` 拒绝启动（实测：cp .env.example .env 后只缺 MONGODB_URI 这一项）。
#
# 注意：这几条用例**走真实入口 `run()`**，不是只测 helper——缺陷恰恰出在
# "每个 helper 都自洽、合起来判错"的地方。
# ══════════════════════════════════════════════════════════════════════


def _neutralize_ambient_checks(monkeypatch, mod) -> None:
    """把与本主题无关、且依赖宿主环境的检查中性化。

    目的是让断言只落在"LLM/数据库判据"上，而不是被"这台机器有没有下过嵌入模型"
    之类的环境差异影响。
    """
    monkeypatch.setattr(mod, "_check_python", lambda: mod._pass("Python ok"))
    monkeypatch.setattr(mod, "_check_dotenv", lambda: mod._pass(".env ok"))
    monkeypatch.setattr(mod, "_check_pip_deps", lambda: mod._pass("deps ok"))
    monkeypatch.setattr(mod, "_check_character_file", lambda cfg: mod._pass("char ok"))
    monkeypatch.setattr(mod, "_check_embedding_model", lambda: mod._pass("emb ok"))
    monkeypatch.setattr(mod, "_check_writable_dirs", lambda: mod._pass("dirs ok"))


def _clear_provider_env(monkeypatch) -> None:
    """清掉所有 provider/DB 相关 env，模拟"只按 config.yaml + providers.yaml 跑"。"""
    for key in ("DEEPSEEK_URL", "DEEPSEEK_BETA_URL", "MONGODB_URI", "MILVUS_URI",
                "MINIMAX_TTS_URL", "SENSEVOICE_URL", "OPENAI_URL", "MIMO_URL"):
        monkeypatch.delenv(key, raising=False)


class TestPreflightMatchesRuntimeAuthority:
    """C1–C4：合法配置必须通过、真非法必须继续失败、优先级按运行期权威。"""

    def _cfg(self, mod, model: str = "deepseek/deepseek-chat"):
        cfg = MagicMock()
        cfg.llm.model = model
        cfg.tts.enabled = False
        cfg.stt.enabled = False
        cfg.agent.character = "character/elysia/config.yaml"
        # 真实运行期权威：仓库 config.yaml 自带的默认值（AppConfig 默认同值）
        cfg.database.mongodb_uri = "mongodb://localhost:27017"
        cfg.database.milvus_uri = "http://localhost:19530"
        return cfg

    def test_c1_legal_config_passes_without_url_and_db_env(self, monkeypatch, capsys):
        """C1：providers.yaml 已给 LLM 默认地址、config.yaml 已给数据库地址 ⇒ 必须通过。

        这正是被误拒的那种配置：只设 API key，不设 `DEEPSEEK_URL` / `MONGODB_URI` /
        `MILVUS_URI`。运行期完全可用（`LLMFactory.create` 走 providers.yaml 默认值，
        仓储走 `config.database.*`）。
        """
        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        ok = m.run(self._cfg(m), full=False)

        out = capsys.readouterr().out
        assert ok is True, f"合法配置被拒绝 —— 输出：\n{out}"
        assert m._COUNTS.failed == 0, out
        assert "DEEPSEEK_URL" not in out or "providers.yaml 默认值" in out, (
            "不应再要求 DEEPSEEK_URL 环境变量（运行期有 yaml 默认值）")

    def test_c1_llm_url_source_is_the_runtime_default(self, monkeypatch):
        """C1 补强：预检解析出的 LLM 地址必须**等于运行期真正会用的那个**。

        不是"预检自己算了个值"，而是与 `resolve_base_url`（运行期同一函数）交叉核对。
        """
        import os as _os

        from Module._common.registry_helpers import resolve_base_url
        from Module.config.providers import load_provider_defaults

        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)

        resolved, _key = m._resolved_llm_url("deepseek")
        defaults = load_provider_defaults("llm")["deepseek"]
        assert _os.getenv("DEEPSEEK_URL") is None
        assert resolved == resolve_base_url(defaults["url_key"], defaults)
        assert resolved == defaults["base_url"] == "https://api.deepseek.com"

    def test_c2_truly_unresolvable_url_still_fails(self, monkeypatch, capsys):
        """C2：任何一层都没有地址的 provider 必须**继续** FAIL（防"改成永远通过"）。

        `mimo` 在 providers.yaml 里的 `base_url` 就是空串（云端端点须由部署方提供），
        运行期 `LLMFactory.create` 同样会拒绝建连 —— 预检必须与之一致。
        """
        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("MIMO_API_KEY", "sk-test")   # key 齐全，唯独没有地址

        cfg = self._cfg(m, model="mimo/mimo-v2.5")
        ok = m.run(cfg, full=False)

        out = capsys.readouterr().out
        assert ok is False, f"无地址的 provider 竟然通过了 —— 输出：\n{out}"
        assert "MIMO_URL" in out and "base URL" in out

    def test_c2_unresolvable_url_fails_even_for_builtin_provider(self, monkeypatch, capsys):
        """C2 变体：连内置 provider 也一样——判据是"有没有地址"，不是"是不是内置"。

        把 providers.yaml 的 deepseek 默认地址清空（模拟运维副本里写漏），
        此时运行期建不出后端 ⇒ 预检必须 FAIL。
        """
        from Module.config import providers as providers_mod

        real = providers_mod.load_provider_defaults
        stripped = {
            "deepseek": {**real("llm")["deepseek"], "base_url": "", "url_key": "DEEPSEEK_URL"},
        }
        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setattr(providers_mod, "load_provider_defaults",
                            lambda name: stripped if name == "llm" else real(name))

        ok = m.run(self._cfg(m), full=False)

        out = capsys.readouterr().out
        assert ok is False, f"无默认地址竟然通过了 —— 输出：\n{out}"
        assert "DEEPSEEK_URL" in out

    def test_c3_env_still_wins_over_yaml_default(self, monkeypatch, capsys):
        """C3：env 必须仍然赢 providers.yaml 默认值（优先级按运行期权威）。"""
        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_URL", "https://my-gateway.example.com/v1")

        ok = m.run(self._cfg(m), full=False)
        out = capsys.readouterr().out
        assert ok is True
        assert "https://my-gateway.example.com/v1" in out, out

        resolved, _ = m._resolved_llm_url("deepseek")
        assert resolved == "https://my-gateway.example.com/v1"

    def test_c3_database_uri_follows_config_not_env(self, monkeypatch, capsys):
        """C3（数据库侧）：判据是 `config.database.*`，env 只经由 ConfigLoader 参与。

        这里直接改 config 对象（等价于 config.yaml 里写了别的地址），预检必须报出
        **那个**地址，而不是 env 里的值。
        """
        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("MONGODB_URI", "mongodb://from-env:27017")

        cfg = self._cfg(m)
        cfg.database.mongodb_uri = "mongodb://from-yaml:27017"
        ok = m.run(cfg, full=False)

        out = capsys.readouterr().out
        assert ok is True
        assert "mongodb://from-yaml:27017" in out, out
        assert "from-env" not in out, "预检不该改读 env（env 已被 ConfigLoader 折算进 config）"

    def test_c4_optional_subsystem_absence_does_not_fail_startup(self, monkeypatch, capsys):
        """C4：可选子系统（TTS/STT）不可用**不得**让主进程预检失败。

        与 Batch 1 已建立的不变量一致：可选子系统失败不阻断 Agent 启动。
        这里 TTS=minimax（无 key 无地址）与 STT=sensevoice（无地址）都必须只 WARN。
        """
        m = _fresh_module()
        _neutralize_ambient_checks(monkeypatch, m)
        _clear_provider_env(monkeypatch)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        cfg = self._cfg(m)
        cfg.tts.enabled = True
        cfg.tts.model = "minimax/speech-2.8-hd"
        cfg.stt.enabled = True
        cfg.stt.model = "sensevoice/sense-voice"

        ok = m.run(cfg, full=False)

        out = capsys.readouterr().out
        assert ok is True, f"可选子系统缺失竟然阻断了启动 —— 输出：\n{out}"
        assert m._COUNTS.failed == 0, out
        assert m._COUNTS.warned >= 2, f"应各报一条 WARN：\n{out}"
