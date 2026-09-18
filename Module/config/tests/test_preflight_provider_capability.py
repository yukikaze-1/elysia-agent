"""N-10：预检声明的 provider 能力必须与运行期真正能建出来的后端一致。

原始 finding（Batch 3.5 §12 的 N-9 → canonical **N-10**）：

    `gemini/<model>` 预检全绿（`passed=2 failed=0`），
    而运行期 `LLMFactory.create` **无条件** `NotImplementedError`。

根因不是"预检少判了一个 env"，而是**两处各自维护一份「哪些 provider 本仓实现了」**：

* `Module/llm/registry.py::create` —— `if provider == "gemini": raise`（权威，决定行为）；
* `Module/config/preflight.py` —— LLM 侧**根本没有**"未实现"概念（STT 侧有
  `_STT_STUB_PROVIDERS`，是同一个事实的第二份副本，与 STT registry 的内联元组并存）。

本文件钉住三件事（对应 Batch 5 §14 / §34 / §56）：

1. **N10-1** 未实现的 provider 不得在静态预检里报 usable；
2. **N10-2** 事实锚点：该 provider 在运行期**确实**建不出来（不是"看代码像是会抛"）；
3. **N10-3** 已实现的 provider 不得被误拒（含无凭据的本地 provider）。

判据一律问 **registry**（运行期权威），不问预检自己的表——这样"预检绿"与
"运行期建得出来"不可能各说各话。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _fresh_preflight():
    """在干净状态下重新导入 preflight（模块级 `_COUNTS` 需复位）。"""
    import importlib

    import Module.config.preflight

    mod = importlib.reload(Module.config.preflight)
    mod._COUNTS.__init__()
    return mod


def _cfg(service: str, model_id: str) -> MagicMock:
    """构造最小 config：只填被测子系统，其余关掉以免噪声。"""
    cfg = MagicMock()
    cfg.llm.model = model_id
    cfg.tts.enabled = False
    cfg.stt.enabled = False
    if service == "stt":
        cfg.stt.enabled = True
        cfg.stt.model = model_id
    elif service == "tts":
        cfg.tts.enabled = True
        cfg.tts.model = model_id
    return cfg


def _out(capsys) -> str:
    return capsys.readouterr().out


# ---- N10-1：未实现 provider 不得在静态预检报 usable ----------------------------


class TestUnimplementedProviderIsNotReportedUsable:
    """N10-1（PRE_FIX_RED）：gemini 配了 key + URL 时，预检**不得**全绿。"""

    def test_gemini_key_and_url_present_still_not_usable(self, monkeypatch, capsys):
        """Batch 3.5 的实测形态：`GEMINI_API_KEY` + `GEMINI_URL` 齐备 ⇒ 旧判据 13/13 PASS。"""
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        monkeypatch.setenv("GEMINI_URL", "https://generativelanguage.googleapis.com")

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "gemini/gemini-1.5-pro"))
        out = _out(capsys)

        assert m._COUNTS.failed >= 1, (
            "未实现的 LLM provider 在静态预检里被报成可用——运行期 LLMFactory.create "
            f"必然 NotImplementedError。实际输出：\n{out}"
        )
        assert "[FAIL]" in out

    def test_failure_reason_names_unimplemented_not_credential(self, monkeypatch, capsys):
        """失败必须归因到 capability 层，而不是凭据层（凭据其实是齐的，§79）。"""
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        monkeypatch.setenv("GEMINI_URL", "https://generativelanguage.googleapis.com")

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "gemini/gemini-1.5-pro"))
        out = _out(capsys)

        fail_lines = [ln for ln in out.splitlines() if "[FAIL]" in ln]
        assert fail_lines, f"预期至少一条 FAIL，实际：\n{out}"
        assert any(("未实现" in ln) or ("NotImplementedError" in ln) for ln in fail_lines), (
            f"FAIL 未说明是 capability 未实现：{fail_lines}"
        )


# ---- N10-2：事实锚点（运行期确实建不出来） -----------------------------------


class TestRuntimeFactAnchor:
    """N10-2：预检的判据必须锚在**运行期真实行为**上，不能只是"看代码会抛"。"""

    def test_gemini_create_raises_not_implemented(self):
        from Module.llm import LLMFactory

        with pytest.raises(NotImplementedError):
            LLMFactory.create("gemini/gemini-1.5-pro")

    def test_registry_query_agrees_with_factory_behaviour(self):
        """registry 的 capability 查询必须与工厂真实行为一致（两处同一常量）。"""
        from Module.llm import LLMFactory

        assert LLMFactory.is_implemented("gemini/gemini-1.5-pro") is False
        assert LLMFactory.is_implemented("deepseek/deepseek-v4-flash") is True
        # 纯 provider 名也接受（与 supports_multimodal / context_window 同形）
        assert LLMFactory.is_implemented("gemini") is False

    def test_stt_registry_query_agrees_with_factory_behaviour(self):
        from Module.stt import STTFactory

        for stub in ("openai", "deepgram"):
            assert STTFactory.is_implemented(f"{stub}/whatever") is False
            with pytest.raises(NotImplementedError):
                STTFactory.create(f"{stub}/whatever")
        assert STTFactory.is_implemented("sensevoice/sense-voice") is True

    def test_llm_stub_selection_is_the_same_constant_the_factory_guards_with(self):
        """判据与守卫同源：改动"哪些 provider 未实现"只能改一处。"""
        from Module.llm import registry as llm_registry

        assert "gemini" in llm_registry._UNIMPLEMENTED_PROVIDERS
        # 预检不得再自带副本表（§90：不要第二套静态真相表）
        import Module.config.preflight as pf

        assert not hasattr(pf, "_LLM_STUB_PROVIDERS"), (
            "preflight 又自建了 LLM 未实现 provider 表——capability 真相必须只有 registry 一处"
        )

    def test_missing_sdk_does_not_crash_static_check(self, monkeypatch, capsys):
        """capability 查询拉起 openai SDK——依赖不齐时预检**不得**以裸 ImportError 崩掉。

        预检要在"什么都没装"的机器上跑完并说清缺什么（`_check_pip_deps` 判 FAIL）。
        查询本身答不了时 fail-open（同未知 provider 口径），总判定由依赖检查兜住。
        """
        import builtins
        import sys as _sys

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "openai" or name.startswith("openai."):
                raise ImportError("No module named 'openai'")
            return real_import(name, *args, **kwargs)

        # 清掉已缓存的模块，让 `from Module.llm import LLMFactory` 真的走到 __import__
        for mod in [k for k in list(_sys.modules)
                    if k.startswith("openai") or k.startswith("Module.llm")]:
            monkeypatch.delitem(_sys.modules, mod, raising=False)
        monkeypatch.setattr(builtins, "__import__", _fake_import)

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "gemini/gemini-1.5-pro"))   # 不得抛
        m._check_stt_env(_cfg("stt", "openai/whisper-1"))        # 不得抛
        out = _out(capsys)
        assert "[FAIL]" in out or "[PASS]" in out or "[WARN]" in out, out


# ---- N10-3：已实现 provider 不得被误拒 ---------------------------------------


class TestImplementedProviderNotFalselyRejected:
    """N10-3（ALREADY_GREEN_GUARD）：修 N-10 不得引入假阴性。"""

    def test_deepseek_with_key_and_default_url_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.delenv("DEEPSEEK_URL", raising=False)

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "deepseek/deepseek-v4-flash"))
        out = _out(capsys)

        assert m._COUNTS.failed == 0, out
        assert "[PASS]" in out

    def test_ollama_local_provider_skips_credential_check(self, capsys):
        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "ollama/qwen2.5:3b"))
        assert m._COUNTS.failed == 0
        assert "[PASS]" in _out(capsys)

    def test_local_stt_provider_needs_no_credential(self, monkeypatch, capsys):
        """Batch 3.5 §39：合法无-key provider（sensevoice / gpt-sovits）不得被误判为缺凭据。

        地址仍需可达（`SENSEVOICE_URL` 或 providers.yaml 默认值）——只判"无需凭据"，
        不把它变成"无需任何配置"。
        """
        monkeypatch.setenv("SENSEVOICE_URL", "http://127.0.0.1:50000")

        m = _fresh_preflight()
        m._check_stt_env(_cfg("stt", "sensevoice/sense-voice"))
        out = _out(capsys)

        assert m._COUNTS.failed == 0
        assert m._COUNTS.warned == 0, out
        assert "[PASS]" in out
        assert "凭据" not in out or "无需凭据" in out


# ---- N10-4（PCT-3）：静态预检 network-free，连通性归 --full -------------------


class TestStaticCheckStaysNetworkFree:
    """§15 / PCT-3：实现支持但服务离线 ⇒ 普通 `--check` 仍 PASS，只有 `--full` 负责 FAIL。"""

    def test_supported_provider_with_unreachable_endpoint_still_passes_check(
        self, monkeypatch, capsys
    ):
        """端点指向黑洞端口（静态检查只看 resolver 结果，不做任何网络调用）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_URL", "http://127.0.0.1:1")

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "deepseek/deepseek-v4-flash"))
        out = _out(capsys)

        assert m._COUNTS.failed == 0, (
            f"静态预检因'服务可能不通'而变红——连通性不属于 --check（PCT-3）。输出：\n{out}"
        )

    def test_no_network_calls_in_static_checks(self, monkeypatch):
        """静态检查不得发起任何 socket 连接（哨兵：connect 被调用即失败）。"""
        import socket

        def _boom(*args, **kwargs):
            raise AssertionError("静态预检发起了网络连接（PCT-3 违规）")

        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        monkeypatch.setenv("GEMINI_URL", "https://generativelanguage.googleapis.com")
        monkeypatch.setattr(socket.socket, "connect", _boom)
        monkeypatch.setattr(socket, "create_connection", _boom)

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "gemini/gemini-1.5-pro"))
        m._check_llm_env(_cfg("llm", "deepseek/deepseek-v4-flash"))


