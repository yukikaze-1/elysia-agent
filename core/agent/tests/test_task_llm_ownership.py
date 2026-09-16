# 测试文件：core/agent/tests/test_task_llm_ownership.py
"""F1（Batch 1.6）：工作流/任务系统的 LLM 资源**所有权**。

## 缺陷（Batch 1.5 已确认，本批修复）

`Module/workflow_contract/infra.py::AgentInfra.llm` 此前拿的是**主 LLM 适配器的
raw client**（`LLMAdapter.get_raw_client()`，唯一生产构造点
`core/agent/_lifecycle_task_system.py::_start_task_system`）。而
`LLMAdapter.switch_model()` 会把**换下来的旧 backend 在后台 close 掉**
（`core/adapter/llm_adapter.py`：`self._closing_backends.try_spawn(close_meth())`）。
于是热切换主模型之后：

    AgentInfra.llm → 已关闭的 AsyncOpenAI

后台任务的任何 LLM 调用都抛
`APIConnectionError ← RuntimeError: Cannot send a request, as the client has been closed.`

这不是抽象味道，是**资源生命周期所有权错误**：A 创建/关闭，B 长期持有引用。
Batch 1.5 的独立探针实测 `captured.closed` 由 `False` 翻成 `True`
（见 `docs/review/2026-09-16-Batch1.5-config-truthfulness-执行记录.md` §2 F1）。

## 本文件钉住的不变量

> **工作流使用的 LLM 资源，在它自己的生命周期内恒有效。**

修法是方案 A：工作流**拥有**自己的 task LLM backend——
`_start_task_system` 创建、`_stop_task_system` 关闭；主 LLM 的热切换不碰它
（它关的只是自己换下来的那个 backend）。

## 关于替身的一处诚实说明

出站请求走真 `openai.AsyncOpenAI` + `httpx.MockTransport`（离线、确定性）。
`MockTransport` 下**在途**请求不会因为 `aclose()` 而中断（没有真实 socket 可切），
所以 Test F1-5 钉的是"切换期间不被取消 + 切换后那个 client 仍可用"，
而不是"模拟真实网络中断"。真正把"client 已被关闭"暴露出来的是**切换之后**的调用。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from Module.llm import LLMFactory

pytestmark = pytest.mark.service

_TASK_MODEL_ENV = "TASK_SYSTEM_MODEL"
"""任务系统模型名的唯一 env 来源（`Module/workflow_contract/infra.py`）。"""

_TASK_MODEL = "deepseek-v4-pro"
"""env 未设时的回退值——Batch 1.5 已分类为 intentional external env-only config。"""


# ═══════════════════════════════════════════════════════════════════════════════
# 测试装置：agent 替身 + 离线出站 + 后端工厂观测
# ═══════════════════════════════════════════════════════════════════════════════


def _host_agent(llm: Any) -> Any:
    """`object.__new__(Agent)` 真身替身——只补 `_start_task_system` 需要的槽位。

    与 `Module/workflow_contract/tests/test_task_system_model.py::_host_agent` 同构
    （刻意不共用：那个 helper 属于契约层测试，本文件测的是 core 侧的所有权）。
    刻意不用 `MagicMock()` 当 host：`_bg_result_repo()` / `_wal()` 走
    `getattr(self, "_…", None)` 兜底，真身上"没接线"就是 None（本用例要的语义）。
    """
    from core.agent.agent import Agent

    host = object.__new__(Agent)
    host._llm = llm
    host._tool_adapter = SimpleNamespace(tool_context=SimpleNamespace(services={}))
    host._bus = MagicMock()
    host._cognitive = MagicMock()
    host._user_name = "测试用户"
    host._task_system = None
    host._task_llm_backend = None      # `Agent.__init__` 的初值（agent.py）
    host._delegation_preparer = None
    return host


def _completion_body(model: str) -> dict[str, Any]:
    """OpenAI 形状的 chat.completion 响应体（非流式）。"""
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _OfflineHttp:
    """每个 backend 一份**独立**的 `httpx.MockTransport` 工厂 + 出站记录。

    必须是**工厂**而不是单个 client：`AsyncOpenAI.close()` 会连带关闭注入的
    `http_client`，两个 backend 共用一个实例时"关主 LLM"会顺手关掉工作流的传输层
    ——那会让本文件测不出真正的缺陷（假红）。工厂保证每个 backend 拿到全新 client。

    也必须是**每 backend 一份**记录器：本文件要判定"这一发请求走的是哪个 backend"。
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._gate: asyncio.Event | None = None

    def block_until_released(self) -> asyncio.Event:
        """让下一次（及之后）请求阻塞，直到返回的 Event 被 set——用于在途用例。"""
        self._gate = asyncio.Event()
        return self._gate

    def factory(self) -> httpx.AsyncClient:
        async def _handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            model = body.get("model", "")
            self.requests.append({"url": str(request.url), "model": model})
            if self._gate is not None:
                await self._gate.wait()
            return httpx.Response(200, json=_completion_body(model))

        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


