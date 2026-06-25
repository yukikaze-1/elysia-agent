"""make_test_method 单元测试。"""
import pytest

from Module._common.test_helpers import make_test_method


class TestMakeTestMethod:
    @pytest.mark.asyncio
    async def test_ok_returns_latency(self):
        async def ok_check(self):
            return True

        test_fn = make_test_method(ok_check)
        result = await test_fn(None)
        assert result["ok"] is True
        assert "latency_ms" in result
        assert isinstance(result["latency_ms"], int)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unhealthy_returns_false(self):
        async def fail_check(self):
            return False

        test_fn = make_test_method(fail_check)
        result = await test_fn(None)
        assert result["ok"] is False
        assert "latency_ms" in result

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        async def boom(self):
            raise RuntimeError("connection refused")

        test_fn = make_test_method(boom)
        result = await test_fn(None)
        assert result["ok"] is False
        assert result["error"] == "connection refused"
        assert "latency_ms" in result
