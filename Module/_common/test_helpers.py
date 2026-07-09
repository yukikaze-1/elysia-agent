"""test() 工厂：消除 5 个 backend 中逐字相同的 test() 实现。"""
import time
from collections.abc import Awaitable, Callable


def make_test_method(
    health_fn: Callable[[], Awaitable[bool]],
) -> Callable[[], Awaitable[dict]]:
    """返回标准 test() 协程：计时 + 调用 health_fn + {ok, latency_ms, error}。

    health_fn — 绑定到 self 的异步函数，返回 True 表示健康。
    调用方在 __init__ 末尾使用：
        self.test = make_test_method(self._health_check)
    """
    async def test(self) -> dict:
        t0 = time.perf_counter()
        try:
            ok = await health_fn()
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": ok, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}

    return test