class _CountingBackend:
    """真 backend 的薄包装——只为**精确计数** `close()` 调用次数。

    `raw_client` 原样透传（工作流拿到的仍是真 `AsyncOpenAI`），
    于是"关了几次"可判定，而"关掉之后还能不能发请求"仍由真对象回答。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.close_calls = 0

    @property
    def raw_client(self) -> Any:
        return self._inner.raw_client

    async def close(self) -> None:
        self.close_calls += 1
        await self._inner.close()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _LlmHarness:
    """backend 工厂观测器：每个 backend 一份离线出站 + 一份 `close()` 计数。"""

    def __init__(self) -> None:
        self.recorders: dict[str, _OfflineHttp] = {}
        self.backends: list[_CountingBackend] = []
        self._by_backend: dict[int, _OfflineHttp] = {}

    def recorder_for(self, client: Any) -> _OfflineHttp:
        """按 raw client 的**对象同一性**找出服务它的记录器（不依赖 model_id 拼法）。"""
        for backend in self.backends:
            if backend.raw_client is client:
                return self._by_backend[id(backend)]
        raise AssertionError("该 client 不属于任何由工厂创建的 backend")

    def backend_for(self, client: Any) -> _CountingBackend:
        """按 raw client 的**对象同一性**定位 backend（不依赖 model_id 拼法）。"""
        matches = [b for b in self.backends if b.raw_client is client]
        assert len(matches) == 1, (
            f"应当**恰好一个** backend 的 raw_client 是该对象，实测 {len(matches)} 个")
        return matches[0]


def _patch_llm_factory(monkeypatch: pytest.MonkeyPatch) -> _LlmHarness:
    """让**所有** backend（主 LLM 的创建/热切换 + 工作流的 task LLM）走离线出站。"""
    harness = _LlmHarness()
    original = LLMFactory.create

    def _create(model_id: str, **kwargs: Any) -> Any:
        rec = _OfflineHttp()
        harness.recorders[model_id] = rec
        kwargs.setdefault("http_client_factory", rec.factory)
        backend = _CountingBackend(original(model_id, **kwargs))
        harness.backends.append(backend)
        harness._by_backend[id(backend)] = rec   # noqa: SLF001 —— 同类内部登记
        return backend

    monkeypatch.setattr(LLMFactory, "create", _create)
    return harness


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个 provider 的假 key + 清掉 `TASK_SYSTEM_MODEL`（走回退值）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.delenv(_TASK_MODEL_ENV, raising=False)


async def _wait_closed(client: Any, *, timeout: float = 3.0) -> bool:
    """等 `is_closed()` 为真（热切换的"后台关旧后端"是异步的）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.is_closed():
            return True
        await asyncio.sleep(0.01)
    return bool(client.is_closed())


async def _ping(client: Any, model: str) -> Any:
    """经给定 raw client 发一次最小请求（真 SDK → MockTransport）。"""
    return await client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "hi"}])


# ═══════════════════════════════════════════════════════════════════════════════
# Test F1-1：主模型热切换后，工作流仍能正常发请求
# ═══════════════════════════════════════════════════════════════════════════════


