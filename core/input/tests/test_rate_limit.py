"""D6b 公开入口限流 — token-bucket limiter + REST 429 / WS RATE_LIMITED。

覆盖 plan Task D6b：
- TokenBucket 容量/回填/封顶语义
- RateLimiter 规则禁用、per-key 隔离、内存有界（max_keys）+ 旧 key 清理
- key 解析：认证主体优先、匿名回退 IP、不信任 X-Forwarded-For
- REST 高成本入口超限 → 429 RATE_LIMITED + Retry-After
- WS 消息超限 → error RATE_LIMITED + retry hint，连接存活
- WS 建连超限 → 升级前 429

Run: python -m pytest core/input/tests/test_rate_limit.py -v --asyncio-mode=auto
"""

import asyncio
import json

import pytest

from core.input.rate_limiter import RateLimiter, TokenBucket, rate_key_for_request


# ── 单元：TokenBucket ─────────────────────────────────────────────────────

class TestTokenBucket:
    def test_burst_within_capacity_allowed(self):
        b = TokenBucket(capacity=3, refill_per_sec=1.0)
        now = 1000.0
        assert b.try_consume(now) is True
        assert b.try_consume(now) is True
        assert b.try_consume(now) is True
        assert b.try_consume(now) is False  # 容量耗尽

    def test_refill_over_time(self):
        b = TokenBucket(capacity=1, refill_per_sec=1.0)
        now = 1000.0
        assert b.try_consume(now) is True
        assert b.try_consume(now) is False
        assert b.try_consume(now + 1.0) is True  # 1s 回填 1 token
        assert b.try_consume(now + 1.0) is False

    def test_refill_capped_at_capacity(self):
        b = TokenBucket(capacity=2, refill_per_sec=10.0)
        now = 1000.0
        assert b.try_consume(now) is True
        assert b.try_consume(now) is True
        assert b.try_consume(now) is False
        # 长时间过去，最多回填到 capacity（不能囤积无限 token）
        assert b.try_consume(now + 100.0) is True
        assert b.try_consume(now + 100.0) is True
        assert b.try_consume(now + 100.0) is False


# ── 单元：RateLimiter ─────────────────────────────────────────────────────

class TestRateLimiter:
    def test_rpm_zero_disables(self):
        limiter = RateLimiter({"messages": 0})
        assert limiter.check("messages", "k") is True
        assert limiter.check("messages", "k") is True

    def test_unknown_category_allowed(self):
        limiter = RateLimiter({"messages": 1})
        assert limiter.check("other", "k") is True

    def test_per_key_independent(self):
        limiter = RateLimiter({"messages": 1})
        assert limiter.check("messages", "k1") is True
        assert limiter.check("messages", "k1") is False
        assert limiter.check("messages", "k2") is True  # 另一 key 不受影响

    def test_bounded_memory_refuses_new_key_at_capacity(self):
        # stale_after 用大值——本测试只验证容量上限，不涉及过期回收
        limiter = RateLimiter({"messages": 1000}, max_keys=3,
                              cleanup_interval=0.0, stale_after=10_000.0)
        now = 1000.0
        assert limiter.check("messages", "k1", now=now) is True
        assert limiter.check("messages", "k2", now=now) is True
        assert limiter.check("messages", "k3", now=now) is True
        # 高基数 key 超过 max_keys → 拒绝（防内存 DoS，宁可误拒）
        assert limiter.check("messages", "k4", now=now) is False

    def test_stale_key_evicted_and_reusable(self):
        limiter = RateLimiter({"messages": 1000}, max_keys=2,
                              cleanup_interval=0.0, stale_after=1000.0)
        now = 100.0
        assert limiter.check("messages", "old", now=now) is True
        assert limiter.check("messages", "other", now=now) is True
        assert limiter.check("messages", "new", now=now) is False  # 满
        # old 过期 → 下次 check 清理 → 可再入
        assert limiter.check("messages", "old", now=now + 2000.0) is True


# ── 单元：key 解析 ────────────────────────────────────────────────────────

class TestRateKey:
    def test_authenticated_key_stable_across_ips(self):
        """认证主体优先：同一 token 不同 IP → 同一 key。"""
        from types import SimpleNamespace
        auth = "Bearer secret"
        r1 = SimpleNamespace(headers={"Authorization": auth}, remote="1.2.3.4")
        r2 = SimpleNamespace(headers={"Authorization": auth}, remote="5.6.7.8")
        assert rate_key_for_request(r1) == rate_key_for_request(r2)
        assert rate_key_for_request(r1).startswith("auth:")

    def test_anonymous_falls_back_to_ip(self):
        from types import SimpleNamespace
        a = SimpleNamespace(headers={}, remote="1.2.3.4")
        b = SimpleNamespace(headers={}, remote="5.6.7.8")
        assert rate_key_for_request(a) == "ip:1.2.3.4"
        assert rate_key_for_request(a) != rate_key_for_request(b)

    def test_x_forwarded_for_not_trusted(self):
        """默认不信任 X-Forwarded-For——伪造代理头不改变 key。"""
        from types import SimpleNamespace
        forged = SimpleNamespace(headers={"X-Forwarded-For": "6.6.6.6"}, remote="1.2.3.4")
        plain = SimpleNamespace(headers={}, remote="1.2.3.4")
        assert rate_key_for_request(forged) == rate_key_for_request(plain)


