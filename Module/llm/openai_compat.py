"""OpenAI 兼容协议后端 — 覆盖 DeepSeek / OpenAI / Ollama 等。

Prefix Continuation:
  调用方在 messages 末尾添加 {"role": "assistant", "content": "{\\n", "prefix": True}，
  模块检测到后剥离 prefix 字段，设置 extra_body={"prefix": True}，
  并自动切换至 beta_url（DeepSeek 专用）。
"""

import asyncio
import logging
import time
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from .protocol import LLMBackend


class OpenAICompatBackend(LLMBackend):
    """OpenAI 兼容协议后端。

    维护两个 AsyncOpenAI client：
    - _client: 常规 API 调用
    - _beta_client: DeepSeek prefix 续写专用（仅在构造时提供了 beta_url 时创建）
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        base_url: str = "",
        beta_url: str = "",
        supports_prefix: bool = False,
        logger: logging.Logger | None = None,
    ):
        key = api_key or "ollama"  # Ollama accepts any non-empty value
        self._model = model
        self._supports_prefix = supports_prefix
        self._logger = logger or logging.getLogger(__name__)
        self._api_key = key
        self._base_url = base_url
        self._beta_url = beta_url
        _timeout = httpx.Timeout(60.0, connect=10.0)
        self._client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=_timeout)
        self._beta_client: AsyncOpenAI | None = None
        if beta_url:
            self._beta_client = AsyncOpenAI(api_key=key, base_url=beta_url, timeout=_timeout)

    async def chat(self, messages: list[dict], **kwargs) -> str:
        """非流式对话 — 内部调用 _call_api(stream=False)，拼接所有 chunk 返回。"""
        chunks: list[str] = []
        async for chunk in self._call_api(messages, stream=False, **kwargs):
            chunks.append(chunk)
        return "".join(chunks)

    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式对话 — 内部调用 _call_api(stream=True)，逐 chunk 产出。"""
        async for chunk in self._call_api(messages, stream=True, **kwargs):
            yield chunk

    async def stream_think(
        self, messages: list[dict], **kwargs
    ) -> AsyncIterator[dict]:
        """流式思考 — 逐 chunk 产出结构化 dict，同时支持 text 和 tool_calls 检测。

        每个 chunk: {"content": str | None, "tool_calls": list | None}
        调用方可以边流式输出文本边累积 tool_calls，流结束后统一决策。
        """
        async for chunk in self._stream_structured(messages, **kwargs):
            yield chunk

    async def _stream_structured(
        self, messages: list[dict], **kwargs
    ) -> AsyncIterator[dict]:
        """结构化流式 API 调用 — 产出包含 content 和 tool_calls 的 dict。"""
        cleaned, has_prefix = _extract_prefix(messages)
        if has_prefix and not self._supports_prefix:
            raise ValueError(
                f"prefix continuation is not supported by this backend (model={self._model})"
            )

        caller_extra = kwargs.pop("extra_body", None) or {}
        extra_body: dict = {}
        if has_prefix:
            extra_body["prefix"] = True
        extra_body.update(caller_extra)

        client = self._beta_client if has_prefix and self._beta_client else self._client
        extra_body_final = extra_body if extra_body else None

        response = await client.chat.completions.create(
            model=self._model,
            messages=cleaned,  # type: ignore[arg-type]
            stream=True,
            extra_body=extra_body_final,
            **kwargs,
        )
        async for chunk in response:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta
            content = delta.content if delta and delta.content else None
            raw_tc = delta.tool_calls if delta and delta.tool_calls else None
            tool_calls = None
            if raw_tc:
                tool_calls = [
                    {
                        "index": tc.index,
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name if tc.function else "",
                            "arguments": tc.function.arguments if tc.function else "",
                        },
                    }
                    for tc in raw_tc
                ]
            yield {"content": content, "tool_calls": tool_calls}

    async def _call_api(
        self, messages: list[dict], *, stream: bool, **kwargs
    ) -> AsyncIterator[str]:
        """统一 API 调用入口。

        1. 检测 prefix 并剥离
        2. 合并调用方 extra_body 与内部 prefix extra_body
        3. 自动切换 beta client（如需要）
        4. 调用 OpenAI chat.completions.create
        """
        cleaned, has_prefix = _extract_prefix(messages)
        if has_prefix and not self._supports_prefix:
            self._logger.error(
                "prefix continuation requested but not supported (model=%s)", self._model
            )
            raise ValueError(
                f"prefix continuation is not supported by this backend (model={self._model})"
            )

        caller_extra = kwargs.pop("extra_body", None) or {}
        extra_body: dict = {}
        if has_prefix:
            extra_body["prefix"] = True
        extra_body.update(caller_extra)

        client = self._beta_client if has_prefix and self._beta_client else self._client
        extra_body_final = extra_body if extra_body else None

        if stream:
            response = await client.chat.completions.create(
                model=self._model,
                messages=cleaned,  # type: ignore[arg-type]
                stream=True,
                extra_body=extra_body_final,
                **kwargs,
            )
            async for chunk in response:  # type: ignore[union-attr]
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        else:
            response = await client.chat.completions.create(
                model=self._model,
                messages=cleaned,  # type: ignore[arg-type]
                stream=False,
                extra_body=extra_body_final,
                **kwargs,
            )
            content = response.choices[0].message.content
            if content:
                yield content

    async def close(self) -> None:
        await self._client.close()
        if self._beta_client:
            await self._beta_client.close()

    async def test(self) -> dict:
        """测试连通性：发最小 chat 请求，测延迟。5 秒超时。"""
        t0 = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                ),
                timeout=5.0,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}

    async def reconnect(self) -> None:
        """Recreate HTTP clients — used on service restart after stop() closed them."""
        # Close old client if still open
        try:
            await self._client.close()
        except Exception:
            pass
        if self._beta_client:
            try:
                await self._beta_client.close()
            except Exception:
                pass
        # Create new clients with timeout
        timeout = httpx.Timeout(60.0, connect=10.0)
        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=timeout,
        )
        if self._beta_url:
            self._beta_client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._beta_url,
                timeout=timeout,
            )


def _extract_prefix(messages: list[dict]) -> tuple[list[dict], bool]:
    """遍历 messages，检测并剥离 prefix 字段。

    Returns:
        (cleaned_messages, has_prefix)
    """
    has_prefix = any(m.get("prefix") for m in messages)
    cleaned = [{k: v for k, v in m.items() if k != "prefix"} for m in messages]
    return cleaned, has_prefix