# ---- N10-5：`--full` 不得把 unsupported 报成成功 ------------------------------


class TestFullProbeDoesNotPassUnsupported:
    """§34：`--full` 下 unsupported 不得被跳过并报 PASS，也不得伪装成网络错误。"""

    def test_unimplemented_stt_provider_is_not_reported_pass(self, monkeypatch, capsys):
        import asyncio

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")

        m = _fresh_preflight()
        asyncio.run(m._check_stt_connectivity(_cfg("stt", "openai/whisper-1")))
        out = _out(capsys)

        assert "[PASS]" not in out, f"未实现的 STT provider 在 --full 下被报成 PASS：\n{out}"
        assert m._COUNTS.passed == 0
        assert m._COUNTS.warned >= 1, out

    def test_unimplemented_llm_provider_probe_is_not_labelled_network_error(
        self, monkeypatch, capsys
    ):
        """§79：unsupported ≠ connectivity error（也不能被报成 PASS）。"""
        import asyncio

        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        monkeypatch.setenv("GEMINI_URL", "https://generativelanguage.googleapis.com")

        m = _fresh_preflight()
        asyncio.run(m._check_llm_connectivity(_cfg("llm", "gemini/gemini-1.5-pro")))
        out = _out(capsys)

        assert "[PASS]" not in out, out
        assert "连通异常" not in out, (
            f"--full 把'未实现'报成了连通性失败（错误类别与真实失败层不符）：\n{out}"
        )
        assert "未实现" in out, out


