"""真实 Ollama 的 prompt token 真值旁证（Batch 18 / KB-12 的 OLL-E4–E7）。

## 为什么不进任何默认档位

需要**真实本地 Ollama daemon**（不是凭据、也不是本仓能保证存在的依赖），故只标
`external`：`make test-external`（`RUN_EXTERNAL_TESTS=1`）才跑。CI 因此不依赖
本机 Ollama / GPU / 外网模型下载（§114/§158）。

daemon 不可达时**显式 skip**（附原因），不假装通过。

## 它证的是什么

1. **OLL-E4 真值可得**：`prompt_eval_count` 在 `num_ctx` 足够大时是完整 prompt 的
   token 数；把 `num_ctx` 设小会得到 `prompt_eval_count == num_ctx`（被截断），
   那种数**不能当真值**（§13/§14/§152）。
2. **OLL-E5 修前 false accept**：真实 prompt 已超窗口，而修前的字符启发式仍判
   "在预算内"（它既低估密集内容、又完全不看 tools）⇒ 请求被发出并被 provider
   静默截断。新估算器在同一请求上必须判"超预算"。
3. **OLL-E6 修前 false reject**：真实 prompt 装得下窗口，而修前估算 ≥ 窗口 ⇒
   预览本地 400、根本不发请求。新估算器必须放行。
4. **OLL-E7 tool schema 不可见**：带/不带 `tools` 的 `prompt_eval_count` 差值就是
   工具段的真实成本；结构化估算必须落在一个有界区间里（不是"有没有算"而已）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.external

_MODEL = os.getenv("ELYSIA_TEST_OLLAMA_MODEL", "qwen2.5:3b")
_NATIVE = os.getenv("ELYSIA_TEST_OLLAMA_NATIVE_URL", "http://localhost:11434")
_WINDOW = 8192
_BIG_CONTEXT = 32768          # 真值档：远大于被测 prompt，保证不被截断
_REPO = Path(__file__).resolve().parents[3]

from Module.system_prompt.budget import (  # noqa: E402
    budget_from_context_window,
    effective_context_window,
    estimate_messages_tokens,
    estimate_tools_tokens,
)
from Module.system_prompt.tests.test_token_corpus_contract import (  # noqa: E402
    legacy_estimate_messages,
)


def _require_daemon() -> None:
    try:
        with urllib.request.urlopen(f"{_NATIVE}/api/ps", timeout=5):
            return
    except (urllib.error.URLError, OSError, ValueError) as exc:
        pytest.skip(f"本地 Ollama 不可达（{_NATIVE}）：{exc} —— 本模块需要真实 daemon")


async def _count(client: httpx.AsyncClient, messages: list[dict],
                 tools: list[dict] | None = None, *, ctx: int = _BIG_CONTEXT) -> int:
    body: dict = {"model": _MODEL, "messages": messages, "stream": False,
                  "options": {"num_ctx": ctx, "num_predict": 1}}
    if tools:
        body["tools"] = tools
    resp = await client.post(f"{_NATIVE}/api/chat", json=body)
    resp.raise_for_status()
    return int(resp.json()["prompt_eval_count"])


async def _production_tools() -> list[dict]:
    """本仓**真实**工具 schema（12 个内置工具；desktop 无桥时已被过滤）。"""
    from core.adapter.tool_adapter import ToolAdapter
    from core.tools import ALL_PLUGINS

    adapter = ToolAdapter(ALL_PLUGINS, session_id="t", user_name="t")
    await adapter.start(extra_services={})
    try:
        return adapter.definitions()
    finally:
        await adapter.shutdown()


def _real_system_prompt() -> str:
    """仓内真实内置角色 system prompt 快照（签入 fixture，代表性前缀）。"""
    fixture = _REPO / "core/prompt/tests/fixtures/system_prompt_snapshot_elysia_cf4bc008.txt"
    return fixture.read_text(encoding="utf-8")[:1500]


@pytest.mark.asyncio
async def test_truncated_count_is_not_truth():
    """OLL-E4/§152：`num_ctx` 太小时 `prompt_eval_count == num_ctx`，**不是**真值。

    这条用例本身就是"基准不能被截断污染"的机器化检查：它先证明污染可见，
    再证明用大 `num_ctx` 能得到一个**不同且更大**的数。
    """
    _require_daemon()
    big_text = "她打开笔记本，开始把还能记起来的事情一件件写下来。" * 400
    messages = [{"role": "user", "content": big_text}]
    async with httpx.AsyncClient(timeout=600) as client:
        truncated = await _count(client, messages, ctx=1024)
        truth = await _count(client, messages)
    print(f"\n[OLL-E4] num_ctx=1024 -> {truncated} ; num_ctx={_BIG_CONTEXT} -> {truth}")
    assert truncated == 1024, "该请求本该被窗口截住"
    assert truth > truncated, "大窗口下的计数必须更大——否则它也只反映被截断的 prompt"


@pytest.mark.asyncio
async def test_legacy_false_accept_becomes_a_truthful_budget_decision():
    """OLL-E5：修前判"在预算内"、真实已超窗 ⇒ 静默截断；新估算器必须判超预算。"""
    _require_daemon()
    budget = budget_from_context_window(_WINDOW)
    system = _real_system_prompt()
    tools = await _production_tools()
    payload = json.dumps(
        [{"id": i, "name": f"item_{i}", "tags": ["alpha", "beta"],
          "meta": {"rev": i, "active": i % 2 == 0}} for i in range(208)],
        ensure_ascii=False, separators=(",", ":"))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": payload}]
    async with httpx.AsyncClient(timeout=1800) as client:
        truth = await _count(client, messages, tools)
        at_window = await _count(client, messages, tools, ctx=_WINDOW)
    legacy = legacy_estimate_messages(messages)
    model_id = f"ollama/{_MODEL}"
    new = estimate_messages_tokens(messages, model=model_id, tools=tools)
    print(f"\n[OLL-E5] truth={truth} at_window={at_window} legacy={legacy} new={new} "
          f"budget={budget} tools_est={estimate_tools_tokens(tools, model=model_id)}")
    assert truth > _WINDOW, f"本用例前提：真实 prompt 必须超窗（truth={truth}）"
    assert at_window == _WINDOW, "超窗请求在窗口档下必须表现为被截断"
    assert legacy <= budget, (
        f"本用例前提：修前估算必须判「在预算内」（legacy={legacy} budget={budget}）")
    assert new > budget, "修后估算必须如实判超预算（否则 false accept 未修）"
    assert legacy / truth < 0.75, f"修前低估不足 25%（ratio={legacy / truth:.2f}）"


@pytest.mark.asyncio
async def test_legacy_false_reject_becomes_an_accepted_preview():
    """OLL-E6：真实 prompt 装得下窗口，修前估算却 ≥ 窗口 ⇒ 预览 400（从不发请求）。"""
    _require_daemon()
    window = effective_context_window(_WINDOW)
    messages = [{"role": "system", "content": _real_system_prompt()},
                {"role": "user",
                 "content": "她打开笔记本，开始把还能记起来的事情一件件写下来。" * 300}]
    async with httpx.AsyncClient(timeout=1800) as client:
        truth = await _count(client, messages)
    legacy = legacy_estimate_messages(messages)
    new = estimate_messages_tokens(messages, model=f"ollama/{_MODEL}")
    print(f"\n[OLL-E6] truth={truth} legacy={legacy} new={new} window={window}")
    assert truth < window, f"本用例前提：真实 prompt 必须装得下（truth={truth}）"
    assert legacy >= window, f"本用例前提：修前估算必须触发本地拒绝（legacy={legacy}）"
    assert new < window, "修后估算必须放行——否则误拒仍在"


@pytest.mark.asyncio
async def test_tool_schema_cost_is_measured_not_guessed():
    """OLL-E7：带/不带 `tools` 的差值就是工具段真实成本；估算必须有界且非零。"""
    _require_daemon()
    tools = await _production_tools()
    messages = [{"role": "user", "content": "在吗"}]
    model_id = f"ollama/{_MODEL}"
    async with httpx.AsyncClient(timeout=900) as client:
        without = await _count(client, messages)
        with_tools = await _count(client, messages, tools)
    actual = with_tools - without
    est = estimate_tools_tokens(tools, model=model_id)
    print(f"\n[OLL-E7] tools={len(tools)} actual_delta={actual} est={est} "
          f"ratio={est / actual:.2f}")
    assert actual > 500, "本仓工具集不可能这么便宜——证明它值得被计"
    assert 0.75 <= est / actual <= 1.30, (
        f"工具段估算越界：est={est} actual={actual} ratio={est / actual:.2f}")


@pytest.mark.asyncio
async def test_message_framing_is_a_bounded_per_message_cost():
    """OLL-E8/§86/§172：每条消息的框架成本实测为常数；估算不为 0 也不离谱。"""
    _require_daemon()
    model_id = f"ollama/{_MODEL}"
    async with httpx.AsyncClient(timeout=900) as client:
        counts = {}
        for n in (1, 5, 20):
            msgs = [{"role": "system", "content": "S"}] + [
                {"role": "user" if i % 2 == 0 else "assistant", "content": "x"}
                for i in range(n)]
            counts[n] = await _count(client, msgs, ctx=_WINDOW)
    measured = (counts[20] - counts[1]) / 19
    est = (estimate_messages_tokens(
        [{"role": "system", "content": "S"}] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "x"}
            for i in range(20)], model=model_id) -
        estimate_messages_tokens(
            [{"role": "system", "content": "S"},
             {"role": "user", "content": "x"}], model=model_id)) / 19
    print(f"\n[OLL-E8] measured marginal={measured:.2f}/msg est marginal={est:.2f}/msg "
          f"counts={counts}")
    assert 4.0 <= measured <= 8.0, f"框架成本应是有界常数：{measured:.2f}"
    assert est >= measured * 0.8, f"估算明显低于实测框架成本（est={est:.2f}）"
