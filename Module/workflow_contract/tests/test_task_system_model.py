"""Batch 1.5 / Subagent D —— `TASK_SYSTEM_MODEL` / `AgentInfra.llm_model` 的**实测契约**。

## 这些用例钉的是什么

`TASK_SYSTEM_MODEL` 是后台任务系统（Planner / Executor / Evaluator / DelegationPreparer）
所用模型名的**唯一**来源：`Module/workflow_contract/infra.py` 的
`AgentInfra.llm_model` 字段默认从它读，而 `core/agent/_lifecycle_task_system.py`
的**唯一**生产构造点没传 `llm_model=`，于是这个 `default_factory` 必然生效。

实测（`conda run -n Elysia python tmp/batch15/probe_cases.py`，见报告 §Cases）：

| 场景 | `AgentInfra.llm_model` | 到达请求的 `model=` |
|---|---|---|
| env 未设（仓库默认状态） | `"deepseek-v4-pro"` | `"deepseek-v4-pro"` |
| `TASK_SYSTEM_MODEL=deepseek-v4-flash` | `"deepseek-v4-flash"` | `"deepseek-v4-flash"` |
| `TASK_SYSTEM_MODEL=gpt-4o`（client 是 DeepSeek 的） | `"gpt-4o"` | `"gpt-4o"` → provider 400 |

而 `config.yaml::llm.model` 是 `deepseek/deepseek-v4-flash`（**主** Agent 的模型）——
两者**没有任何推导关系**：任务系统恒用 `deepseek-v4-pro`，除非运维在 `.env` 里显式
设置 `TASK_SYSTEM_MODEL`（本仓 `.env` 由 `Module/_common/env.py::ensure_dotenv`
泛化载入，故这个键**技术上可设**，但 `.env.example` 从未列出它——git 历史可证）。

## 钉的是"契约"还是"缺陷"

下面两组分开：

- **不变式**（任何后续设计都必须继续成立）：模型名只有一个来源；它是**裸模型名**
  （不含 `provider/` 前缀，因为它被原样塞进 API 的 `model=`）；跨 provider 的错值
  必须**显式**失败并在两条消费路径上都收口，不得静默。
- **当前契约快照**（主 agent 可能有意改动的行为，改时本文件随之改）：
  env 未设时的回退字面量、`_start_task_system` 不传 `llm_model`、热切换不重新接线。
  这几条的 docstring 里都注明「这是 2026-09-16 的实测现状，改动属行为变更」。

需要 marker：本文件两条线分开——纯契约层用 `unit`，要真装配 `Agent`/`LLMAdapter`
（但不连任何外部服务）的用 `service`。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from Module.workflow_contract import ConstraintConfig, ReturnMode, SessionId, WorkflowId
from Module.workflow_contract.infra import AgentInfra
from Module.tool.tool_descriptor import Tool

_REPO_ROOT = next(
    p for p in Path(__file__).resolve().parents
    if (p / "conftest.py").is_file() and (p / "core").is_dir() and (p / "Module").is_dir()
)
"""仓库根——不猜层级（与 `test_public_surface.py` 同一写法）。"""

_ENV_KEY = "TASK_SYSTEM_MODEL"
_HARDCODED_FALLBACK = "deepseek-v4-pro"
"""env 未设时的回退值。**实测**：`AgentInfra` 的 `default_factory` 字面量。"""

_TOOLS = [Tool(name="web_search", description="搜索互联网", capabilities=["read"])]

_PREPARER_OK = (
    '{"status": "ok", "reason": "ok", "tools": ["web_search"], "summarized_context": "x",'
    ' "max_dag_nodes": 3, "max_mutations": 0, "max_total_tokens": null,'
    ' "max_task_duration_s": null, "max_failure_count": null}'
)


def _infra(client: Any = None, **overrides: Any) -> AgentInfra:
    """`AgentInfra` 的最小可用构造（client 缺省给一个哨兵对象）。"""
    kwargs: dict[str, Any] = {
        "llm": client if client is not None else object(),
        "milvus": None,
        "http": None,
        "tool_registry": object(),
        "constraints": ConstraintConfig(),
    }
    kwargs.update(overrides)
    return AgentInfra(**kwargs)


def _fake_completion(payload: str) -> SimpleNamespace:
    """OpenAI 形状的非流式响应替身（choices + usage）。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _completion_body(model: str, payload: str) -> dict[str, Any]:
    """MockTransport 用的 chat.completion JSON。"""
    return {
        "id": "x", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": payload}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 一、契约层：`llm_model` 的取值面（unit）
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestLlmModelField:
    """`AgentInfra.llm_model` 的取值与形状。"""

    def test_default_when_env_unset_is_the_hardcoded_fallback(self, monkeypatch):
        """env 未设 → 回退到代码里的硬编码字面量。

        **当前契约快照**（实测 2026-09-16）。它同时是"用户的 `config.yaml::llm.model`
        选择对后台任务无效"这一现象的直接来源——该现象已被
        `docs/review/2026-09-16-架构职责分离与分层重构审计.md:446` 记为缺陷，
        修它（让工作流跟随主模型）属**行为变更**，由主 agent 决策。
        若该决策落地，本条应改为断言新契约。
        """
        monkeypatch.delenv(_ENV_KEY, raising=False)
        assert _infra().llm_model == _HARDCODED_FALLBACK

    def test_env_var_overrides_the_default(self, monkeypatch):
        """设了 env → 原样生效。

        本条的用途是**反证"死配置"**：`TASK_SYSTEM_MODEL` 不是死代码，它在任何
        `.env` 驱动的部署里都能设（`.env` 由 `Module/_common/env.py::ensure_dotenv`
        泛化载入 `os.environ`），且实测能到达 provider 请求。它的问题是
        **未被任何发行面文档化**（`.env.example` / `config.yaml` / `scripts/setup.sh`
        / `docker-compose*.yml` / `.github/workflows/` 全部零命中），不是不可设。
        """
        monkeypatch.setenv(_ENV_KEY, "deepseek-v4-flash")
        assert _infra().llm_model == "deepseek-v4-flash"

    def test_default_is_a_bare_model_name_not_provider_qualified(self, monkeypatch):
        """回退值是**裸模型名**，不含 `provider/` 前缀。

        为什么必须有这条：`llm_model` 被原样塞进 OpenAI 兼容 API 的 `model=` 字段
        （`planner/parser.py:57`、`executor.py:392/410/487/503`、`evaluator.py:266/282`），
        而 API 期望的是 `deepseek-v4-pro`，不是 `deepseek/deepseek-v4-pro`
        （后者是 `LLMFactory` 的标识串）。于是任何"直接从 `config.yaml::llm.model`
        接线"的修法都必须先**剥掉 provider 前缀**——本条把这个陷阱钉成红的。
        """
        monkeypatch.delenv(_ENV_KEY, raising=False)
        assert "/" not in _infra().llm_model

    def test_explicit_value_wins_over_env(self, monkeypatch):
        """显式传参优先于 env（注入者可以覆盖操作员的兜底）。"""
        monkeypatch.setenv(_ENV_KEY, "from-env")
        assert _infra(llm_model="from-caller").llm_model == "from-caller"

    def test_preparer_takes_the_model_from_infra_verbatim(self):
        """`DelegationPreparer` 用注入的 `infra.llm_model`，**没有第二份默认值**。

        Batch 1 之前 `core/cognitive/delegation.py` 自己也读一次 `TASK_SYSTEM_MODEL`
        并带一份同样的 `"deepseek-v4-pro"` 默认值（同一 env 名 + 同一默认值在仓库里
        出现两次）。本条钉住"单一来源"。
        """
        from core.cognitive.delegation import DelegationPreparer

        assert DelegationPreparer(_infra(client=object(), llm_model="only-here"))._model == "only-here"  # noqa: SLF001

    def test_preparer_without_infra_has_no_silent_default(self):
        """未注入 infra → 模型名留空，而不是悄悄用某个 provider 的默认模型。

        `prepare()` 会先因 `_client is None` 抛 `DelegationUnavailableError`
        （显式不可用），所以空模型名不会变成一次静默请求。
        """
        from core.cognitive.delegation import DelegationPreparer

        preparer = DelegationPreparer()
        assert preparer._model == ""  # noqa: SLF001
        assert preparer._client is None  # noqa: SLF001


# ═══════════════════════════════════════════════════════════════════════════════
# 二、契约层：模型名的**唯一来源**（unit，静态）
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSingleModelSource:
    """后台任务的模型名在全仓只有一个读取点，且引擎不内嵌第二个模型字面量。"""

    @staticmethod
    def _production_files() -> list[Path]:
        files: list[Path] = []
        for pkg in ("core", "Module"):
            for path in (_REPO_ROOT / pkg).rglob("*.py"):
                s = str(path)
                if "__pycache__" in s or "/tests/" in s or path.name.startswith("test_"):
                    continue
                files.append(path)
        return files

    def test_env_var_has_exactly_one_production_read(self):
        """`TASK_SYSTEM_MODEL` 在生产代码里**恰好一处读取、零处写入**。

        实测（Batch 1.5/D 全仓 grep）：唯一读点是
        `Module/workflow_contract/infra.py` 的 `default_factory`。写点为零——
        没有任何生产代码设置它，也没有任何配置加载器把它灌进 `os.environ`。
        两个读点会让"模型名到底从哪来"重新变成不可判定；一个写点则是新配置面的信号，
        届时应连同文档一起显式更新本条。
        """
        read_patterns = (
            f'getenv("{_ENV_KEY}"',
            f"getenv('{_ENV_KEY}'",
            f'environ["{_ENV_KEY}"]',
            f"environ['{_ENV_KEY}']",
            f'environ.get("{_ENV_KEY}"',
            f"environ.get('{_ENV_KEY}'",
        )
        write_patterns = (
            f'environ["{_ENV_KEY}"] =',
            f"environ['{_ENV_KEY}'] =",
            f'setenv("{_ENV_KEY}"',
            f"setenv('{_ENV_KEY}'",
            f'environ.setdefault("{_ENV_KEY}"',
        )
        reads: set[str] = set()
        writes: set[str] = set()
        for path in self._production_files():
            text = path.read_text(encoding="utf-8")
            rel = str(path.relative_to(_REPO_ROOT))
            if any(p in text for p in read_patterns):
                reads.add(rel)
            if any(p in text for p in write_patterns):
                writes.add(rel)

        assert reads == {"Module/workflow_contract/infra.py"}, (
            f"{_ENV_KEY} 的生产读取点应恰好一处（AgentInfra.default_factory），"
            f"实测 {sorted(reads)}")
        assert not writes, (
            f"{_ENV_KEY} 不应有任何生产写点（没有配置加载器设置它），实测 {sorted(writes)}")

    def test_no_model_string_literal_passed_to_model_kwarg_in_engine(self):
        """引擎生产代码里没有 `model="<字面量>"`——模型名必须来自 `infra.llm_model`。

        7 个调用点（planner 1 / executor 4 / evaluator 2）全部传 `infra.llm_model`。
        这条挡的是"某处再写死一个 DeepSeek 模型名"的回归：那种第二来源不会让任何
        现有用例变红，只会让"换模型"变成一次考古。
        """
        offenders: list[str] = []
        engine_root = _REPO_ROOT / "Module" / "workflow_engine"
        for path in engine_root.rglob("*.py"):
            s = str(path)
            if "__pycache__" in s or "/tests/" in s:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "model" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        offenders.append(
                            f"{path.relative_to(_REPO_ROOT)}:{node.lineno} "
                            f'model="{kw.value.value}"')
        assert not offenders, (
            "引擎不得把模型名字面量直接传给 model=——应统一走 infra.llm_model："
            f"{offenders}")


# ═══════════════════════════════════════════════════════════════════════════════
# 三、引擎路径：`infra.llm_model` 原样到达请求（unit）
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEngineSendsInfraModelVerbatim:
    """Planner 的共享 LLM 调用点把 `infra.llm_model` 原样放进 `model=`。"""

    @pytest.mark.asyncio
    async def test_planner_call_llm_uses_infra_llm_model(self):
        from Module.workflow_engine.core.planner.parser import call_llm

        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            return_value=_fake_completion('{"nodes": []}'))
        infra = _infra(client=client, llm_model="some-model-x")

        await call_llm(infra, None, "sys", "usr")

        kwargs = client.chat.completions.create.await_args.kwargs
        assert kwargs["model"] == "some-model-x"
        assert kwargs["messages"][0]["role"] == "system"


