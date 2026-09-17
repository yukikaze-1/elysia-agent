# 测试文件：core/cognitive/tests/test_delegation_provider_agnostic.py
"""Issue C：`DelegationPreparer` 不得自行假定某个具体 provider 必须存在。

## 缺陷（修复前，实测）

`core/cognitive/delegation.py::DelegationPreparer.__init__` 读
`os.environ["DEEPSEEK_API_KEY"]`，缺失即 `raise ValueError`，再自建
`AsyncOpenAI(base_url="https://api.deepseek.com")`。而它由
`core/agent/_lifecycle_task_system.py::_start_task_system` 构造，该方法是
`AgentLifecycle._STARTUP_STEPS` 的一步（`core/agent/lifecycle.py:123`）——
启动步骤失败会 re-raise（`lifecycle.py:238-240`）并把 Agent 回滚到 STOPPED
（`:243-255`）。于是**一个可选的后台工作流子系统的 provider 假设**被升级成
**整个应用启动的硬前置**：主 Agent 配 OpenAI / Ollama / MiMo / custom
（无 `DEEPSEEK_API_KEY`）时根本起不来。

## 结论：Case 2 —— 它确实需要 LLM 能力，而该能力应用早已持有并可注入

`_start_task_system` 在构造 preparer **之前**（`_lifecycle_task_system.py:182`）
就已经建好 `AgentInfra(llm=self._llm.get_raw_client(), …, llm_model=…)`；engine 的
Planner/Executor/Evaluator 正是用
`infra.llm.chat.completions.create(model=infra.llm_model)` 调模型
（`Module/workflow_engine/core/planner/parser.py:57`、
`core/executor.py:391`）。preparer 调的是**同一个形状**的接口，所以正确做法是
复用这个既有注入点，而不是 `os.environ` + 自建 client。

**Case 1 被排除**（不是"删掉守卫即可"）：本仓 openai SDK（2.53.0）在 api_key 为空
且解析不到 `OPENAI_API_KEY` 时 `raise OpenAIError("Missing credentials…")`——
删掉手写 raise 只会把崩溃点挪到下一行；而若 `OPENAI_API_KEY` **已设**，SDK 会
用 OpenAI 的 key 去连 deepseek 端点（静默错路由 + 凭据外发）。

## 这些用例钉住的不变式

1. 启动步骤不读 provider 环境变量、不因缺 key 抛异常（C1）；
2. **工作流的 task LLM 建不起来**时**显式降级**为"工作流委派不可用"（既有表示：
   `_delegation_preparer is None` → `submit_task` 不注册 / 显式失败文案），
   启动不崩（C2）；
3. DeepSeek 配置下路径不变（C3）；
4. stop→start 后拿到**重建的** task LLM，不复活已关闭的旧 client（C4）；
5. 工作流关停只关**自己拥有的** task LLM，不碰主 LLM 的 client（C5）。

## Batch 1.6 / F1 对 C2 的改写（如实说明）

修复前 C2 的判据是"**主 LLM 运行时交不出可复用 raw client**（`get_raw_client()`
返回 None）→ 委派不可用"。F1 之后工作流**不再借**主运行时的 client——它自己建一个
task LLM（`_build_task_llm_backend`），所以那个判据在结构上不再存在：可用性问题变成
"**工作流自己的 task LLM 能不能建起来**"。本文件因此：

- `test_main_backend_without_raw_client_no_longer_gates_the_workflow`（新增）——
  钉住"主后端的形状不再是工作流可用性的前提"这一语义变化；
- `test_task_llm_construction_failure_degrades_instead_of_crashing`（改写自原 C2）——
  用**真实的**降级触发点（构造函数抛错）验证降级路径本身仍然完整。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.workflow_contract import ConstraintConfig, SessionId
from Module.workflow_contract.infra import AgentInfra
from Module.tool.tool_descriptor import Tool
from core.adapter.llm_adapter import LLMAdapter
from core.cognitive.delegation import DelegationPreparer, DelegationUnavailableError
from core.cognitive.tools import CognitiveTools
from core.cognitive.user_profile import UserProfile
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId

# ── 替身 ──────────────────────────────────────────────────────────────────────


class _NoRawClientBackend:
    """`raw_client` 为 None 的 LLM 后端替身。

    复刻"配置的运行时**无法**交出可复用原始 client"这一状态：`LLMBackend.raw_client`
    的契约允许 None（`Module/llm/protocol.py:83`、
    `core/adapter/llm_adapter.py::get_raw_client` 返回 `object | None`）。
    """

    raw_client = None

    async def chat(self, messages, **kwargs):  # pragma: no cover - 本用例不调模型
        return ""

    async def close(self) -> None:
        return None


def _infra(client, model: str | None = None) -> AgentInfra:
    """带注入 client 的 `AgentInfra`（模型名可选覆盖）。"""
    return AgentInfra(
        llm=client,
        milvus=None,
        http=None,
        tool_registry=object(),
        constraints=ConstraintConfig(),
        **({"llm_model": model} if model else {}),
    )


def _submit_task_call(intent: str = "查天气") -> ToolCall:
    """构造一个 submit_task 的 ToolCall（`_handle_submit_task` 的入参）。"""
    return ToolCall(
        id=ToolCallId("call_1"),
        function=ToolCallFunction(name="submit_task", arguments=f'{{"intent": "{intent}"}}'),
    )


def _host_agent(llm) -> object:
    """`object.__new__(Agent)` 真身替身——只补 `_start_task_system` 需要的槽位。

    刻意**不用** `MagicMock()` 当 host：`_bg_result_repo()` / `_wal()` 走
    `getattr(self, "_…", None)` 兜底，真身上"没接线"就是 None（本用例要的语义）；
    而 MagicMock 会给自动属性，把"未接线"变成"接了个 MagicMock"。
    """
    from core.agent.agent import Agent

    host = object.__new__(Agent)
    host._llm = llm
    host._tool_adapter = SimpleNamespace(tool_context=SimpleNamespace(services={}))
    host._bus = MagicMock()
    host._cognitive = MagicMock()
    host._user_name = "测试用户"
    # `Agent.__init__` 的初值（agent.py:540-541）
    host._task_system = None
    host._delegation_preparer = None
    return host


async def _teardown(host) -> None:
    """停引擎 + 关 LLM（避免用例间泄漏后台任务/httpx 连接池）。"""
    await host._stop_task_system()  # noqa: SLF001 — 被测算的关停步骤
    await host._llm.close()


def _fake_completion(payload: str) -> SimpleNamespace:
    """OpenAI 形状的非流式响应替身（choices + usage）。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


