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
import hashlib
import json

import pytest

from core.network.rate_limiter import RateLimiter, TokenBucket, rate_key_for_request


# ── 单元：TokenBucket ─────────────────────────────────────────────────────

@pytest.mark.service
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

    def test_retry_after_ms_drained_bucket_at_least_1000(self):
        """满/空桶 retry_after_ms ≥ 1000 且有限（int）——客户端退避下界。"""
        b = TokenBucket(capacity=1, refill_per_sec=1.0)
        now = 1000.0
        assert b.try_consume(now) is True   # 唯一 token 消耗 → 桶空（下次立即拒绝）
        retry = b.retry_after_ms()
        assert isinstance(retry, int)
        assert retry >= 1000, f"满桶退避应 ≥1000ms，实际 {retry}"
        assert retry < 10**9                # 有限值（非 inf/NaN）


# ── 单元：RateLimiter ─────────────────────────────────────────────────────

@pytest.mark.service
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

    def test_retry_after_ms_no_bucket_or_disabled_returns_zero(self):
        """无桶（未消耗过）或未限流类别 → retry_after_ms() == 0。"""
        limiter = RateLimiter({"messages": 1})
        assert limiter.retry_after_ms("messages", "k") == 0     # 无桶 → 0
        assert limiter.retry_after_ms("other", "k") == 0        # 未限流类别 → 0
        assert limiter.retry_after_ms("messages", "k2") == 0    # 无桶 → 0

    def test_retry_after_ms_drained_bucket_ge_1000(self):
        """桶已耗（刚被拒）→ retry_after_ms ≥ 1000（与 TokenBucket 同源）。"""
        limiter = RateLimiter({"messages": 1})
        now = 1000.0
        assert limiter.check("messages", "k", now=now) is True
        assert limiter.check("messages", "k", now=now) is False  # 桶空
        retry = limiter.retry_after_ms("messages", "k")
        assert retry >= 1000, f"耗空桶退避应 ≥1000ms，实际 {retry}"

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

@pytest.mark.service
class TestRateKey:
    def test_authenticated_key_stable_across_ips(self):
        """认证主体优先：同一合法 token（∈ valid_tokens）不同 IP → 同一 key。"""
        from types import SimpleNamespace
        auth = "Bearer secret"
        valid = frozenset({"secret"})
        r1 = SimpleNamespace(headers={"Authorization": auth}, remote="1.2.3.4")
        r2 = SimpleNamespace(headers={"Authorization": auth}, remote="5.6.7.8")
        assert rate_key_for_request(r1, valid) == rate_key_for_request(r2, valid)
        assert rate_key_for_request(r1, valid).startswith("auth:")

    def test_anonymous_falls_back_to_ip(self):
        from types import SimpleNamespace
        a = SimpleNamespace(headers={}, remote="1.2.3.4")
        b = SimpleNamespace(headers={}, remote="5.6.7.8")
        assert rate_key_for_request(a, frozenset()) == "ip:1.2.3.4"
        assert rate_key_for_request(a, frozenset()) != rate_key_for_request(b, frozenset())

    def test_x_forwarded_for_not_trusted(self):
        """默认不信任 X-Forwarded-For——伪造代理头不改变 key。"""
        from types import SimpleNamespace
        forged = SimpleNamespace(headers={"X-Forwarded-For": "6.6.6.6"}, remote="1.2.3.4")
        plain = SimpleNamespace(headers={}, remote="1.2.3.4")
        assert rate_key_for_request(forged, frozenset()) == rate_key_for_request(plain, frozenset())

    def test_valid_token_gets_auth_bucket(self):
        """合法 token（∈ valid_tokens）→ auth:<sha256(token)[:16]> 桶。"""
        from types import SimpleNamespace
        r = SimpleNamespace(headers={"Authorization": "Bearer ok"}, remote="1.2.3.4")
        key = rate_key_for_request(r, frozenset({"ok"}))
        assert key == "auth:" + hashlib.sha256(b"ok").hexdigest()[:16]

    def test_invalid_token_falls_back_to_ip(self):
        """无效 token（∉ valid_tokens）→ ip 桶——任意 Authorization 不再独立分桶。"""
        from types import SimpleNamespace
        valid = frozenset({"secret"})
        r1 = SimpleNamespace(headers={"Authorization": "Bearer bad1"}, remote="1.2.3.4")
        r2 = SimpleNamespace(headers={"Authorization": "Bearer bad2"}, remote="1.2.3.4")
        assert rate_key_for_request(r1, valid) == "ip:1.2.3.4"
        assert rate_key_for_request(r2, valid) == "ip:1.2.3.4"
        assert rate_key_for_request(r1, valid) == rate_key_for_request(r2, valid)

    def test_missing_authorization_falls_back_to_ip(self):
        """缺失 Authorization → ip 桶（即使 valid_tokens 非空）。"""
        from types import SimpleNamespace
        r = SimpleNamespace(headers={}, remote="9.9.9.9")
        assert rate_key_for_request(r, frozenset({"secret"})) == "ip:9.9.9.9"


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


