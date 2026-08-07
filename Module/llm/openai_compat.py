"""OpenAI 兼容协议后端 — 覆盖 DeepSeek / OpenAI / Ollama 等。

Prefix Continuation:
  调用方在 messages 末尾添加 {"role": "assistant", "content": "{\\n", "prefix": True}，
  模块检测到后剥离 prefix 字段，设置 extra_body={"prefix": True}，
  并自动切换至 beta_url（DeepSeek 专用）。
"""

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable

import httpx
from openai import AsyncOpenAI

from Module.llm.protocol import LLMBackend
from Module._common.test_helpers import make_test_method


def _extract_usage(u: Any) -> dict:
    """从 SDK usage 对象提取归一化 token 消耗 dict（input/output 语义）。

    产出契约（供 TokenUsage.from_api 消费）：
    {"input_tokens": int, "output_tokens": int, "total_tokens": int,
     "cached_tokens": int | None, "reasoning_tokens": int | None}
    cached/reasoning 始终带键，provider 未返回时值为 None（or 0 归一化保留，
    防必填 int 校验失败；getattr 守卫防 details Optional 下 AttributeError）。
    仅做字段映射，不碰 chunk 遍历 / choices 哨兵 / is-not-None 守卫（防 M6 回归）。
    """
    cached = getattr(u, "prompt_tokens_details", None)
    reasoning = getattr(u, "completion_tokens_details", None)
    return {
        "input_tokens": u.prompt_tokens or 0,
        "output_tokens": u.completion_tokens or 0,
        "total_tokens": u.total_tokens or 0,
        "cached_tokens": cached.cached_tokens if cached is not None else None,
        "reasoning_tokens": reasoning.reasoning_tokens if reasoning is not None else None,
    }


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
        http_client: httpx.AsyncClient | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        """初始化 OpenAI 兼容协议后端。

        Args:
            model: 模型名称（如 "deepseek-v4-pro"）。
            api_key: API 密钥，空串时默认 "ollama"（Ollama 接受任意非空值）。
            base_url: 常规 API 的基础 URL。
            beta_url: DeepSeek prefix 续写专用的 beta API URL，
                非空时创建独立的 _beta_client。
            supports_prefix: 是否支持 prefix continuation 特性。
            logger: 日志记录器，未提供时自动创建。
            http_client: 出站守卫挂载点（批次 5）——注入的 httpx.AsyncClient 实例。
                仅静态复用（构造时使用），**reconnect 不重建**（单测/一次性场景）。
            http_client_factory: 出站守卫工厂（批次 5 review 修正）——零参可调用，
                每次调用返回**全新**守卫 client。生产 caller（switch/test/启动）传它：
                AsyncOpenAI wrapper 关闭会连带关闭当前注入 client，reconnect 经工厂
                重建全新守卫 client（守卫跨重启保留，修复"关闭后复用已关闭 client"）。
        """
        key = api_key or "ollama"  # Ollama 接受任意非空值
        self._model = model
        self._supports_prefix = supports_prefix
        self._logger = logger or logging.getLogger(__name__)
        self._api_key = key
        self._base_url = base_url
        self._beta_url = beta_url
        self._http_client_factory = http_client_factory
        self._injected_client: httpx.AsyncClient | None = http_client
        if http_client_factory is not None and http_client is None:
            self._injected_client = http_client_factory()
        self._beta_client: AsyncOpenAI | None = None
        self.last_usage: dict | None = None  # 最近一次 API 调用的 token 消耗
        self._create_clients()

        async def _health_check() -> bool:
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

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """非流式对话 — 内部调用 _call_api(stream=False)，拼接所有 chunk 返回。"""
        chunks: list[str] = []
        async for chunk in self._call_api(messages, stream=False, **kwargs):
            chunks.append(chunk)
        return "".join(chunks)

    async def chat_stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        """流式对话 — 内部调用 _call_api(stream=True)，逐 chunk 产出。"""
        async for chunk in self._call_api(messages, stream=True, **kwargs):
            yield chunk

    async def stream_think(
        self, messages: list[dict], **kwargs: Any
    ) -> AsyncIterator[dict]:
        """流式思考 — 逐 chunk 产出结构化 dict，同时支持 text 和 tool_calls 检测。

        每个 chunk: {"content": str | None, "tool_calls": list | None}
        调用方可以边流式输出文本边累积 tool_calls，流结束后统一决策。
        """
        async for chunk in self._stream_structured(messages, **kwargs):
            yield chunk

    async def _stream_structured(
        self, messages: list[dict], **kwargs: Any
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
                self.last_usage = _extract_usage(chunk.usage)
                yield {"content": None, "tool_calls": None, "usage": self.last_usage}
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
        self, messages: list[dict], *, stream: bool, **kwargs: Any
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
            stream_opts = {"include_usage": True}
            response = await client.chat.completions.create(
                model=self._model,
                messages=cleaned,  # type: ignore[arg-type]
                stream=True,
                stream_options=stream_opts,
                extra_body=extra_body_final,
                **kwargs,
            )
            async for chunk in response:  # type: ignore[union-attr]
                # 先判 choices 空：usage chunk 的 choices 恒为空数组（真实 API 契约），
                # 不能先访问 chunk.choices[0] 否则 IndexError；也不能用 getattr(usage)
                # 判真——MagicMock 下恒真。空 choices 也可能是 provider 流结束哨兵。
                if not chunk.choices:
                    if getattr(chunk, "usage", None) is not None:
                        self.last_usage = _extract_usage(chunk.usage)
                    continue  # usage chunk / 空 choices 流结束哨兵，均无实质内容
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
                # 非流式空 choices——显式抛错让上层决策（CognitiveLoop 走空响应守卫/
                # 重试）。原实现 error 后 return 空串，调用方收到「空白回复」无法感知（O2）。
                raise RuntimeError(
                    f"LLM 返回空 choices — 模型={self._model}"
                )
            content = response.choices[0].message.content
            # 捕获 usage 统计
            if getattr(response, "usage", None):
                self.last_usage = _extract_usage(response.usage)
            if content:
                yield content

    async def close(self) -> None:
        await self._client.close()
        if self._beta_client:
            await self._beta_client.close()

    async def reconnect(self) -> None:
        """重建 HTTP 客户端 — 用于服务重启后恢复连接（close() 之后调用）。

        先关闭尚存的旧 client（忽略异常），再通过 _create_clients() 重新创建。
        幂等操作：可安全多次调用。
        """
        # 关闭尚存的旧 client
        try:
            await self._client.close()
        except Exception:
            self._logger.warning("reconnect: 关闭旧 HTTP client 失败", exc_info=True)
        if self._beta_client:
            try:
                await self._beta_client.close()
            except Exception:
                self._logger.warning("reconnect: 关闭旧 beta HTTP client 失败", exc_info=True)
        # review 修正：AsyncOpenAI.close() 连带关闭当前注入 client（含守卫 client）。
        # 有工厂时重建全新守卫 client——否则旧 client 已关闭仍被复用，custom LLM 重启即死。
        if self._http_client_factory is not None:
            self._injected_client = self._http_client_factory()
        self._create_clients()

    def _create_clients(self) -> None:
        """创建/重建 OpenAI client（常规 + beta），共用超时配置。

        创建 self._client（常规 API），如果提供了 beta_url 则额外创建
        self._beta_client。超时配置：60s 总超时，10s 连接超时。
        当前注入 client（批次 5 出站守卫挂载点，self._injected_client）非 None 时
        透传给 AsyncOpenAI；reconnect() 已从工厂重建 self._injected_client（守卫
        跨重启保留，不复用已关闭实例）。
        """
        timeout = httpx.Timeout(60.0, connect=10.0)
        client_kwargs: dict = {"api_key": self._api_key, "timeout": timeout}
        if self._injected_client is not None:
            client_kwargs["http_client"] = self._injected_client
        self._client = AsyncOpenAI(base_url=self._base_url, **client_kwargs)
        if self._beta_url:
            self._beta_client = AsyncOpenAI(base_url=self._beta_url, **client_kwargs)


def _extract_prefix(messages: list[dict]) -> tuple[list[dict], bool]:
    """遍历 messages，检测并剥离 prefix 字段。

    Returns:
        (cleaned_messages, has_prefix)
    """
    has_prefix = any(m.get("prefix") for m in messages)
    cleaned = [{k: v for k, v in m.items() if k != "prefix"} for m in messages]
    return cleaned, has_prefix