_OK_PAYLOAD = (
    '{"status": "ok", "reason": "意图清晰", "tools": ["web_search"],'
    ' "summarized_context": "用户想查天气", "max_dag_nodes": 3, "max_mutations": 0,'
    ' "max_total_tokens": null, "max_task_duration_s": null, "max_failure_count": null}'
)

_TOOLS = [Tool(name="web_search", description="搜索互联网", capabilities=["read"])]


# ═══════════════════════════════════════════════════════════════════════════════
# 单元层：preparer 自身不接受 provider 假设
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestPreparerTakesInjectedCapability:
    """preparer 从既有注入点（`AgentInfra`）取 LLM 能力，而非环境变量。"""

    def _infra(self, client, model: str | None = None) -> AgentInfra:
        return _infra(client, model)

    def test_uses_injected_client_and_model(self):
        """注入的 client / model 被原样采用（不再自建 DeepSeek client）。"""
        client = object()
        dp = DelegationPreparer(self._infra(client, "my-model"))
        assert dp._client is client
        assert dp._model == "my-model"

    def test_injected_client_wins_over_provider_env(self, monkeypatch):
        """即便 DEEPSEEK_API_KEY 也在，也**不得**改用它——provider 由配置决定。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-be-used")
        client = object()
        dp = DelegationPreparer(self._infra(client))
        assert dp._client is client

    def test_no_deepseek_key_is_not_a_precondition(self, monkeypatch):
        """DEEPSEEK_API_KEY 缺席 + 已有注入 client → 构造成功（C1 的最小面）。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        client = object()
        dp = DelegationPreparer(self._infra(client))
        assert dp._client is client
        assert dp._model  # 有默认模型名，不因缺 env 而空

    @pytest.mark.asyncio
    async def test_unconfigured_preparer_is_explicitly_unavailable(self, monkeypatch):
        """没有可复用 client（直接构造、无注入）→ 构造不抛，使用时**显式**不可用。

        这条区分"启动不能崩"（构造）与"真要用时必须说清楚"（prepare）。
        """
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        dp = DelegationPreparer()  # 不抛

        with pytest.raises(DelegationUnavailableError):
            await dp.prepare(
                intent="查天气",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=_TOOLS,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s1"),
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 服务层：真实启动步骤 `_start_task_system`
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.service
class TestStartupStepIsProviderAgnostic:
    """`_start_task_system` 不再把 DEEPSEEK_API_KEY 当作启动前置。"""

    def test_startup_step_still_registered(self):
        """可选/必需关系不变：task_system 仍是 `_STARTUP_STEPS` 的一步。"""
        from core.agent.lifecycle import AgentLifecycle

        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert "task_system" in names

    @pytest.mark.asyncio
    async def test_openai_deployment_starts_without_deepseek_key(self, monkeypatch):
        """C1：配 OpenAI、**无** DEEPSEEK_API_KEY → 启动步骤不抛，且走 OpenAI 端点。

        修复前：`DelegationPreparer()` 抛
        `ValueError: DEEPSEEK_API_KEY 环境变量未设置…` → `Agent.start()` 回滚到 STOPPED。

        F1（Batch 1.6）：工作流用的不再是主适配器的 client，而是**它自己**按主 provider
        建出来的 task LLM——所以断言从"`_client is` 主 client"改成"**独立对象** + 仍走
        OpenAI 端点"。要钉的语义（缺 DeepSeek key 不是前置、不静默路由到别家）不变。
        """
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        llm = LLMAdapter("openai/gpt-4o")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001 — 被测算的启动步骤

            assert host._delegation_preparer is not None, (  # noqa: SLF001
                "配了可用 provider 就该有 preparer——缺 DeepSeek key 不是前置条件"
            )
            raw = host._delegation_preparer._client  # noqa: SLF001
            assert raw is not None
            assert raw is not llm.get_raw_client(), (
                "工作流用的是自己拥有的 task LLM，不是主适配器的 client（F1）")
            assert "openai" in str(raw.base_url)            # type: ignore[attr-defined]
            assert "deepseek" not in str(raw.base_url)      # type: ignore[attr-defined]
            assert host._task_system is not None            # noqa: SLF001
            host._cognitive.configure.assert_called_once()  # noqa: SLF001
        finally:
            await _teardown(host)

    @pytest.mark.asyncio
    async def test_main_backend_without_raw_client_no_longer_gates_the_workflow(self, monkeypatch):
        """F1 语义变化：**主后端的形状**不再是工作流可用性的前提。

        修复前工作流借主适配器的 raw client，于是"主后端不实现 `raw_client`"
        （`get_raw_client()` → None）会让委派不可用。修复后工作流用**自己**按主 provider
        建出来的 task LLM（`_build_task_llm_backend`），不再读那个属性——所以同一场景下
        委派**照常可用**。这不是放宽：可用性判据换成了更准确的那个（"工作流的 task LLM
        能不能建起来"，见下一条用例）。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        monkeypatch.delenv("TASK_SYSTEM_MODEL", raising=False)
        llm = LLMAdapter(_test_backend=_NoRawClientBackend())
        host = _host_agent(llm)
        try:
            assert llm.get_raw_client() is None, "前提：主后端交不出 raw client"
            await host._start_task_system()  # noqa: SLF001

            assert host._delegation_preparer is not None, (  # noqa: SLF001
                "工作流自持 task LLM 之后，主后端的形状不再是它的前提")
            client = host._delegation_preparer._client  # noqa: SLF001
            assert client is not None and client.is_closed() is False
        finally:
            await _teardown(host)

    @pytest.mark.asyncio
    async def test_task_llm_construction_failure_degrades_instead_of_crashing(
            self, monkeypatch, caplog):
        """C2（F1 改写版）：**task LLM 建不起来** → 显式降级，启动不崩。

        降级用的是**既有**表示：`_delegation_preparer is None`（`loop.py` 据此
        不把 submit_task 注册给 LLM；`tools.py` 据此返回显式失败文案）。
        引擎仍照常启动——WorkflowEngine 的启停语义**没有**被改动。

        触发点是真实的：`_build_task_llm_backend` 会在 provider 配置缺欠
        （缺 base_url / 未知 provider / custom 条目解析不出 / runtime != backend）时抛错，
        而 `_start_task_system` 是 `_STARTUP_STEPS` 的一步——**抛出去会回滚整只 Agent**
        （`lifecycle.py` 的 start 异常分支）。本用例把那个抛错注入进来，钉住"它被吞掉
        并降级，而不是升级成启动失败"。
        """
        import logging

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        monkeypatch.delenv("TASK_SYSTEM_MODEL", raising=False)
        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)

        def _boom(task_model_name: str) -> object:
            raise ValueError(f"provider 配置缺欠（模拟；task 模型={task_model_name}）")

        monkeypatch.setattr(type(host), "_build_task_llm_backend", _boom, raising=False)
        try:
            with caplog.at_level(logging.WARNING, logger="core.agent._lifecycle_task_system"):
                await host._start_task_system()  # noqa: SLF001

            assert host._delegation_preparer is None              # noqa: SLF001
            assert host._task_system is not None, "引擎本身仍启动（必需关系不变）"  # noqa: SLF001
            assert host._task_llm_backend is None                 # noqa: SLF001
            warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert warnings, "降级必须留可见痕迹，不能静默"

            # 传播面：真正走到 submit_task 时是**显式**失败文案，不是异常
            from core.cognitive.tools import CognitiveTools
            from core.schema.tool import ToolCall, ToolCallFunction
            from core.schema.types import ToolCallId

            tc = ToolCall(
                id=ToolCallId("call_1"),
                function=ToolCallFunction(
                    name="submit_task", arguments='{"intent": "查天气"}'),
            )
            result = await CognitiveTools._handle_submit_task(host, tc)  # noqa: SLF001
            assert result.success is False
            assert "暂不可用" in (result.error or "")
        finally:
            await _teardown(host)

    @pytest.mark.asyncio
    async def test_deepseek_deployment_still_works(self, monkeypatch):
        """C3：DeepSeek 配置的 happy path 不回退——工作流的 client 真的能跑完一次 prepare。

        走**真实的** `_call_llm_with_usage`（只把最外层 SDK 调用换成 AsyncMock），
        所以钉住的是"工作流自持的 client + `infra.llm_model` 被真正用于请求"。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        monkeypatch.delenv("TASK_SYSTEM_MODEL", raising=False)
        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            dp = host._delegation_preparer  # noqa: SLF001
            assert dp is not None
            raw = dp._client  # noqa: SLF001
            assert raw is not None
            assert "deepseek" in str(raw.base_url)

            raw.chat.completions.create = AsyncMock(  # type: ignore[method-assign]
                return_value=_fake_completion(_OK_PAYLOAD))
            result = await dp.prepare(
                intent="查天气",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=_TOOLS,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s-ok"),
            )

            assert result.status.value == "ok"
            assert [t.name for t in result.context.tools] == ["web_search"]
            assert result.token_usage == {
                "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
            kwargs = raw.chat.completions.create.await_args.kwargs  # type: ignore[attr-defined]
            assert kwargs["model"] == host._task_system._infra.llm_model  # noqa: SLF001
            assert kwargs["messages"][0]["role"] == "system"
        finally:
            await _teardown(host)

    @pytest.mark.asyncio
    async def test_task_system_model_env_still_honoured(self, monkeypatch):
        """`TASK_SYSTEM_MODEL` 覆盖仍生效（模型名来自 infra，而不是另一处硬编码）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        monkeypatch.setenv("TASK_SYSTEM_MODEL", "deepseek-v4-flash")
        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            assert host._delegation_preparer._model == "deepseek-v4-flash"  # noqa: SLF001
        finally:
            await _teardown(host)

    @pytest.mark.asyncio
    async def test_stop_closes_only_the_workflow_owned_client(self, monkeypatch):
        """C5（F1 改写版）：工作流关停**只关自己拥有的** task LLM，不碰主 LLM 的 client。

        修复前本用例钉的是"不得关掉借来的共享 client"（当时 `_stop_task_system` 确实
        什么也不关）。F1 之后所有权反转：工作流自己建 task LLM，于是**两半**都要成立——
        ① 自己的那个**必须**被关（否则每次 restart 泄一个连接池）；
        ② 主适配器的 client **必须**留给它的所有者（`stop_llm_tts` 步骤）关——
           在这里关会掐掉停机序列后续步骤仍在用的连接池。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            main_client = llm.get_raw_client()
            task_client = host._delegation_preparer._client  # noqa: SLF001
            assert task_client is not main_client, "前提：两者是不同对象（F1）"

            await host._stop_task_system()   # noqa: SLF001

            assert task_client.is_closed() is True, "工作流必须关掉自己拥有的 task LLM"  # noqa: SLF001
            assert main_client.is_closed() is False, (  # noqa: SLF001
                "主 LLM 的 client 必须留给它的所有者（stop_llm_tts）关闭")
        finally:
            await host._llm.close()

    @pytest.mark.asyncio
    async def test_restart_rebuilds_the_task_llm(self, monkeypatch):
        """C4（F1 改写版）：stop → start 后拿到**重建的** task LLM，不复用已关闭的旧 client。

        完整 `Agent.start()` 在单测里不可行（要 Mongo/Milvus/角色/WS 全套），这里覆盖
        最贴近的可达缝：`lifecycle.start()` 的实际顺序是
        `_llm.reconnect()`（重建**主** client）→ 走 `_STARTUP_STEPS` 含
        `_start_task_system`；`stop()` 里的 `_stop_task_system` 关掉 task LLM。
        本用例按该顺序驱动，钉住"重启后不把已关闭的 task client 交给引擎"。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            first, first_client = host._delegation_preparer, host._delegation_preparer._client  # noqa: SLF001
            await host._stop_task_system()   # noqa: SLF001
            assert first_client.is_closed() is True, "前提：停机确实关掉了第一个 task LLM"  # noqa: SLF001

            # 模拟 stop() 末段关主 LLM + start() 开头 reconnect（lifecycle.py:217）
            await host._llm.close()
            await host._llm.reconnect()
            await host._start_task_system()  # noqa: SLF001

            second = host._delegation_preparer  # noqa: SLF001
            assert second is not None and second is not first
            assert second._client is not first_client, "不得复用已关闭的旧 client"  # noqa: SLF001
            assert second._client.is_closed() is False            # noqa: SLF001
            assert second._client is not host._llm.get_raw_client()  # noqa: SLF001
        finally:
            await _teardown(host)


# ═══════════════════════════════════════════════════════════════════════════════
# 服务层：工具处理器边界 —— prepare() 的失败不得冒进认知回合
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.service
class TestSubmitTaskHandlerNeverRaises:
    """`_handle_submit_task` 是工具处理器：prepare() 失败必须收口成 `ToolResult`。

    为什么这是 Issue C 的一部分（主 agent 复核 §10「是否只是删掉 guard，把错误
    推迟到更难理解的位置」）：调用链 `_run_submit_tasks`
    （`core/cognitive/tool_phase.py`）是**裸** `await self._handle_submit_task(tc)`，
    没有 try/except。改了构造之后，非 DeepSeek 部署上 client 是**可用**的，于是
    `prepare()` 会真的发请求——而请求体仍带 DeepSeek 专属参数
    （`reasoning_effort` / `extra_body.thinking`）与 `deepseek-v4-pro` 默认模型名，
    provider 报错 → 异常冒进已 spawn 的认知回合，用户只看到"这一轮生成莫名死掉"。
    """

    def _handler_host(self, preparer) -> SimpleNamespace:
        """`_handle_submit_task` 真正会读到的那几个槽位。"""
        return SimpleNamespace(
            _delegation_preparer=preparer,
            _task_system=SimpleNamespace(constraints=ConstraintConfig()),
            _tool_adapter=SimpleNamespace(
                definitions=lambda: [{"function": {"name": "web_search", "description": "搜索"}}]),
            _session=SimpleNamespace(recent_messages=lambda: [], session_id="s1"),
            _user_profile=None,
        )

    @pytest.mark.asyncio
    async def test_provider_error_becomes_tool_result(self):
        """client 在、provider 调用炸（DeepSeek 专属参数被拒）→ 失败结果，不抛异常。

        断言本身即"无异常逃逸"：异常会让本用例 error 而不是 pass。
        """
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("400 Invalid parameter: reasoning_effort"))
        preparer = DelegationPreparer(_infra(client))

        result = await CognitiveTools._handle_submit_task(  # noqa: SLF001
            self._handler_host(preparer), _submit_task_call())

        assert result.success is False
        assert "暂不可用" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unavailable_preparer_becomes_tool_result(self):
        """preparer 在、client 不在（`DelegationUnavailableError` 分支）→ 同样收口。"""
        preparer = DelegationPreparer()

        result = await CognitiveTools._handle_submit_task(  # noqa: SLF001
            self._handler_host(preparer), _submit_task_call())

        assert result.success is False
        assert "不可用" in (result.error or "")