# ── BE-P2-02：认证主体分类限流 ─────────────────────────────────────────────

@pytest.mark.service
class TestRateLimitAuthPrincipal:
    """BE-P2-02：限流按认证主体分桶——缺失/无效 Authorization 统一 ip 桶。

    只有命中 valid_tokens 的 Bearer token 才获 auth 桶；伪造/轮换 token 不再
    获得 per-token 配额，也不能填满 max_keys 挤掉合法主体桶。
    """

    def test_fake_token_rotation_same_ip_no_quota_explosion(self):
        """同 IP 轮换 10001 个假 Token → 仍受 ip 桶限制（≤2 次放行），不挤掉合法主体。"""
        from types import SimpleNamespace
        limiter = RateLimiter({"messages": 2}, max_keys=10_000)
        valid = frozenset({"real-token"})
        # 合法主体先占自己的桶：2 次配额后第 3 次被限
        legit = SimpleNamespace(headers={"Authorization": "Bearer real-token"},
                                remote="1.2.3.4")
        for _ in range(2):
            assert limiter.check("messages", rate_key_for_request(legit, valid)) is True
        assert limiter.check("messages", rate_key_for_request(legit, valid)) is False
        # 同 IP 轮换 10001 个假 token：全部落同一 ip 桶，最多放行 2 次（ip 配额）
        allowed = 0
        for i in range(10_001):
            fake = SimpleNamespace(
                headers={"Authorization": f"Bearer fake-{i}"}, remote="1.2.3.4")
            if limiter.check("messages", rate_key_for_request(fake, valid)):
                allowed += 1
        assert allowed <= 2, f"假 token 轮换应受 ip 桶限制，实际放行 {allowed}"
        # 不创建 per-token 桶：桶数恒为 2（合法 auth + ip）——旧行为会填满 max_keys
        assert len(limiter._buckets) == 2  # noqa: SLF001 — 测试断言桶基数
        # 合法主体桶未被挤掉/复用：仍是已耗的旧桶（配额仍尽 → 仍被限）
        assert limiter.check("messages", rate_key_for_request(legit, valid)) is False
        # 攻击后新合法主体仍能创建自己的桶（旧行为下 max_keys 满会拒新 key）
        late = SimpleNamespace(headers={"Authorization": "Bearer late-valid"},
                               remote="1.2.3.4")
        valid2 = frozenset({"real-token", "late-valid"})
        assert limiter.check("messages", rate_key_for_request(late, valid2)) is True

    @pytest.mark.asyncio
    async def test_invalid_token_and_anonymous_share_ip_bucket_http(self, aiohttp_client):
        """端到端：匿名耗尽 ip 桶后，无效 token（同 IP）立即 429——不再 per-token 独立配额。"""
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, access_token="secret",
                         rate_limits={"provider_test": 2})
        client = await aiohttp_client(app)
        try:
            # 匿名 2 次（限流先于 auth，消耗 ip 桶）
            for _ in range(2):
                r = await client.post("/api/providers/test", json={"service": "llm"})
                assert r.status != 429, f"配额内不应限流: {r.status}"
            # 无效 token（同 IP）→ 同一 ip 桶已耗 → 429（旧行为：独立 auth 桶 → 放行）
            r3 = await client.post("/api/providers/test", json={"service": "llm"},
                                   headers={"Authorization": "Bearer wrong-token"})
            assert r3.status == 429
            body = json.loads((await r3.read()).decode())
            assert body["code"] == "RATE_LIMITED"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_valid_token_own_auth_bucket_http(self, aiohttp_client):
        """端到端：合法 token 独立 auth 桶——匿名耗尽 ip 桶后合法 token 仍可用自己配额。"""
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, access_token="secret",
                         rate_limits={"provider_test": 2})
        client = await aiohttp_client(app)
        try:
            for _ in range(2):
                assert (await client.post(
                    "/api/providers/test", json={"service": "llm"})).status != 429
            hdr = {"Authorization": "Bearer secret"}
            # 合法 token 独立 auth 桶：不受 ip 桶影响，自己的 2 次配额放行
            for _ in range(2):
                r = await client.post(
                    "/api/providers/test", json={"service": "llm"}, headers=hdr)
                assert r.status != 429, f"合法 token 配额内不应限流: {r.status}"
            # 自己配额耗尽 → 429
            assert (await client.post(
                "/api/providers/test", json={"service": "llm"}, headers=hdr)).status == 429
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_ws_connect_fake_token_no_fresh_bucket(self, aiohttp_client):
        """WS 建连：无 token 环境轮换假 Authorization → 仍受 ip 桶限制（不获独立桶）。"""
        import aiohttp
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, rate_limits={"ws": 1})
        client = await aiohttp_client(app)
        try:
            # 第 1 次假 token 建连成功（ip 桶配额内）
            ws = await client.session.ws_connect(
                client.make_url("/ws"), headers={"Authorization": "Bearer fake-1"})
            await ws.close()
            # 第 2 次不同假 token、同 IP → 同一 ip 桶已耗 → 升级前 429
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await client.session.ws_connect(
                    client.make_url("/ws"), headers={"Authorization": "Bearer fake-2"})
            assert exc.value.status == 429
            assert exc.value.headers.get("Retry-After")
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

    @staticmethod
    def _rate_limited_acks(ws) -> list[dict]:
        return [a for s in ws.sent if s.get("type") == "user_message.ack"
                for a in [s] if a.get("status") == "rejected"
                and a.get("error", {}).get("code") == "RATE_LIMITED"]

    @staticmethod
    def _has_interrupt(agent) -> bool:
        return any(getattr(p, "event_type", "") == "interrupt" for p in agent.bus_posts)

    @pytest.mark.asyncio
    async def test_text_over_limit_rejected_ack_with_original_request_id(self):
        """v2 text 超限（合法 clientRequestId）→ rejected ACK RATE_LIMITED
        （原 clientRequestId + retryAfterMs），promise 立即 settle；连接存活。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 1})
        agent, ws, mgr = await self._make()
        # 第一条 text 消耗唯一 token → 进 handler（FakeAgent 无 process_message → INTERNAL ACK）
        await handle_json_message(
            {"type": "text", "content": "hi", "clientRequestId": "r1",
             "clientMessageId": "m1"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        # 第二条 text → 超限 → rejected ACK（不再进 handler）
        await handle_json_message(
            {"type": "text", "content": "hi2", "clientRequestId": "r2",
             "clientMessageId": "m2"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        rl = self._rate_limited_acks(ws)
        assert rl, f"应收到 rejected RATE_LIMITED ACK: {ws.sent}"
        assert rl[-1]["clientRequestId"] == "r2"          # 原 clientRequestId 回显
        assert rl[-1]["error"]["retryable"] is True
        assert rl[-1]["error"]["retryAfterMs"] >= 1000    # 数值退避
        assert not any(s.get("type") == "error" and s.get("code") == "RATE_LIMITED"
                       for s in ws.sent)                  # text 走 ACK 通道，非 ErrorMessage
        # 连接存活：后续 interrupt 仍被处理（bus.post 收到）
        await handle_json_message(
            {"type": "interrupt"}, agent, None, ws, voice_mgr=mgr,
            limiter=limiter, rate_key="k")
        assert self._has_interrupt(agent), f"interrupt 未处理: {ws.sent}"

    @pytest.mark.asyncio
    async def test_voice_commit_over_limit_rejected_ack(self):
        """voice.commit 超限（合法 clientRequestId）→ rejected ACK RATE_LIMITED。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 1})
        agent, ws, mgr = await self._make()
        assert limiter.check("messages", "k") is True  # 消耗唯一 token
        await handle_json_message(
            {"type": "voice.commit", "recordingId": "rec_1", "text": "语音",
             "clientRequestId": "vr1"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        rl = self._rate_limited_acks(ws)
        assert rl, f"voice.commit 应收到 rejected RATE_LIMITED ACK: {ws.sent}"
        assert rl[-1]["clientRequestId"] == "vr1"
        assert rl[-1]["error"]["retryable"] is True
        assert rl[-1]["error"]["retryAfterMs"] >= 1000
        assert not any(s.get("type") == "error" for s in ws.sent)

    @pytest.mark.asyncio
    async def test_switch_model_over_limit_error_with_request_id(self):
        """其余命令（switch_model）超限 → generic error RATE_LIMITED + clientRequestId + 顶层 retryAfterMs。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"provider_test": 1})
        agent, ws, mgr = await self._make()
        assert limiter.check("provider_test", "k") is True  # 消耗唯一 token
        await handle_json_message(
            {"type": "switch_model", "service": "llm", "model": "x",
             "clientRequestId": "cm1"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        err = self._last_error(ws)
        assert err["code"] == "RATE_LIMITED"
        assert err["retryable"] is True
        assert err["clientRequestId"] == "cm1"       # 关联客户端请求 ID
        assert err["retryAfterMs"] >= 1000            # 顶层退避
        assert not any(s.get("type") == "user_message.ack" for s in ws.sent)

    @pytest.mark.asyncio
    async def test_text_over_limit_missing_request_id_generic_error(self):
        """text 超限但 clientRequestId 缺失 → 退化 generic error RATE_LIMITED。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 1})
        agent, ws, mgr = await self._make()
        assert limiter.check("messages", "k") is True  # 消耗唯一 token
        await handle_json_message(
            {"type": "text", "content": "hi"},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        err = self._last_error(ws)
        assert err["code"] == "RATE_LIMITED"
        assert err["retryable"] is True
        assert err["retryAfterMs"] >= 1000
        assert not any(s.get("type") == "user_message.ack" for s in ws.sent)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_crid", [
        123,                                   # 非 str
        "x" * 129,                             # 超长（>128）
    ])
    async def test_text_over_limit_invalid_request_id_generic_error(self, bad_crid):
        """text 超限但 clientRequestId 非法（非 str/超长）→ 退化 generic error。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 1})
        agent, ws, mgr = await self._make()
        assert limiter.check("messages", "k") is True  # 消耗唯一 token
        await handle_json_message(
            {"type": "text", "content": "hi", "clientRequestId": bad_crid},
            agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k")
        err = self._last_error(ws)
        assert err["code"] == "RATE_LIMITED"
        assert err.get("clientRequestId") is None     # 非法值不回显
        assert not any(s.get("type") == "user_message.ack" for s in ws.sent)

    @pytest.mark.asyncio
    async def test_concurrent_sends_only_limited_one_rejected(self):
        """并发两条 text：配额内一条进 handler，超限一条 rejected RATE_LIMITED——只拒被限那条。"""
        from core.input.ws_handler import handle_json_message

        limiter = RateLimiter({"messages": 1})
        agent, ws, mgr = await self._make()
        await asyncio.gather(
            handle_json_message(
                {"type": "text", "content": "a", "clientRequestId": "ra"},
                agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k"),
            handle_json_message(
                {"type": "text", "content": "b", "clientRequestId": "rb"},
                agent, None, ws, voice_mgr=mgr, limiter=limiter, rate_key="k"),
        )
        rl = self._rate_limited_acks(ws)
        assert len(rl) == 1, f"只拒被限那条: {ws.sent}"
        # 配额内那条进入 handler（FakeAgent 无 process_message → INTERNAL rejected）
        internal = [a for s in ws.sent if s.get("type") == "user_message.ack"
                    for a in [s] if a.get("status") == "rejected"
                    and a.get("error", {}).get("code") == "INTERNAL"]
        assert len(internal) == 1

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
        assert not self._rate_limited_acks(ws)


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
            # 第 2 次建连 → 429 + Retry-After 头（与 REST 中间件一致）
            import aiohttp
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await client.session.ws_connect(client.make_url("/ws"))
            assert exc.value.status == 429
            assert exc.value.headers.get("Retry-After"), \
                f"建连 429 应带 Retry-After 头: {exc.value.headers}"
        finally:
            await agent.stop()