# ── 集成：REST 429 ────────────────────────────────────────────────────────

@pytest.mark.service
class TestRestRateLimit:
    @pytest.mark.asyncio
    async def test_provider_test_burst_429_with_retry_after(self, aiohttp_client):
        """/api/providers/test 超限 → 429 RATE_LIMITED + Retry-After（中间件先于 handler）。"""
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, rate_limits={"provider_test": 2})
        client = await aiohttp_client(app)
        try:
            for _ in range(2):
                r = await client.post("/api/providers/test", json={"service": "llm"})
                assert r.status != 429, f"配额内不应限流: {r.status}"
            r = await client.post("/api/providers/test", json={"service": "llm"})
            assert r.status == 429
            body = json.loads((await r.read()).decode())
            assert body["code"] == "RATE_LIMITED"
            assert body["retryable"] is True
            assert "Retry-After" in r.headers
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_disabled_category_not_limited(self, aiohttp_client):
        """未配置的类别不限流（rate_limits 空 → 无中间件）。"""
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, rate_limits=None)
        client = await aiohttp_client(app)
        try:
            for _ in range(3):
                r = await client.post("/api/providers/test", json={"service": "llm"})
                assert r.status != 429
        finally:
            await agent.stop()


# ── WS：消息级限流（FakeWs 确定性）──────────────────────────────────────

@pytest.mark.service
class TestWsMessageRateLimit:
    async def _make(self):
        """FakeAgent + FakeWs（仿 test_voice_ws.make）。"""
        from core.input.tests.test_voice_ws import FakeAgent, FakeWs
        from core.input.voice_sessions import VoiceSessionManager

        ws = FakeWs()
        agent = FakeAgent(ws)
        agent.bus = type("Bus", (), {
            "post": lambda self, d, **kw: agent.bus_posts.append(d)})()
        async def set_service(service, enabled):
            return {"ok": True, "enabled": enabled}
        agent.set_service = set_service
        mgr = VoiceSessionManager(agent=agent, emit=ws.send_json,
                                  on_vad_interrupt=lambda: None)
        return agent, ws, mgr

    @staticmethod
    def _last_error(ws) -> dict:
        errs = [s for s in ws.sent if s.get("type") == "error"]
        assert errs, f"无 error 帧: {ws.sent}"
        return errs[-1]

    @pytest.mark.asyncio
    async def test_text_over_limit_emits_rate_limited_and_connection_alive(self):
        """v2 text 超限 → error RATE_LIMITED + retry hint，连接存活。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 1})
        agent, ws, mgr = await self._make()
        # 第一条 text 消耗唯一 token → 进 handler（FakeAgent 无 process_message → INTERNAL ACK）
        await handle_json_message(
            {"type": "text", "content": "hi", "clientRequestId": "r1",
             "clientMessageId": "m1"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        # 第二条 text → 超限 → RATE_LIMITED（不再进 handler）
        await handle_json_message(
            {"type": "text", "content": "hi2", "clientRequestId": "r2",
             "clientMessageId": "m2"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        err = self._last_error(ws)
        assert err["code"] == "RATE_LIMITED"
        assert err["retryable"] is True
        assert err.get("details", {}).get("retryHint")
        # 连接存活：后续 interrupt 仍被处理（bus.post 收到）
        await handle_json_message(
            {"type": "interrupt"}, agent, None, ws, voice_mgr=mgr,
            limiter=limiter, rate_key="k")
        assert any(getattr(p, "event_type", "") == "interrupt"
                   for p in agent.bus_posts), f"interrupt 未处理: {ws.sent}"

    @pytest.mark.asyncio
    async def test_within_limit_not_limited(self):
        """配额内：不产生 RATE_LIMITED。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 5})
        agent, ws, mgr = await self._make()
        await handle_json_message(
            {"type": "text", "content": "hi", "clientRequestId": "r1",
             "clientMessageId": "m1"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        assert not any(s.get("type") == "error" and s.get("code") == "RATE_LIMITED"
                       for s in ws.sent)


# ── 集成：WS 建连限流 ─────────────────────────────────────────────────────

@pytest.mark.service
class TestWsConnectionRateLimit:
    @pytest.mark.asyncio
    async def test_ws_connect_over_limit_429_before_upgrade(self, aiohttp_client):
        """WS 建连超限 → 升级前 429 RATE_LIMITED（不建立连接）。"""
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, rate_limits={"ws": 1})
        client = await aiohttp_client(app)
        try:
            # 第 1 次建连成功（配额内）
            ws = await client.session.ws_connect(client.make_url("/ws"))
            await ws.close()
            # 第 2 次建连 → 429
            import aiohttp
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await client.session.ws_connect(client.make_url("/ws"))
            assert exc.value.status == 429
        finally:
            await agent.stop()