# ---- N10-6（§56）：跨面一致性——预检判据 == registry 判据 ---------------------

class TestCrossSurfaceCapabilityConsistency:
    """预检声明的 capability 必须与 registry 的 capability 同源。

    遍历 `providers.yaml` 声明的每个 provider，断言"预检说可用" ⇔ "registry 说已实现"。
    这条测试是 N-10 的**防漂移网**：任何人把一份副本表塞回 preflight，
    只要它与 registry 不一致就会红。

    `custom` 是 meta-provider（无默认地址、必须由调用方提供 base_url），
    预检对它报的是"地址缺失"而非 capability，故不参与本断言。
    """

    @pytest.mark.parametrize("service", ["llm", "stt"])
    def test_declared_providers_verdict_matches_registry(self, service, monkeypatch, capsys):
        from Module.config.providers import load_provider_defaults

        if service == "llm":
            from Module.llm import LLMFactory as factory
        else:
            from Module.stt import STTFactory as factory

        providers = load_provider_defaults(service)
        # 让所有凭据/地址都"看起来齐备"——这样旧判据才会对未实现的 provider 报 PASS
        for cfg in providers.values():
            for key_field in ("env_key", "url_key"):
                key = str(cfg.get(key_field, "") or "")
                if key:
                    monkeypatch.setenv(key, "https://example.invalid")

        m = _fresh_preflight()
        checked = 0
        for name, cfg in providers.items():
            if name == "custom":
                continue
            models = cfg.get("models") or ["any-model"]
            model_id = f"{name}/{models[0]}"
            m._COUNTS.__init__()
            if service == "llm":
                m._check_llm_env(_cfg(service, model_id))
            else:
                m._check_stt_env(_cfg(service, model_id))
            captured = capsys.readouterr().out
            checked += 1

            implemented = factory.is_implemented(model_id)
            reported_usable = ("[PASS]" in captured) and "未实现" not in captured
            assert reported_usable == implemented, (
                f"{service}/{name}: registry.is_implemented={implemented} "
                f"但预检 reported_usable={reported_usable}\n{captured}"
            )
        assert checked >= 3, f"覆盖的 provider 太少（{checked}），测试可能没生效"


