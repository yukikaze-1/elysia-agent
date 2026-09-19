"""Ollama 运行时 context authority —— wire 级确定性用例（Batch 17 / KB-13 / AR-16）。

## 这些用例的判据是**行为**，不是"某个类存不存在"

本模块只走公开入口（`LLMFactory.create` + `LLMBackend` 协议方法），用
`httpx.MockTransport` 抓**出站请求本身**，断言：

1. `providers.yaml::llm.ollama.context_window` 的声明值 **必须**出现在请求体的
   `options.num_ctx` 上（否则 budgeting authority 与 serving window 分叉）；
2. 承载它的**必须是 native `/api/chat`**——实测（Ollama 0.24.0，冷启动 A/B）
   OpenAI-compatible `/v1/chat/completions` **完全忽略** `options.num_ctx`
   （带与不带都是 4096），所以"在 compat 端点上加个 options"不是修复；
3. **通用预算回退（32768）绝不许**变成运行时分配（`num_ctx` 是真实显存分配请求，
   与"预算别撑爆"不是一个风险等级）；
4. 非 Ollama provider 的请求体**零污染**（不带 `options` / `num_ctx` / `keep_alive`）。

修复前本模块 1/2 组为 **RED**（出站是 `/v1/chat/completions` 且没有 `options`），
即 KB-13 的可执行复现；真实 daemon 上的旁证见
`docs/review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md`。

分层：`service`（MockTransport，无真实网络、无真实 Ollama、不进任何 CI 的网络依赖）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from Module.llm.registry import LLMFactory

pytestmark = pytest.mark.service

_TIMEOUT = httpx.Timeout(5.0)


def _native_frame(**extra: Any) -> dict:
    """native `/api/chat` 的响应帧（字段名与真实 0.24.0 响应一致）。"""
    frame = {
        "model": "qwen2.5:3b",
        "created_at": "2026-09-19T00:00:00Z",
        "message": {"role": "assistant", "content": "ok"},
        "done": True,
        "done_reason": "stop",
        "total_duration": 1,
        "load_duration": 1,
        "prompt_eval_count": 7,
        "prompt_eval_duration": 1,
        "eval_count": 3,
        "eval_duration": 1,
    }
    frame.update(extra)
    return frame


def _openai_body() -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen2.5:3b",
        "choices": [
            {"index": 0, "finish_reason": "stop",
             "message": {"role": "assistant", "content": "ok"}},
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


class _CapturingServer:
    """双协议捕获服务器：记录 (path, body)，并按路径回一个合法响应。

    **刻意同时支持两种协议**：本模块要断言的是"Elysia 用了哪个协议"，而不是
    "哪个协议能跑通"。真实 Ollama 0.24.0 两个端点都能跑，但只有 native 认
    `num_ctx`——所以断言里 **path 与 body 必须一起看**：只断言 body 会让
    "在 compat 端点上加 options"（真实 daemon 上无效）假绿。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    # ---- httpx.MockTransport handler ----
    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        self.calls.append((request.url.path, body))
        if request.url.path.endswith("/api/chat"):
            return self._native(body)
        return self._openai_compat(body)

    # ---- 响应构造 ----
    def _native(self, body: dict) -> httpx.Response:
        if body.get("stream"):
            first = _native_frame(**{"done": False, "done_reason": None,
                                     "prompt_eval_count": None, "eval_count": None})
            lines = json.dumps(first) + "\n" + json.dumps(_native_frame()) + "\n"
            return httpx.Response(200, text=lines,
                                  headers={"content-type": "application/x-ndjson"})
        return httpx.Response(200, json=_native_frame())

    def _openai_compat(self, body: dict) -> httpx.Response:
        if body.get("stream"):
            chunk = {
                "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1,
                "model": "qwen2.5:3b",
                "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
            }
            usage = {
                "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1,
                "model": "qwen2.5:3b", "choices": [],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }
            text = (f"data: {json.dumps(chunk)}\n\n"
                    f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n")
            return httpx.Response(200, text=text,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_openai_body())

    # ---- 断言辅助 ----
    @property
    def last(self) -> tuple[str, dict]:
        assert self.calls, "没有捕获到任何出站请求"
        return self.calls[-1]

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


def _client(server: _CapturingServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server.handler),
                             timeout=_TIMEOUT)


def _ollama(server: _CapturingServer, **kwargs: Any):
    """经**真实工厂**创建 ollama 后端（不直接 import 任何 ollama 专用类）。"""
    return LLMFactory.create("ollama/qwen2.5:3b",
                             http_client=_client(server), **kwargs)