# ═══════════════════════════════════════════════════════════════════════════════
# 四、服务层：真实启动步骤 `_start_task_system` 的接线（service）
# ═══════════════════════════════════════════════════════════════════════════════


def _host_agent(llm: object) -> object:
    """`object.__new__(Agent)` 真身替身——只补 `_start_task_system` 需要的槽位。

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
    host._delegation_preparer = None
    return host


@pytest.mark.service
class TestStartupStepWiring:
    """`_start_task_system`（`AgentInfra` 的唯一生产构造点）给引擎与委派器同一模型、同一 client。"""

    @pytest.mark.asyncio
    async def test_engine_and_preparer_share_model_and_client(self, monkeypatch):
        """单一来源：`WorkflowEngine.infra.llm_model` == `DelegationPreparer._model`，
        且两者持有**同一个** raw client 对象。

        实测（Case 1/2）：`_start_task_system` 构造 `AgentInfra` 时**不传**
        `llm_model=`，于是 `default_factory` 生效。本条只钉"两边一致"这一不变式；
        具体取值由上面 `TestLlmModelField` 覆盖。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        monkeypatch.delenv(_ENV_KEY, raising=False)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            infra = host._task_system._infra  # noqa: SLF001
            assert host._delegation_preparer._model == infra.llm_model  # noqa: SLF001
            assert host._delegation_preparer._client is infra.llm  # noqa: SLF001
            assert infra.llm is llm.get_raw_client()
        finally:
            await host._stop_task_system()  # noqa: SLF001
            await llm.close()

    @pytest.mark.asyncio
    async def test_task_model_is_independent_of_main_agent_model(self, monkeypatch):
        """**实测现状**：任务系统模型与主 Agent 的 `llm.model` 无推导关系。

        主 Agent 是 `deepseek/deepseek-v4-flash`（`config.yaml::llm.model`），
        任务系统是 `deepseek-v4-pro`——`infra.llm_model` 不是从 `llm.model` 派生的。
        这正是审计报告 `2026-09-16-…审计.md:446` 记录的"用户的模型选择对后台任务无效"。

        **当前契约快照**：主 agent 若决定让工作流跟随主模型，本条应改为断言
        `infra.llm_model == llm.model.split("/", 1)[1]`（或新配置源），
        并同步报告 §建议。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        monkeypatch.delenv(_ENV_KEY, raising=False)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            infra = host._task_system._infra  # noqa: SLF001
            assert llm.model == "deepseek/deepseek-v4-flash"
            assert infra.llm_model == _HARDCODED_FALLBACK
            assert infra.llm_model != llm.model
        finally:
            await host._stop_task_system()  # noqa: SLF001
            await llm.close()

    @pytest.mark.asyncio
    async def test_hot_switch_does_not_rewire_task_system(self, monkeypatch):
        """**实测现状**：主 LLM 热切换**不**重新接线任务系统。

        `LLMAdapter.switch_model()` 只换 `self._backend`；`AgentInfra` 是在
        `Agent.start()` 的 `task_system` 步骤里一次性构造的（`_STARTUP_STEPS` 的一步），
        热切换不重启 Agent，所以：

        - `engine.infra.llm` 仍是**旧** client（对象同一性不变）；
        - `infra.llm_model` 不变；
        - 且旧 client 已被 `switch_model` 的异步 `close()` 关掉
          （`LLMAdapter._closing_backends`）——实测 `old_client.is_closed() is True`，
          此后任务系统的任何调用都会以
          `APIConnectionError`（`__cause__` = `RuntimeError: Cannot send a request,
          as the client has been closed.`）失败。
          这是 Batch 1.5/D 报告里的发现，**不是**本用例要固化的期望行为。

        **当前契约快照**：主 agent 若决定"热切换后任务系统跟随"，本条应改为断言
        `infra.llm is llm.get_raw_client()` 且 `infra.llm_model` 同步。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
        monkeypatch.delenv(_ENV_KEY, raising=False)
        from core.adapter.llm_adapter import LLMAdapter

        llm = LLMAdapter("deepseek/deepseek-v4-flash")
        host = _host_agent(llm)
        try:
            await host._start_task_system()  # noqa: SLF001
            infra = host._task_system._infra  # noqa: SLF001
            before_client, before_model = infra.llm, infra.llm_model

            llm.switch_model("openai/gpt-4o")
            after_client = llm.get_raw_client()

            assert after_client is not before_client, "前提：热切换确实换了 client"
            assert infra.llm is before_client, "任务系统仍持有旧 client"
            assert infra.llm_model == before_model, "任务系统的模型名不随热切换变化"
            assert host._delegation_preparer._client is before_client  # noqa: SLF001
        finally:
            await host._stop_task_system()  # noqa: SLF001
            await llm.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 五、服务层：跨 provider 的错值必须**显式**失败并在两条路径上收口（service）
