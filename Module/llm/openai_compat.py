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
from Module._common.test_helpers import make_test_method


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
        self._beta_client: AsyncOpenAI | None = None
        self.last_usage: dict | None = None  # 最近一次 API 调用的 token 消耗
        self._create_clients()

        async def _health_check(self) -> bool:
            await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                ),
                timeout=5.0,
            )
            return True

        self._test_fn = make_test_method(_health_check)

    async def test(self) -> dict:
        """测试连通性：发最小 chat 请求，测延迟。5 秒超时。"""
        return await self._test_fn(self)

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
            stream_options={"include_usage": True},
            **kwargs,
        )
        async for chunk in response:  # type: ignore[union-attr]
            # 流式最后一个 chunk 可能携带 usage 统计（choices 通常为空）
            if getattr(chunk, "usage", None):
                u = chunk.usage
                usage = {
                    "prompt_tokens": u.prompt_tokens or 0,
                    "completion_tokens": u.completion_tokens or 0,
                    "total_tokens": u.total_tokens or 0,
                }
                self.last_usage = usage
                yield {"content": None, "tool_calls": None, "usage": usage}
                continue  # usage chunk 没有实质内容，跳过后续处理
            if not chunk.choices:
                continue  # 某些 provider（MiMo 等）返回空 choices 表示流结束
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
            if not response.choices:
                logger.error("LLM 返回空 choices — 模型=%s", self._model)
                return
            content = response.choices[0].message.content
            # 捕获 usage 统计
            if getattr(response, "usage", None):
                self.last_usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                    "total_tokens": response.usage.total_tokens or 0,
                }
            if content:
                yield content

    async def close(self) -> None:
        await self._client.close()
        if self._beta_client:
            await self._beta_client.close()

    async def reconnect(self) -> None:
        """重建 HTTP 客户端 — 用于服务重启后恢复连接（close() 之后调用）。"""
        # 关闭尚存的旧 client
        try:
            await self._client.close()
        except Exception:
            pass
        if self._beta_client:
            try:
                await self._beta_client.close()
            except Exception:
                pass
        self._create_clients()

    def _create_clients(self) -> None:
        """创建/重建 OpenAI client，共用超时配置。"""
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
