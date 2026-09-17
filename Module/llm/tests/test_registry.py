"""LLMFactory 注册表测试 — 批次 8a T12：custom 初始配置 SSRF 守卫。

测试经真实 `LLMFactory.create` 入口触发守卫（字面量内网 IP，不触网/DNS）。
"""

import pytest

from Module.llm.registry import LLMFactory

pytestmark = pytest.mark.service


class TestCustomSSRFGuard:
    def test_initial_custom_endpoint_ssrf_guarded(self):
        from Module._common.ssrf import SSRFError

        minimal_kwargs: dict = {}
        # 初始配置 custom + 内网 base_url → SSRFError（而非裸创建）
        with pytest.raises(SSRFError):
            LLMFactory.create(
                "custom/x",
                base_url="http://169.254.169.254/",
                api_key="k",
                **minimal_kwargs,
            )

    def test_builtin_provider_skips_guard(self):
        """内置 provider（openai）跳过守卫——内网 base_url 也能创建（守卫仅 custom）。"""

        backend = LLMFactory.create(
            "openai/gpt-5", base_url="http://169.254.169.254/", api_key="sk-test"
        )
        assert backend is not None


class TestEmptyBaseUrlFailsEarly:
    """S2-H7：内置 provider 没有可用默认地址时，必须**建连即报错**。

    原状：空 `base_url` 被直接交给后端（实测 `AsyncOpenAI(base_url="")` 的 base_url 就是
    空串——SDK 只对 None 回退）⇒ 直到首次请求才以一个莫名其妙的 URL 错误暴露。
    MiniMax TTS 早就是"为空即抛可操作错误"的做法，LLM 侧对齐它。

    例外是有默认值的那些（deepseek/openai/ollama 已在 providers.yaml 声明）——本用例
    用没有默认值的 `mimo` 触发。
    """

    def test_builtin_without_default_raises_actionable_error(self, monkeypatch):
        monkeypatch.delenv("MIMO_URL", raising=False)

        with pytest.raises(ValueError) as exc_info:
            LLMFactory.create("mimo/mimo-v2.5", api_key="dummy")

        msg = str(exc_info.value)
        assert "base_url" in msg and "MIMO_URL" in msg, (
            f"报错没告诉运维该设哪个环境变量：{msg}")

    def test_builtin_with_declared_default_does_not_raise(self, monkeypatch):
        """反向：有默认值的 provider（deepseek）不得被这条守卫误伤。"""
        monkeypatch.delenv("DEEPSEEK_URL", raising=False)
        backend = LLMFactory.create("deepseek/deepseek-v4-flash", api_key="dummy")
        assert backend is not None
        assert "deepseek" in str(backend._client.base_url) or \
            "deepseek" in str(getattr(backend, "_base_url", "")), \
            "没拿到 providers.yaml 里声明的默认地址"