# ═══════════════════════════════════════════════════════════════════════════════


def _wire_capture(handler: Any) -> tuple[Any, list[dict[str, Any]]]:
    """真 `AsyncOpenAI` + `httpx.MockTransport`（离线、确定性），抓出站请求。"""
    from openai import AsyncOpenAI

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append({"url": str(request.url), "body": body,
                         "model": body.get("model")})
        return handler(request, body)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AsyncOpenAI(api_key="sk-test-only",
                         base_url="https://api.deepseek.com", http_client=http_client)
    return client, captured


@pytest.mark.service
class TestCrossProviderValueFailsExplicitly:
    """`TASK_SYSTEM_MODEL` 指向别的 provider 的模型名时：错误来自 provider，且两条路径都收口。"""

    @pytest.mark.asyncio
    async def test_wrong_model_name_travels_to_the_provider_verbatim(self, monkeypatch):
        """出站请求把错值**原样**发给当前 client 的端点——本地不做任何校验。

        实测：`TASK_SYSTEM_MODEL=gpt-4o` + DeepSeek client →
        `POST https://api.deepseek.com/chat/completions`，body `model="gpt-4o"`。
        也就是说，**失败点完全在 provider 侧**：本地既不拦、也没有 provider
        capability 表可查（`AgentInfra` 只有 client 与模型名两个字段）。
        """
        monkeypatch.delenv(_ENV_KEY, raising=False)
        monkeypatch.setenv(_ENV_KEY, "gpt-4o")
        from core.cognitive.delegation import DelegationPreparer
        from core.cognitive.user_profile import UserProfile

        client, captured = _wire_capture(lambda req, body: httpx.Response(
            200, json=_completion_body(body.get("model", ""), _PREPARER_OK)))
        try:
            infra = _infra(client=client)
            assert infra.llm_model == "gpt-4o"
            await DelegationPreparer(infra).prepare(
                intent="查天气", hints=None, session_messages=[],
                user_profile=UserProfile(), tools=_TOOLS,
                global_constraints=ConstraintConfig(), session_id=SessionId("s1"))

            assert len(captured) == 1
            assert captured[0]["url"] == "https://api.deepseek.com/chat/completions"
            assert captured[0]["model"] == "gpt-4o"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delegation_path_degrades_to_tool_result(self, monkeypatch):
        """委派路径：provider 报错 → 收口成 `ToolResult.fail`，异常**不冒进认知回合**。

        实测：`openai.BadRequestError`（400 Model Not Exist）→
        `CognitiveTools._handle_submit_task` 返回
        `success=False` / `"后台任务准备失败，请告知用户后台任务系统暂不可用，不要重复尝试。"`
        —— 这是 Batch 1 引入的降级，本用例钉住它**对引擎入口同样成立**。
        """
        monkeypatch.delenv(_ENV_KEY, raising=False)
        monkeypatch.setenv(_ENV_KEY, "gpt-4o")
        from core.cognitive.delegation import DelegationPreparer
        from core.cognitive.tools import CognitiveTools
        from core.schema.tool import ToolCall, ToolCallFunction
        from core.schema.types import ToolCallId

        def _reject(_req: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
            return httpx.Response(400, json={"error": {
                "message": "Model Not Exist", "type": "invalid_request_error",
                "param": None, "code": "invalid_request_error"}})

        client, _captured = _wire_capture(_reject)
        try:
            host = SimpleNamespace(
                _delegation_preparer=DelegationPreparer(_infra(client=client)),
                _task_system=SimpleNamespace(constraints=ConstraintConfig()),
                _tool_adapter=SimpleNamespace(definitions=lambda: [
                    {"function": {"name": "web_search", "description": "搜索"}}]),
                _session=SimpleNamespace(recent_messages=lambda: [], session_id="s1"),
                _user_profile=None,
            )
            tc = ToolCall(
                id=ToolCallId("call_1"),
                function=ToolCallFunction(
                    name="submit_task", arguments='{"intent": "查天气"}'))

            result = await CognitiveTools._handle_submit_task(host, tc)  # noqa: SLF001

            assert result.success is False
            assert "暂不可用" in (result.error or "")
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_engine_path_degrades_to_failed_task(self, monkeypatch):
        """引擎路径：Planner 的 provider 错误 → 任务 FAILED（`planning_failed`），不炸进程。

        这是"引擎路径也干净降级"的证据（不只 DelegationPreparer）：`_plan_with_retry`
        对**非解析类**异常同样重试 2 次，耗尽后 `task_state.fail(..., PLANNING_FAILED)`，
        编排层随后把 `failed` 事件推进 mailbox → `drain_mailboxes_once` →
        `terminal_payload` → 用户看到"任务执行失败: …"。
        """
        monkeypatch.delenv(_ENV_KEY, raising=False)
        monkeypatch.setenv(_ENV_KEY, "gpt-4o")
        from Module.workflow_contract import DelegationContext
        from Module.workflow_engine.core.planner.planner import Planner
        from Module.workflow_engine.core.sub_agent.planning import _plan_with_retry
        from Module.workflow_engine.schema import WorkflowErrorCode, WorkflowState

        def _reject(_req: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
            return httpx.Response(400, json={"error": {
                "message": "Model Not Exist", "type": "invalid_request_error",
                "param": None, "code": "invalid_request_error"}})

        client, _captured = _wire_capture(_reject)
        try:
            infra = _infra(client=client)
            context = DelegationContext(
                intent="查天气", hints=None, tools=list(_TOOLS),
                summarized_context="x", session_id=SessionId("s1"),
                return_mode=ReturnMode.NATURAL_LANGUAGE)
            task_state = WorkflowState(task_id=WorkflowId("task-1"))

            plan = await _plan_with_retry(
                Planner(infra, token_ledger=None), context,
                ConstraintConfig(), task_state, task_state.task_id)

            assert plan is None
            assert task_state.status.value == "failed"
            assert task_state.error_code == WorkflowErrorCode.PLANNING_FAILED
            assert "规划失败" in (task_state.error or "")
        finally:
            await client.close()
