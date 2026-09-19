"""真实 Ollama 的 runtime context 旁证（Batch 17 / KB-13 的 OLL-E1/E2/E3）。

## 为什么不进任何默认档位

本模块需要**真实本地 Ollama daemon**（不是凭据、也不是本仓能保证存在的依赖），
故只标 `external`：`make test-external`（`RUN_EXTERNAL_TESTS=1`）才跑，
`make test-quick` / `test-normal` / `test-full` 都不会碰它——CI 因此**不依赖**
本机 Ollama / GPU / 外网模型下载（§114）。

**跑它会卸载测试模型**（`ollama stop <model>`，为了拿到真正的 cold runner）：
只动 `ELYSIA_TEST_OLLAMA_MODEL`（默认 `qwen2.5:3b`）这一个模型，不 pull 任何模型、
不改 daemon 级配置（§36/§117）。daemon 不可达时**显式 skip**（附原因），不假装通过。

## 它证的是什么

```text
configured（providers.yaml::context_window，预算权威）
    == requested（wire options.num_ctx）
    == observed（GET /api/ps context_length，**事后**旁证）
```

`/api/ps` 只在这里、以及 `probe_runtime_context()` 里出现——**不在 generation 热路径**
（OL4）。环境与实测输出见 `docs/review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md`。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

import httpx
import pytest

pytestmark = pytest.mark.external

_MODEL = os.getenv("ELYSIA_TEST_OLLAMA_MODEL", "qwen2.5:3b")
_NATIVE = os.getenv("ELYSIA_TEST_OLLAMA_NATIVE_URL", "http://localhost:11434")
_PS_TIMEOUT = 5.0


def _ps(url: str = _NATIVE) -> list[dict]:
    with urllib.request.urlopen(f"{url}/api/ps", timeout=_PS_TIMEOUT) as response:
        return json.loads(response.read()).get("models", [])


def _require_daemon() -> None:
    try:
        _ps()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        pytest.skip(f"本地 Ollama 不可达（{_NATIVE}）：{exc} —— 本模块需要真实 daemon")


def _unload() -> None:
    subprocess.run(["ollama", "stop", _MODEL], capture_output=True, check=False)


def _observed(model: str = _MODEL) -> int | None:
    short = model.split(":", 1)[0]
    for entry in _ps():
        name = str(entry.get("name") or "")
        if name == model or name.split(":", 1)[0] == short:
            value = entry.get("context_length")
            return int(value) if value else None
    return None


async def _generate(messages: list[dict], model_id: str):
    """走**真实生产路径**（工厂 + LLMAdapter + 流式），返回 (文本, usage, backend)。"""
    from core.adapter.llm_adapter import LLMAdapter

    adapter = LLMAdapter(model_id)
    text = ""
    usage: dict | None = None
    async for chunk in adapter.stream_think(
            messages, extra_body={"thinking": {"type": "disabled"}}):
        if chunk.get("content"):
            text += chunk["content"]
        if "usage" in chunk:
            usage = chunk["usage"]
    backend = adapter._backend  # noqa: SLF001 — 诊断面：本用例要读的就是"哪个后端 + 它请求了什么"
    return text, usage, backend


class TestColdRuntimeContext:
    @pytest.mark.asyncio
    async def test_cold_ollama_serves_the_configured_context(self):
        """OLL-E1/E2：冷 runner 上，Elysia 声明 8192 ⇒ 请求 8192 ⇒ 观测 8192。"""
        _require_daemon()
        from Module.llm.registry import LLMFactory

        model_id = f"ollama/{_MODEL}"
        configured = LLMFactory.context_window(model_id)
        assert configured, "providers.yaml 必须为 ollama 声明 context_window"

        _unload()
        assert _observed() is None, "冷启动前提：模型必须未加载"

        text, usage, backend = await _generate(
            [{"role": "user", "content": "只回复两个字：收到"}], model_id)
        try:
            assert text.strip(), "真实生成不得为空"
            assert usage and usage["input_tokens"] > 0 and usage["output_tokens"] > 0, (
                f"native 终帧 usage 必须归一：{usage}")
            assert backend.configured_context == configured
            assert backend.requested_context == configured

            info = await backend.probe_runtime_context()
            print(f"\n[OLL-E] model={_MODEL} configured={configured} "
                  f"requested={backend.requested_context} observed={info['observed']} "
                  f"usage={usage} error={info['error']}")
            assert info["error"] is None, info["error"]
            assert info["observed"] == configured, (
                f"runtime observed={info['observed']} != configured={configured}"
                "——budgeting authority 与 serving window 分叉（KB-13）")
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_configured_downshift_changes_the_observed_window(self, monkeypatch):
        """§37/§72：声明 4096 ⇒ 观测 4096（证明不是硬编码 8192，且 warm 模型会按请求重载）。"""
        _require_daemon()
        from Module.llm import registry
        from Module.llm.registry import LLMFactory

        monkeypatch.setitem(registry._BACKENDS["ollama"], "context_window", 4096)  # noqa: SLF001
        model_id = f"ollama/{_MODEL}"
        assert LLMFactory.context_window(model_id) == 4096

        _text, _usage, backend = await _generate(
            [{"role": "user", "content": "只回复两个字：收到"}], model_id)
        try:
            info = await backend.probe_runtime_context()
            print(f"\n[OLL-E downshift] requested=4096 observed={info['observed']}")
            assert info["observed"] == 4096, info
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_missing_declaration_does_not_allocate_the_generic_fallback(
            self, monkeypatch):
        """OL2 的真实面：未声明 ⇒ 不申请分配（**尤其**不许把 32K 预算回退发出去）。

        daemon 默认窗口是机器相关的（本机 4096），故断言只落在**我们有没有申请**上；
        `observed` 仅打印。
        """
        _require_daemon()
        from Module.llm import registry
        from Module.llm.registry import LLMFactory
        from Module.system_prompt.budget import DEFAULT_CONTEXT_WINDOW

        monkeypatch.delitem(registry._BACKENDS["ollama"], "context_window")  # noqa: SLF001
        model_id = f"ollama/{_MODEL}"
        assert LLMFactory.context_window(model_id) is None

        _text, _usage, backend = await _generate(
            [{"role": "user", "content": "只回复两个字：收到"}], model_id)
        try:
            info = await backend.probe_runtime_context()
            print(f"\n[OLL-E missing] requested={info['requested']} "
                  f"observed={info['observed']} (daemon default)")
            assert backend.requested_context is None, "未声明时不得申请运行时分配"
            assert info["observed"] != DEFAULT_CONTEXT_WINDOW, (
                "通用预算回退值被真的分配了——OL2 被破坏")
        finally:
            await backend.close()


class TestWarmContextChange:
    @pytest.mark.asyncio
    async def test_warm_runner_follows_the_requested_context(self, monkeypatch):
        """§71/§72：warm runner 上 4096 → 8192 → 4096，`/api/ps` 跟着请求走。

        这是"要不要 unload/retry"这个问题的答案：Ollama 0.24.0 会按请求的 `num_ctx`
        重载 runner（实测 ~2.2s；同值则复用 ~0.1s）⇒ **不需要**任何自动卸载/重试。
        """
        _require_daemon()
        from Module.llm import registry

        model_id = f"ollama/{_MODEL}"
        seen: list[int | None] = []
        for window in (4096, 8192, 4096):
            monkeypatch.setitem(registry._BACKENDS["ollama"], "context_window", window)  # noqa: SLF001
            _text, _usage, backend = await _generate(
                [{"role": "user", "content": "只回复两个字：收到"}], model_id)
            try:
                info = await backend.probe_runtime_context()
                seen.append(info["observed"])
            finally:
                await backend.close()
        print(f"\n[OLL-E warm] observed sequence = {seen}")
        assert seen == [4096, 8192, 4096], (
            f"warm runner 未跟随请求的 num_ctx：{seen}")


class TestSilentTruncationContract:
    @pytest.mark.asyncio
    async def test_oversized_prompt_is_truncated_silently_not_400(self, monkeypatch):
        """§18/§19/§95：超出 runtime context 的真实行为是 **HTTP 200 + 静默截断**。

        `prompt_eval_count` 因此只能作**诊断**（Elysia 不能把"provider 接受了请求"
        等价于"整份 prompt 被消费了"）；本用例把它与"我们发的 prompt 估算量"并排打印，
        不做任何 reverse-engineering（KB-12 不在本批范围）。
        """
        _require_daemon()
        from Module.llm import registry
        from Module.system_prompt.budget import estimate_messages_tokens

        monkeypatch.setitem(registry._BACKENDS["ollama"], "context_window", 512)  # noqa: SLF001
        model_id = f"ollama/{_MODEL}"
        big = "word " * 3000
        messages = [{"role": "user", "content": f"请复述最后一句：{big}"}]
        estimated = estimate_messages_tokens(messages)

        _text, usage, backend = await _generate(messages, model_id)
        try:
            info = await backend.probe_runtime_context()
            print(f"\n[OLL-E truncation] estimated_prompt={estimated} "
                  f"usage={usage} observed={info['observed']}")
            assert info["observed"] == 512
            assert usage is not None and usage["input_tokens"] <= 512, (
                f"prompt_eval_count 应被 runtime window 截住：{usage}")
            assert usage["input_tokens"] > 0
        finally:
            await backend.close()


class TestDiagnosticsDoNotTouchTheHotPath:
    @pytest.mark.asyncio
    async def test_generation_works_even_if_ps_is_unreachable(self, monkeypatch):
        """§68/§106：`/api/ps` 查询失败（这里用错端口模拟）不得影响 generation。

        说明：native `/api/chat` 仍走真实 daemon；只有**诊断**端点的地址被指错，
        以证明诊断失败与 generation 无耦合。
        """
        _require_daemon()
        from Module.llm.registry import LLMFactory

        model_id = f"ollama/{_MODEL}"
        backend = LLMFactory.create(model_id)
        try:
            backend._native_base = "http://127.0.0.1:9"  # noqa: SLF001 — 只毒化诊断端点
            info = await backend.probe_runtime_context()
            assert info["observed"] is None and info["error"], info

            backend._native_base = _NATIVE  # noqa: SLF001 — 恢复后 generation 照常
            text = await backend.chat([{"role": "user", "content": "只回复：好"}])
            assert text.strip()
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_connection_failure_is_not_a_silent_success(self):
        """§63：daemon 不可达 ⇒ 抛错（不伪造空回复）；且该错误不带可重试状态码。"""
        from Module.llm.registry import LLMFactory

        backend = LLMFactory.create(f"ollama/{_MODEL}",
                                    base_url="http://127.0.0.1:9/v1")  # 无人监听
        try:
            with pytest.raises((httpx.ConnectError, httpx.HTTPError)):
                await backend.chat([{"role": "user", "content": "hi"}])
        finally:
            await backend.close()