# ---- N10-7（Challenger F2/F3 补的守卫）：判据不得依赖第二张手抄表 ---------------


class TestCapabilityGateDoesNotDependOnTheEnvTable:
    """capability 判据必须只依赖 registry，**不能**依赖本模块手抄的 `_*_PROVIDER_ENV`。

    Challenger 实测过的反面（修前会原样复现 N-10）：把一个 provider 只登记进
    registry 的 `_UNIMPLEMENTED_PROVIDERS`、忘了改 env 表 —— 旧顺序先命中
    "未知 provider" 分支 early-return，于是预检又变绿了。
    """

    def test_llm_unimplemented_provider_absent_from_env_table_still_fails(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        monkeypatch.setenv("GEMINI_URL", "https://generativelanguage.googleapis.com")

        m = _fresh_preflight()
        monkeypatch.delitem(m._LLM_PROVIDER_ENV, "gemini", raising=False)  # 模拟漂移

        m._check_llm_env(_cfg("llm", "gemini/gemini-1.5-pro"))
        out = _out(capsys)

        assert m._COUNTS.failed >= 1, (
            "env 表里没有它，capability 判据就完全没跑——N-10 的假绿原样回来。"
            f"输出：\n{out}"
        )
        assert "未实现" in out

    def test_stt_unimplemented_provider_absent_from_env_table_still_warns(
        self, monkeypatch, capsys
    ):
        m = _fresh_preflight()
        monkeypatch.delitem(m._STT_PROVIDER_ENV, "openai", raising=False)

        m._check_stt_env(_cfg("stt", "openai/whisper-1"))
        out = _out(capsys)

        assert "未实现" in out, f"同 LLM：判据退化成'未知 provider'，缺口无人报。输出：\n{out}"

    def test_tts_report_layer_asks_the_registry(self, monkeypatch, capsys):
        """TTS 报告层必须问 `is_implemented`——否则登记的占位 provider 会被报成
        "[PASS] TTS 可用"，直到 `--full` 才以一个"连通异常"暴露（Capability 缺口
        伪装成网络问题，§79）。当前集合为空，用 monkeypatch 模拟"登记了占位"。"""
        from Module.tts import registry as tts_registry

        monkeypatch.setattr(tts_registry, "_UNIMPLEMENTED_PROVIDERS",
                            frozenset({"gpt-sovits"}), raising=True)
        m = _fresh_preflight()
        m._check_tts_env(_cfg("tts", "gpt-sovits/default"))
        out = _out(capsys)

        assert "[PASS]" not in out, f"占位 TTS provider 被报成可用：\n{out}"
        assert "未实现" in out, out

    def test_tts_full_probe_does_not_mislabel_unimplemented_as_connectivity(
        self, monkeypatch, capsys
    ):
        import asyncio

        from Module.tts import registry as tts_registry

        monkeypatch.setattr(tts_registry, "_UNIMPLEMENTED_PROVIDERS",
                            frozenset({"gpt-sovits"}), raising=True)
        m = _fresh_preflight()
        asyncio.run(m._check_tts_connectivity(_cfg("tts", "gpt-sovits/default")))
        out = _out(capsys)

        assert "[PASS]" not in out, out
        assert "连通异常" not in out, f"能力缺口被报成网络问题：\n{out}"
        assert "未实现" in out, out

    def test_unknown_and_unimplemented_are_reported_as_different_categories(
        self, monkeypatch, capsys
    ):
        """同一次运行里，"未知 provider" 与 "未实现 provider" 不得互相冒充（§79）。"""
        import asyncio

        m = _fresh_preflight()
        m._check_llm_env(_cfg("llm", "nope/x"))
        static_out = _out(capsys)
        asyncio.run(m._check_llm_connectivity(_cfg("llm", "nope/x")))
        full_out = _out(capsys)

        assert "未知" in static_out and "未实现" not in static_out, static_out
        assert "未知" in full_out and "未实现" not in full_out, full_out