class TestHotSwitchDoesNotInvalidateTaskLlm:
    @pytest.mark.asyncio
    async def test_task_llm_survives_main_model_hot_switch(self, monkeypatch):
        """F1-1：切换主模型后，工作流的 client **未被关闭**，且最小调用真的发得出去。"""
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()          # noqa: SLF001
            infra = host._task_system._infra         # noqa: SLF001
            task_client = infra.llm
            assert task_client is not None, "工作流必须拿到一个可用的 raw client"
            assert not task_client.is_closed()

            old_main = llm.get_raw_client()
            llm.switch_model("openai/gpt-4o")
            assert await _wait_closed(old_main), (
                "前提：热切换确实关掉了主 LLM 换下来的那个 client")
            assert llm.get_raw_client() is not old_main, "前提：主 LLM 确实换了 client"

            assert not task_client.is_closed(), (
                "工作流的 LLM 资源不得被主 LLM 的热切换关闭——"
                "它是工作流自己的资源，不是主 LLM 借出的")
            # 最小真实调用：走真 AsyncOpenAI → MockTransport
            await _ping(task_client, infra.llm_model)
            assert harness.recorder_for(task_client).requests[-1]["model"] == _TASK_MODEL
        finally:
            await host._stop_task_system()           # noqa: SLF001
            await llm.close()

    @pytest.mark.asyncio
    async def test_inflight_task_request_is_not_cancelled_by_the_switch(self, monkeypatch):
        """F1-5：切换**发生在请求在途时**——在途请求不被取消，其后同一 client 仍可用。

        `MockTransport` 下 `aclose()` 不会中断已在途的请求（没有真实 socket 可切），
        故本用例的判别力在**后半句**：切换之后同一个 client 还要能再发一发——
        修复前它是"已关闭"的，第二发必抛。
        """
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()          # noqa: SLF001
            infra = host._task_system._infra         # noqa: SLF001
            task_client = infra.llm
            task_recorder = harness.recorder_for(task_client)
            gate = task_recorder.block_until_released()

            inflight = asyncio.create_task(_ping(task_client, infra.llm_model))
            # 等请求真的进到 transport（被 gate 拦住），否则"在途"只是名义上的
            while not task_recorder.requests:
                await asyncio.sleep(0)

            old_main = llm.get_raw_client()
            llm.switch_model("openai/gpt-4o")
            assert await _wait_closed(old_main)
            assert not inflight.done(), "在途请求不得被主模型切换取消"

            gate.set()
            await inflight
            assert not inflight.cancelled()

            await _ping(task_client, infra.llm_model)   # 切换后仍可用
            assert len(task_recorder.requests) == 2
        finally:
            await host._stop_task_system()           # noqa: SLF001
            await llm.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Test F1-2：任务模型与主模型互相独立
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskModelIsIndependent:
    @pytest.mark.asyncio
    async def test_task_model_stays_put_across_main_model_switch(self, monkeypatch):
        """F1-2：main = A、task = B，主模型切到 C —— 工作流保持 B，且调用仍成功。"""
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")     # main = A
        host = _host_agent(llm)
        try:
            await host._start_task_system()                # noqa: SLF001
            infra = host._task_system._infra               # noqa: SLF001
            task_client, task_model = infra.llm, infra.llm_model
            assert task_model == _TASK_MODEL               # task = B（与 A 无关）
            assert task_model != llm.model

            old_main = llm.get_raw_client()
            llm.switch_model("openai/gpt-4o")              # main → C
            assert await _wait_closed(old_main)

            assert infra.llm_model == _TASK_MODEL, "任务系统的模型名不随主模型热切换变化"
            assert infra.llm is task_client, "任务系统的 client 对象也不该被换掉"
            await _ping(infra.llm, infra.llm_model)
            assert harness.recorder_for(task_client).requests[-1]["model"] == _TASK_MODEL
        finally:
            await host._stop_task_system()                 # noqa: SLF001
            await llm.close()

    @pytest.mark.asyncio
    async def test_preparer_shares_the_engine_task_llm(self, monkeypatch):
        """委派器与引擎用**同一个** task LLM（单一来源，不是各拿一份）。"""
        _env(monkeypatch)
        _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()                # noqa: SLF001
            infra = host._task_system._infra               # noqa: SLF001
            preparer = host._delegation_preparer           # noqa: SLF001
            assert preparer is not None
            assert preparer._client is infra.llm           # noqa: SLF001
            assert preparer._model == infra.llm_model      # noqa: SLF001
        finally:
            await host._stop_task_system()                 # noqa: SLF001
            await llm.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Test F1-3 / F1-4：停—启与关闭次数
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskSystemLifecycleOwnsItsLlm:
    @pytest.mark.asyncio
    async def test_stop_closes_the_task_llm_and_start_rebuilds_it(self, monkeypatch):
        """F1-3：`start → 可用 → stop → start → 可用`，且**不复用**已关闭的 client。"""
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()                # noqa: SLF001
            first = host._task_system._infra.llm           # noqa: SLF001
            first_backend = harness.backend_for(first)

            await host._stop_task_system()                 # noqa: SLF001
            assert first_backend.close_calls == 1, "停机必须关掉工作流自己的 LLM"
            assert await _wait_closed(first)

            # 同一实例重建（stop → start，不经 /restart 的整只新 Agent）
            await host._start_task_system()                # noqa: SLF001
            second = host._task_system._infra.llm          # noqa: SLF001
            assert second is not first, "重启必须新建 client，不得复用已关闭的那个"
            assert not second.is_closed()
            await _ping(second, host._task_system._infra.llm_model)  # noqa: SLF001
        finally:
            await host._stop_task_system()                 # noqa: SLF001
            await llm.close()

    @pytest.mark.asyncio
    async def test_task_llm_is_closed_exactly_once(self, monkeypatch):
        """F1-4：`_stop_task_system` 幂等——task LLM 恰好被关一次，无双重关闭。"""
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()                # noqa: SLF001
            task_backend = harness.backend_for(host._task_system._infra.llm)  # noqa: SLF001

            await host._stop_task_system()                 # noqa: SLF001
            await host._stop_task_system()                 # noqa: SLF001 第二次必须安全

            assert task_backend.close_calls == 1, (
                f"task LLM 只能被关一次，实测 {task_backend.close_calls} 次")
        finally:
            await llm.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Test F1-4b（Challenger H1）：`_stop_task_system` 的**每一个**出口都要关掉 task LLM
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskLlmIsClosedOnEveryStopExit:
    """关闭语句不能只挂在"引擎正常停下"这一条路径上。

    缺陷形态（由 Challenger 实测发现，修复前）：关闭排在
    `await wait_for(self._task_system.stop(), …)` **之后**，而 `except` 只覆盖
    `asyncio.TimeoutError`。`Agent.stop()` 按 CR-02 逐步吞掉每个步骤的
    `BaseException` 继续往下走 ⇒ 只要 `WorkflowEngine.stop()` 抛出别的东西
    （`RuntimeError` / `CancelledError`——后者正是"用户再按一次 Ctrl+C"的形态），
    关闭语句被整段跳过：backend 泄漏、`_task_llm_backend` 还留在身上，下一次
    `start()` 直接覆盖引用 ⇒ **永久泄漏**。修复前工作流不拥有任何 LLM 资源，
    所以这个洞是本批 F1 **新引入**的。

    修法：先取走并置 None（所有权当场转移），关闭放 `finally`。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("engine stop blew up"), ValueError("engine stop blew up"),
         asyncio.CancelledError()],
        ids=["RuntimeError", "ValueError", "CancelledError"],
    )
    async def test_close_runs_when_engine_stop_raises(self, monkeypatch, exc):
        """引擎 stop() 抛非 TimeoutError → task LLM 仍被关闭，且槽位已清空。"""
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()          # noqa: SLF001
            task_backend = harness.backend_for(host._task_system._infra.llm)  # noqa: SLF001

            async def _boom() -> None:
                raise exc

            monkeypatch.setattr(host._task_system, "stop", _boom)  # noqa: SLF001
            with pytest.raises(BaseException):
                await host._stop_task_system()       # noqa: SLF001

            assert task_backend.close_calls == 1, (
                f"{type(exc).__name__} 出口下 task LLM 必须仍被关闭（恰好一次）")
            assert host._task_llm_backend is None, (  # noqa: SLF001
                "槽位必须先清空——否则下一次 start() 覆盖引用即永久泄漏")
        finally:
            await llm.close()

    @pytest.mark.asyncio
    async def test_close_runs_when_the_engine_never_started(self, monkeypatch):
        """引擎对象缺席（未启动/已置空）时也不该漏关——所有权在本方法，不在引擎。"""
        _env(monkeypatch)
        harness = _patch_llm_factory(monkeypatch)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()          # noqa: SLF001
            task_backend = harness.backend_for(host._task_system._infra.llm)  # noqa: SLF001
            host._task_system = None                 # noqa: SLF001

            await host._stop_task_system()           # noqa: SLF001

            assert task_backend.close_calls == 1
            assert host._task_llm_backend is None    # noqa: SLF001
        finally:
            await llm.close()