def _num_ctx(body: dict) -> Any:
    """出站请求体里的运行时 context 分配请求（没有则 None）。"""
    options = body.get("options")
    if not isinstance(options, dict):
        return None
    return options.get("num_ctx")


# ============================================================
# 1. 声明值必须落到 wire 上（PRE_FIX_RED）
# ============================================================

class TestConfiguredContextReachesTheWire:
    @pytest.mark.asyncio
    async def test_configured_context_is_requested_on_the_native_wire(self):
        """OL-T1：`contextWindow=8192` ⇒ 出站请求必须显式请求 `num_ctx=8192`。

        修复前实测：出站路径 `/v1/chat/completions`，请求体**没有** `options`
        （KB-13 的可执行复现）。
        """
        declared = LLMFactory.context_window("ollama/qwen2.5:3b")
        assert declared, "providers.yaml 必须为 ollama 声明 context_window（本用例的前提）"
        server = _CapturingServer()
        backend = _ollama(server)
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        path, body = server.last
        assert path.endswith("/api/chat"), (
            f"Ollama 的运行时分配只能在 native 端点表达；实测出站路径 = {path}。"
            "（Ollama 0.24.0 的 /v1/chat/completions 会**静默忽略** options.num_ctx）")
        assert _num_ctx(body) == declared, (
            f"声明窗口 {declared} 必须成为运行时分配请求；实测请求体 options="
            f"{body.get('options')!r}")

    @pytest.mark.asyncio
    async def test_downshift_is_honored_not_hardcoded(self, monkeypatch):
        """OL-T3：声明 4096 ⇒ wire 请求 4096（证明不是硬编码 8192）。"""
        from Module.llm import registry

        monkeypatch.setitem(registry._BACKENDS["ollama"], "context_window", 4096)  # noqa: SLF001
        server = _CapturingServer()
        backend = _ollama(server)
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        _path, body = server.last
        assert _num_ctx(body) == 4096, f"实测 options={body.get('options')!r}"

    @pytest.mark.asyncio
    async def test_every_generation_entry_point_declares_the_context(self):
        """三条生成入口（chat / chat_stream / stream_think）都必须带上它。"""
        server = _CapturingServer()
        backend = _ollama(server)
        declared = LLMFactory.context_window("ollama/qwen2.5:3b")
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
            async for _ in backend.chat_stream([{"role": "user", "content": "hi"}]):
                pass
            async for _ in backend.stream_think([{"role": "user", "content": "hi"}]):
                pass
        finally:
            await backend.close()

        assert len(server.calls) == 3, server.paths
        for path, body in server.calls:
            assert path.endswith("/api/chat"), path
            assert _num_ctx(body) == declared, (path, body.get("options"))


# ============================================================
# 2. 回退/非法值绝不成为运行时分配（OL2；M2/M7 的杀手）
# ============================================================

class TestGenericFallbackIsNeverAllocated:
    @pytest.mark.asyncio
    async def test_missing_context_is_not_promoted_to_generic_fallback(self, monkeypatch):
        """OL-T2 / OL2：未声明 context_window ⇒ **不发** `num_ctx`。

        尤其不许把预算兼容回退（`DEFAULT_CONTEXT_WINDOW = 32768`）当分配指令
        ——那是"预算别撑爆"，不是"给我 32K 显存"（本机实测 32768 → 4.28GB VRAM）。
        """
        from Module.llm import registry
        from Module.system_prompt.budget import DEFAULT_CONTEXT_WINDOW

        monkeypatch.delitem(registry._BACKENDS["ollama"], "context_window")  # noqa: SLF001
        server = _CapturingServer()
        backend = _ollama(server)
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        _path, body = server.last
        assert _num_ctx(body) is None, (
            f"未声明窗口时不得请求任何运行时分配；实测 options={body.get('options')!r}")
        assert str(DEFAULT_CONTEXT_WINDOW) not in json.dumps(body), (
            "通用预算回退值泄漏进了出站请求")

    @pytest.mark.parametrize("bad", [0, -1, -8192])
    @pytest.mark.asyncio
    async def test_zero_or_negative_context_is_never_sent(self, monkeypatch, bad):
        """§40：0/负数**禁止**作为 `num_ctx` 发出。

        实测（Ollama 0.24.0）：`num_ctx=0` 与 `num_ctx=-1` 都返回 **HTTP 200**，
        但 `/api/ps context_length = 4`——一个 4 token 的畸形 runner。所以宁可
        "不表态"（交给 daemon 默认），也绝不把这个数字发出去。
        """
        from Module.llm import registry

        monkeypatch.setitem(registry._BACKENDS["ollama"], "context_window", bad)  # noqa: SLF001
        server = _CapturingServer()
        backend = _ollama(server)
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        _path, body = server.last
        assert _num_ctx(body) is None, f"非法值 {bad} 被发出：{body.get('options')!r}"


