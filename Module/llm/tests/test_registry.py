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
        from Module._common.ssrf import SSRFError

        backend = LLMFactory.create(
            "openai/gpt-5", base_url="http://169.254.169.254/", api_key="sk-test"
        )
        assert backend is not None
