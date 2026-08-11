"""D6b 公开入口限流 — 单进程 token-bucket 限流器。

语义（plan Task D6b）：
- key 优先认证主体（token 摘要），匿名回退 IP；默认不信任 ``X-Forwarded-For``
  （本实现不解析代理头——伪造代理头不得改变 key，防限流绕过与内存 DoS）。
- 内存有界（``max_keys``）：超限时拒绝新 key（宁可误拒，不撑爆内存）；
  旧 key 定期清理（``stale_after`` 未使用即回收）。
- **单进程语义**：per-process 限流，不假装全局限流——多实例部署需自建
  共享 backend（计划禁止「假装全局限流」），见 docs 产品化门禁。
- ``rpm=0`` 关闭该类别限流；未配置类别不限流。
"""

from __future__ import annotations

import hashlib
import time

from aiohttp import web


class TokenBucket:
    """单 key 令牌桶：容量 + 每秒回填率（RPM → refill_per_sec = rpm / 60）。

    burst 消耗 capacity 内即时放行，超过后按回填率恢复；回填封顶到 capacity
    （长时间空闲不囤积无限 token）。
    """

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.tokens = capacity
        self.refill_per_sec = refill_per_sec
        self.last_refill = time.monotonic()
        self.last_used = self.last_refill

    def try_consume(self, now: float | None = None) -> bool:
        """消耗一个 token。True=放行，False=限流。更新 last_used（活跃 key 不被回收）。"""
        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        self.last_used = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after_ms(self) -> int:
        """限流后的建议重试退避毫秒（下限 1000ms）。

        Task 7（BE-P1-03）：按 ``(capacity - tokens)`` 需回填量估算（保守略长于
        严格回填 1 token——耗尽后回满才再给一桶配额）；``max(1.0, ...)`` 防
        capacity==tokens（满桶）时为 0；``max(1000, ...)`` 保证客户端退避下界
        （避免瞬时自旋重试）。
        """
        return max(1000, int(max(1.0, self.capacity - self.tokens)
                             / self.refill_per_sec * 1000))


class RateLimiter:
    """按 (category, key) 的令牌桶集合。

    Args:
        rules: 类别 → 每分钟允许次数（rpm）。rpm=0 关闭该类别。
        max_keys: 桶上限（内存有界）。超限时新 key 拒绝，防伪造 header 内存 DoS。
        cleanup_interval: 周期性清理间隔（秒）。
        stale_after: 超过该时长未使用的桶回收（秒）。
    """

    def __init__(self, rules: dict[str, int], *, max_keys: int = 10_000,
                 cleanup_interval: float = 60.0, stale_after: float = 3600.0) -> None:
        self._rules = rules
        self._max_keys = max_keys
        self._cleanup_interval = cleanup_interval
        self._stale_after = stale_after
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        # 0.0 起点：首次 check 即跑一次（空）清理，测试可注入 now 确定性复现回收
        self._last_cleanup = 0.0

    def check(self, category: str, key: str, *, now: float | None = None) -> bool:
        """检查并消耗一个 token。True=放行，False=限流拒绝。"""
        rpm = self._rules.get(category, 0)
        if rpm <= 0:
            return True
        now = time.monotonic() if now is None else now
        if now - self._last_cleanup >= self._cleanup_interval:
            self._cleanup(now)
        bkey = (category, key)
        if self._max_keys and len(self._buckets) >= self._max_keys:
            bucket = self._buckets.get(bkey)
            if bucket is None:
                return False  # 内存有界：新 key 超上限，宁可误拒
            return bucket.try_consume(now)
        bucket = self._buckets.get(bkey)
        if bucket is None:
            bucket = TokenBucket(rpm, rpm / 60.0)
            self._buckets[bkey] = bucket
        return bucket.try_consume(now)

    def _cleanup(self, now: float) -> None:
        """回收 stale_after 未使用的桶（amortized：每 cleanup_interval 一次全扫）。"""
        stale = [k for k, b in self._buckets.items()
                 if now - b.last_used > self._stale_after]
        for k in stale:
            del self._buckets[k]
        self._last_cleanup = now

    def retry_after_ms(self, category: str, key: str) -> int:
        """限流退避毫秒：桶存在返回桶估算；无桶或类别未限流 → 0（不限流/不拒）。

        Task 7（BE-P1-03）：限流块构造 RATE_LIMITED 的 retryAfterMs 数据源——
        刚被拒后桶已耗，返回 ≥1000 的数值退避；配额内或无桶时为 0（不携带退避）。
        """
        rpm = self._rules.get(category, 0)
        if rpm <= 0:
            return 0
        bucket = self._buckets.get((category, key))
        if bucket is None:
            return 0
        return bucket.retry_after_ms()

    def reset(self) -> None:
        """清空全部桶（测试隔离）。"""
        self._buckets.clear()


def rate_key_for_request(request: web.Request) -> str:
    """请求限流 key：认证主体（token 摘要）优先，匿名回退 IP。

    默认不信任 ``X-Forwarded-For``——只有显式 trusted proxy 才应解析，本实现不解析
    （无信任代理配置），伪造代理头不影响 key（防限流绕过与高基数内存 DoS）。
    """
    auth = request.headers.get("Authorization", "")
    if auth:
        return "auth:" + hashlib.sha256(auth.encode("utf-8")).hexdigest()[:16]
    return "ip:" + (request.remote or "unknown")