class TestDeclaredValueCoercion:
    """同一个声明值有**两个**消费方（预算 ×0.75 / wire `num_ctx`），必须同解析、同结论。

    Challenger（2026-09-19）实测的新失败模式：预算侧 `int(window)` 而运行时侧拿原样值，
    于是手写成 `context_window: "8192"`（带引号的 YAML）时预算照算 6144、而**每一次
    ollama 生成**都在发请求前抛 `TypeError: '>' not supported between 'str' and 'int'`
    ——一个书写差异把 provider 打成 100% 不可用。下面把两侧的结论逐形态钉在一起。
    """

    @pytest.mark.parametrize("raw, expected", [
        (8192, 8192),
        ("8192", 8192),      # 手写 YAML 的常见形态（带引号）
        (" 8192 ", 8192),
        (8192.0, 8192),
        (0, None),
        (-1, None),
        (0.5, None),         # int() 截断后 ≤ 0 ⇒ 与"未声明"同视
        (True, None),        # bool 是 int 子类：int(True)=1 会把预算退化成 1 token
        (False, None),
        ("not-a-number", None),
        ("", None),
        (None, None),
        ([], None),
        ({"window": 8192}, None),
    ])
    @pytest.mark.asyncio
    async def test_both_sides_agree_on_every_value_shape(self, monkeypatch, raw, expected):
        from Module.llm import registry

        monkeypatch.setitem(registry._BACKENDS["ollama"], "context_window", raw)  # noqa: SLF001
        server = _CapturingServer()
        backend = _ollama(server)
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        budget_authority = LLMFactory.context_window("ollama/qwen2.5:3b")
        assert budget_authority == expected, (
            f"预算侧解析 {raw!r} 得 {budget_authority}（期望 {expected}）")
        assert backend.configured_context == expected
        assert _num_ctx(server.last[1]) == expected, (
            f"运行时侧对 {raw!r} 得 {_num_ctx(server.last[1])}（期望 {expected}）")

    @pytest.mark.asyncio
    async def test_explicit_create_override_is_not_a_duplicate_kwarg(self):
        """Challenger A4：`create(..., context_window=N)` 不得抛重复关键字 TypeError。

        它按既有 `base_url`/`api_key` 的覆盖模式生效（无生产调用方；测试/内部用），
        且同样过一遍解析规则。
        """
        server = _CapturingServer()
        backend = LLMFactory.create("ollama/qwen2.5:3b", context_window=4096,
                                    http_client=_client(server))
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()
        assert backend.requested_context == 4096
        assert _num_ctx(server.last[1]) == 4096


# ============================================================
# 3. 非 Ollama 零污染（OL6；M3/M4 的杀手）
# ============================================================

class TestNonOllamaIsolation:
    @pytest.mark.asyncio
    async def test_openai_payload_is_byte_free_of_ollama_fields(self):
        """OL-T5 / OL6：OpenAI 兼容 provider 的请求体**逐字段**不含 Ollama 语义。"""
        server = _CapturingServer()
        backend = LLMFactory.create("openai/gpt-4o", api_key="sk-test",
                                    http_client=_client(server))
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        path, body = server.last
        assert path.endswith("/chat/completions"), path
        assert set(body) == {"model", "messages", "stream"}, (
            f"OpenAI 兼容路径的请求体字段集变了：{sorted(body)}")
        for forbidden in ("options", "num_ctx", "keep_alive", "options.num_ctx"):
            assert forbidden not in body, f"Ollama 字段污染了通用 backend：{forbidden}"

    @pytest.mark.asyncio
    async def test_deepseek_payload_is_byte_free_of_ollama_fields(self):
        """DeepSeek（chat 路径带 extra_body 与 stream_options）同样零污染。"""
        server = _CapturingServer()
        backend = LLMFactory.create("deepseek/deepseek-v4-flash", api_key="sk-test",
                                    http_client=_client(server))
        try:
            async for _ in backend.stream_think(
                    [{"role": "user", "content": "hi"}],
                    extra_body={"thinking": {"type": "disabled"}}):
                pass
        finally:
            await backend.close()

        path, body = server.last
        assert path.endswith("/chat/completions"), path
        assert set(body) == {"model", "messages", "stream", "stream_options", "thinking"}, (
            f"DeepSeek 请求体字段集变了：{sorted(body)}")


# ============================================================
# 4. 后端选择（canonical provider identity，不是 URL/模型名启发式）
#    与 OL1 的**结构面**：预算来源与运行时请求同源
# ============================================================

class TestBackendSelection:
    @pytest.mark.asyncio
    async def test_ollama_provider_routes_to_the_native_backend(self):
        """OL-T4a：canonical provider = ollama ⇒ native 后端（与 base_url 长什么样无关）。"""
        server = _CapturingServer()
        backend = LLMFactory.create("ollama/qwen2.5:3b",
                                    base_url="http://127.0.0.1:11434/v1",
                                    http_client=_client(server))
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()
        assert type(backend).__name__ == "OllamaBackend"
        assert server.last[0].endswith("/api/chat")

    @pytest.mark.asyncio
    async def test_an_ollama_looking_url_does_not_change_the_protocol(self):
        """OL-T4b / §29/§30：**只看 canonical provider**。

        一个指向 `localhost:11434` 的 OpenAI 兼容 provider（这里是 mimo 的条目 + 显式
        base_url）**不得**被 URL 启发式劫持成 native 语义——它按声明就是
        OpenAI-compatible，走的仍是 `/chat/completions`。
        """
        server = _CapturingServer()
        backend = LLMFactory.create("mimo/mimo-v2.5", api_key="k",
                                    base_url="http://localhost:11434/v1",
                                    http_client=_client(server))
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()
        assert type(backend).__name__ == "OpenAICompatBackend"
        path, body = server.last
        assert path.endswith("/chat/completions"), path
        assert "options" not in body


class TestOneAuthorityForBudgetAndRuntime:
    @pytest.mark.asyncio
    async def test_declared_window_is_the_single_source_for_both_sides(self):
        """**OL1 的结构面**：预算读的 `LLMFactory.context_window()` 与后端请求的值
        必须来自同一个 `_BACKENDS` 条目——不是"两次调用同一个函数"，而是同一个值。
        """
        from Module.llm import registry

        for provider in registry._BACKENDS:  # noqa: SLF001
            declared = LLMFactory.context_window(provider)
            if provider != "ollama":
                if not LLMFactory.is_implemented(provider) or provider == "custom":
                    # gemini 仍未实现（本批不动它）；custom 的 create 带 SSRF 守卫
                    # （解析式判据，离线/无 DNS 环境下会误红）——它的构造路径另有覆盖。
                    continue
                # 其余 provider **不能**显式申请分配 ⇒ 它们的 backend 上不存在
                # Ollama 的运行时语义属性（OL3：generic backend 看不到 num_ctx 概念）。
                # 只构造、不触网（内置 provider 不做端点校验）。
                other = LLMFactory.create(f"{provider}/probe-model",
                                          base_url="https://example.com/v1",
                                          api_key="k")
                try:
                    assert not hasattr(other, "requested_context"), provider
                    assert not hasattr(other, "probe_runtime_context"), provider
                finally:
                    await other.close()
                continue
            server = _CapturingServer()
            backend = _ollama(server)
            try:
                await backend.chat([{"role": "user", "content": "hi"}])
            finally:
                await backend.close()
            assert backend.configured_context == declared
            assert _num_ctx(server.last[1]) == declared

    @pytest.mark.asyncio
    async def test_missing_declaration_yields_no_allocation_on_both_sides(self, monkeypatch):
        """OL2 的反面：未声明时预算侧走兼容回退（32K），运行时侧**不表态**——两者不混。"""
        from Module.llm import registry
        from Module.system_prompt.budget import DEFAULT_CONTEXT_WINDOW

        monkeypatch.delitem(registry._BACKENDS["ollama"], "context_window")  # noqa: SLF001
        server = _CapturingServer()
        backend = _ollama(server)
        try:
            await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()

        assert LLMFactory.context_window("ollama/qwen2.5:3b") is None
        assert backend.configured_context is None
        assert _num_ctx(server.last[1]) is None
        assert DEFAULT_CONTEXT_WINDOW > 0  # 预算侧的兼容回退仍存在（只是不外发）

