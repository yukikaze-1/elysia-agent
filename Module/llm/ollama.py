"""Ollama native 协议后端 —— provider-specific runtime semantics 的**收口点**（AR-16）。

## 为什么需要独立后端（而不是在通用 backend 里加一个分支）

`KB-13`：Elysia 用 `providers.yaml::llm.<provider>.context_window` 做 prompt budgeting，
但 Ollama 的**实际 serving context 是运行时分配**（`options.num_ctx`）。两者没有绑定
时，"预算装得下"与"provider 真的服务这么多"可以分叉，而 Ollama 超窗是
**HTTP 200 + 静默截断**——provider error 不会替我们发现 mismatch。

本机实测（Ollama 0.24.0 / `qwen2.5:3b`，冷启动 A/B，详见 Batch 17 执行记录）：

| 请求 | `GET /api/ps context_length` |
|---|---|
| OpenAI-compatible `/v1/chat/completions`（无 `options`） | 4096 |
| OpenAI-compatible `/v1/chat/completions` + `options.num_ctx=8192` | **4096（静默忽略）** |
| native `/api/chat` + `options.num_ctx=8192` | **8192** ✅ |

> **"4096" 是本机（6GB 显存）的 daemon 默认，不是 Ollama 的固定事实**：二进制文档里
> `OLLAMA_CONTEXT_LENGTH` 的默认是"按显存取 4k/32k/256k"。所以别把 4096 当常数写进
> 断言或文档结论——本批钉住的是**请求侧能不能表达**（compat 不能 / native 能），
> 以及"**不声明就不申请**"（OL2）。独立复核还试过 12 条 compat 旁路
> （顶层 `num_ctx`、`options.options`、query/header、`/v1/completions`、模型名后缀…）
> **全部无效**，其中模型名后缀直接 400 `invalid model name`。

⇒ **通用后端所用的 OpenAI-compatible 端点根本无法表达 runtime context**。
所以修复不是"多传一个字段"，而是把 Ollama 的 wire/runtime 语义移到 native 协议上收口
（协议切换判据见执行记录 §Protocol decision；`raw_client` 例外见下）。

## 职责边界（**只做**这些）

- request / wire translation（**双向**都必须做，这是 AR-16 的核心职责）：
  - messages：OpenAI 形状 → native（`content` 块列表 → `content` + `images`）；
  - **回放的 `tool_calls`**：`function.arguments` 从 JSON **字符串**（通用契约）转回
    native 要求的**对象**——不转则工具轮第二次请求必 400（见 `_native_tool_arguments`）；
  - `tool_call_id` / `tool_name` 透传（native 认得，丢掉等于删信息）；
- 运行时选项：`num_ctx`（**唯一**分配权威，来自显式声明的 `context_window`）、
  `num_predict`（`max_tokens` 的 native 对应）、`temperature`、`keep_alive`（内部钩子）；
- response / usage normalization：native 的 `tool_calls`（对象 + `function.index`）→
  通用 delta 契约（JSON 字符串 + 顶层 `index`）；`prompt_eval_count`/`eval_count` →
  通用 usage 契约（**终帧恰好一次**）；
- runtime diagnostics：`probe_runtime_context()`（configured / requested / observed，
  opt-in、失败不抛）。

**不做**：对话策略、prompt 预算、记忆、人格、会话生命周期——那些在上层，且本后端
不感知（它只看到最终的 `messages[]`）。也**不**把 `num_ctx` 暴露给 `CognitiveLoop`。

## 为什么"预算 == `num_ctx`"是**承重**的（而不是好看）

Ollama 超窗时的截断策略是"**丢中间**"：只保留最前面 `num_keep` 个 token（默认 **4**）
+ 最近 `num_ctx - num_keep`，中间全部丢弃（独立复核用"头部标记 + `num_keep` 三档"实测：
`num_keep=4`/`0` 时模型回忆不起头部标记，`num_keep=64` 时能）。而 Elysia 的预算裁剪
保护的是 **system prompt + 当前消息**（丢的是最老的历史）——**两边保护的正好相反**。
所以一旦 budget 与 `num_ctx` 分叉，**最先静默劣化的是人设/system prompt**，
而不是最老的历史：这正是把两者绑在同一个值上的理由（OL1）。

## 三条硬边界

1. **通用预算回退不是分配指令（OL2）**：`context_window` 未声明/非法（≤0）时
   **不发** `options.num_ctx`，由 Ollama 用自己的默认；绝不发
   `budget.DEFAULT_CONTEXT_WINDOW`（32768）。实测 `num_ctx=0` / `-1` 都会得到
   `context_length=4` 的畸形 runner，而"不发"至少得到 daemon 的正常默认。
2. **`/api/ps` 不在热路径（OL4）**：正常运行保持 1 user generation → 1 provider request；
   `probe_runtime_context()` 是**显式**诊断调用，失败不抛、也绝不被 generation 依赖。
   ⚠️ `observed` 是**daemon 级、最后写入者胜**的视图，且小值会被夹（实测 `num_ctx=0/1/4`
   在 ps 上读作 4，而 llama 实际分配 256 格）。故它只能作"**紧接着我们自己的请求之后**"
   的旁证，**不是** allocation 的精确账本（诊断文档如此表述，集成测试也如此使用）。
3. **`raw_client` 保持 OpenAI 兼容侧车**：`WorkflowEngine` Planner/Executor 消费的是
   OpenAI SDK 形状（`chat.completions.create(...)`），属本批范围外的消费面；为不打断它，
   侧车**懒创建**、只在有人取 `raw_client` 时才建连接。⇒ 工作流路径（task LLM）
   **仍**不受 Elysia 的 runtime context 控制，这是本批的已知边界，已登记。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, AsyncIterator, Callable

import httpx

from Module._common.test_helpers import make_test_method
from Module.llm.protocol import LLMBackend

logger = logging.getLogger(__name__)

# native API 的默认根（providers.yaml 的 ollama base_url 是 OpenAI 兼容形态 .../v1）
_DEFAULT_NATIVE_BASE = "http://localhost:11434"

# 超时策略与 `OpenAICompatBackend` **逐字一致**（§88：不为 Ollama 另立一套）；
# 实测 3B 模型冷加载 ~1.9s、8192 重载 ~2.2s，60s 总预算充裕。
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _native_base_url(base_url: str) -> str:
    """把解析后的 `base_url` 归一成 native API 的根（chat 端点 = `{root}/api/chat`）。

    只处理**已知**的书写形态，其余原样透传（不猜用户意图）：

    - 去尾部 `/`：`http://host:11434/v1/` → `http://host:11434/v1`
    - 去 OpenAI 兼容后缀 `/v1`（`providers.yaml` 默认值就带它）
    - 去 native 后缀 `/api`（有人会写成 `.../api`；两种写法都指向同一个根）

    OpenAI 兼容侧车（`raw_client`）从**同一个根**派生 `{root}/v1`，于是
    "两个端点"在同一条归一化规则下，不会各自漂移（缺省配置下的结果与修复前逐字相同）。
    """
    base = (base_url or "").strip().rstrip("/")
    for suffix in ("/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/") or _DEFAULT_NATIVE_BASE


def _usage_from_native(data: dict) -> dict | None:
    """native 响应帧 → 通用 usage 契约（键集与 `openai_compat._extract_usage` 一致）。

    `prompt_eval_count` → `input_tokens`、`eval_count` → `output_tokens`：语义确实匹配
    （实测同一 prompt 在 native 与 OpenAI 兼容端点上分别报 30 / 30）。两者都缺失时
    返回 None——**不编造** 0（那会让 TokenTracker 记下一笔假的"0 token 回合"）。

    只从**终帧**调用（`done=true`）：native 流式只有终帧带这两个字段，故不存在
    "每 chunk 累加"的重复计数（OL5）。
    """
    prompt = data.get("prompt_eval_count")
    completion = data.get("eval_count")
    if prompt is None and completion is None:
        return None
    input_tokens = int(prompt or 0)
    output_tokens = int(completion or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_tokens": None,
        "reasoning_tokens": None,
    }


def _to_openai_tool_calls(raw: Any) -> list[dict] | None:
    """native `message.tool_calls` → 通用（OpenAI delta 形状）tool_calls。

    native 与 OpenAI 的**真实差异**（实测 0.24.0）：

    - native `function.arguments` 是**对象**（`{"city": "Paris"}`），OpenAI 是 JSON **字符串**；
    - native 带 `function.index`（并行调用时 0/1/...），OpenAI 的 index 在 delta 顶层；
    - native 每次给的是**完整调用**（不是分片），所以这里一次性转成 JSON 字符串——
      `merge_stream_tool_calls` 的拼接语义因此不会被触发成重复。
    """
    if not raw:
        return None
    converted: list[dict] = []
    for position, call in enumerate(raw):
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments_str = arguments
        elif arguments is None:
            arguments_str = ""
        else:
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        converted.append({
            "index": function.get("index", position),
            "id": call.get("id") or "",
            "type": call.get("type") or "function",
            "function": {"name": function.get("name") or "", "arguments": arguments_str},
        })
    return converted


def _native_tool_arguments(arguments: Any, *, model: str) -> dict:
    """OpenAI 形状的 tool_call `arguments` → native 要求的**对象**。

    ## 为什么必须转（P0：不转 = 每一轮工具调用的第二次请求必 400）

    实测（Ollama 0.24.0，用不存在的模型名只触发 body 解码）：

    | wire 上的 `arguments` | 结果 |
    |---|---|
    | 对象 `{"city": "Paris"}` / 缺省 / `null` | body 通过（随后 404 model not found） |
    | **任何字符串**（含 `""`、`'{"city": "Paris"}'`、`'{}'`） | **HTTP 400** `Value looks like object, but can't find closing '}' symbol` |
    | 数组 `[1, 2]` | 同上 400 |

    Elysia 回放工具轮时带的是 OpenAI 形状：`core/cognitive/tool_phase.py` 把
    `ToolCall.model_dump()`（`arguments` 是 **JSON 字符串**，见 `core/schema/tool.py`）
    原样 append 进 messages ⇒ 不翻译就是"工具已经真的执行了、第二轮必然 400"，
    且 400 不在 `_RETRYABLE_STATUS` 里 ⇒ 该回合永久失败。

    转写规则（有序，越靠前越保真）：

    1. 已是 dict ⇒ 原样；
    2. `str`：空串 ⇒ `{}`；否则 `json.loads` ⇒ 是 dict 就用；
    3. 其余（非 dict 的 JSON 值 / 解不开的串 / None）⇒ `{}` + 一次 warning。

    第 3 条刻意"降级而**不**打死回合"：仓库对畸形 tool_call 的既有处置是
    「跳过并 warning」（`normalize_tool_calls` 的同名先例），而这条消息只是**上下文回放**
    （工具结果文本在紧随其后的 `role="tool"` 消息里，模型仍能看到）。把 `{}` 发出去
    语义上等于"这个调用没带参数"，比"整轮 400、用户永远拿不到回复"更轻。
    """
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "tool_call arguments 不是合法 JSON，已按空对象回放（model=%s）：%.120s",
                model, stripped)
            return {}
        if isinstance(parsed, dict):
            return parsed
        logger.warning(
            "tool_call arguments 是 %s 而非对象，已按空对象回放（model=%s）：%.120s",
            type(parsed).__name__, model, stripped)
        return {}
    if arguments is None:
        return {}
    logger.warning(
        "tool_call arguments 类型意外（%s），已按空对象回放（model=%s）",
        type(arguments).__name__, model)
    return {}


def _to_native_message(message: dict, *, model: str = "") -> dict:
    """OpenAI 形状的一条 message → native 形状。

    - `content` 是字符串：原样。
    - `content` 是块列表：`text` 块拼成 content，`image_url` 块（data URI 或裸 base64）
      收进 native 的 `images[]`。当前 `providers.yaml::llm.ollama.supports_multimodal`
      为 `false`，上层因此**不会**把图片块发到这里——本函数只是**忠实转写**，
      不声称新增多模态能力。
    - `tool_calls`：**必须**把 `function.arguments` 从 JSON 字符串转回对象
      （native 对字符串直接 400，见 `_native_tool_arguments`）；`id` / `type` / `index`
      原样保留（native 忽略多余键，实测）。
    - `tool_call_id` / `tool_name`（`role="tool"` 的结果消息）**透传**——native 认得这两键
      （实测 body 解码通过），丢掉它们等于把"哪次调用的结果"这一信息删掉。
    - `prefix`（DeepSeek 续写专用）与其它非 native 字段**丢弃**：native 无对应语义
      （`prefix` 在此之前已被 `_build_payload` 显式拒绝）。
    """
    role = message.get("role") or "user"
    content = message.get("content")
    images: list[str] = []
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                texts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                texts.append(str(block.get("text", "")))
            elif block_type == "image_url":
                url = (block.get("image_url") or {}).get("url") or ""
                if url.startswith("data:"):
                    _, _, payload = url.partition(",")
                    if payload:
                        images.append(payload)
                elif url:
                    images.append(url)
        text: Any = "\n".join(t for t in texts if t)
    elif content is None:
        text = ""
    else:
        text = str(content)

    native: dict = {"role": role, "content": text}
    if images:
        native["images"] = images
    tool_calls = message.get("tool_calls")
    if tool_calls:
        translated: list[dict] = []
        for call in tool_calls:
            function = dict(call.get("function") or {})
            function["arguments"] = _native_tool_arguments(
                function.get("arguments"), model=model)
            entry = dict(call)
            entry["function"] = function
            translated.append(entry)
        native["tool_calls"] = translated
    for key in ("tool_call_id", "tool_name"):
        if message.get(key):
            native[key] = message[key]
    return native


class OllamaBackend(LLMBackend):
    """Ollama native `/api/chat` 后端（runtime context authority 的落实点）。"""

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        base_url: str = "",
        context_window: int | None = None,
        keep_alive: str | int | None = None,
        logger: logging.Logger | None = None,
        http_client: httpx.AsyncClient | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        """初始化 Ollama native 后端。

        Args:
            model: 模型名（如 "qwen2.5:3b"）。
            api_key: 远端 gateway 的凭据。空串时与既有 OpenAI 兼容路径同样兜底成
                "ollama"（本地 Ollama 接受任意非空值）——**不做第二套默认**，也不
                在远端形态下丢认证头（§86）。
            base_url: 解析后的 base_url（可以是 `.../v1` 形态），经
                `_native_base_url` 归一到 native 根。
            context_window: **显式声明**的 effective context window（token）。
                `None`/`<=0` ⇒ 不申请运行时分配（OL2）。**不接受**通用预算回退值。
            keep_alive: native `keep_alive` 的内部转写钩子。默认 `None` ⇒ **不发该键**
                （由 Ollama 自己的默认管辖）——本批**不**新增用户可见配置项（§26-C），
                该形参只服务于内部/测试对 runner 生命周期的显式控制。
            logger: 日志记录器。
            http_client: 出站守卫挂载点（与 `OpenAICompatBackend` 同形的注入点）——
                仅作用于 native 请求路径；测试用它注入 `MockTransport`。
            http_client_factory: 同上的工厂形态（reconnect 时重建全新 client）。
        """
        key = api_key or "ollama"  # 与 OpenAI 兼容后端同一句兜底，本地 Ollama 接受任意非空值
        self._model = model
        self._api_key = key
        self._base_url = base_url
        self._native_base = _native_base_url(base_url)
        self._context_window = context_window
        self._keep_alive = keep_alive
        self._logger = logger or logging.getLogger(__name__)
        self._http_client_factory = http_client_factory
        self._injected_client: httpx.AsyncClient | None = http_client
        if http_client_factory is not None and http_client is None:
            self._injected_client = http_client_factory()
        self._extra_drop_warned = False
        self._compat_client: Any | None = None  # OpenAI 兼容侧车（懒创建，raw_client 用）
        self._closed = False
        self.last_usage: dict | None = None  # 最近一次调用的归一化 token 消耗
        self._create_clients()

        async def _health_check() -> bool:
            # 连通性探活**不**申请运行时分配：`/api/providers/test` 与 `--full` 是
            # "远端此刻可用吗"（PC 家族），不是一次预算过的 generation；把
            # `num_ctx` 捎上去会让一次探活触发大额显存分配（小显存机器上可能直接 OOM）。
            payload = {
                "model": self._model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "options": {"num_predict": 1},
            }
            data = await self._post_chat(payload, timeout=5.0)
            return "message" in data or data.get("done") is True

        self._test_fn = make_test_method(_health_check)

    # ---- 诊断面（configured / requested / observed；**绝不进热路径**）----

    @property
    def configured_context(self) -> int | None:
        """Elysia 侧**声明**的 effective context window（未声明/非法 ⇒ None）。"""
        return self._context_window if (self._context_window or 0) > 0 else None

    @property
    def requested_context(self) -> int | None:
        """**generation** 请求里会带上的 `options.num_ctx`（不申请 ⇒ None）。

        与 `configured_context` 在当前实现下同值——它们是两个概念、不是两个来源：
        配置缺失时两者同时为 None，正是 OL2 的"不表态"。
        连通性探活（`test()`）刻意**不**申请分配，故不在本属性的语义内。
        """
        return self.configured_context

    async def probe_runtime_context(self) -> dict:
        """诊断：把 configured / requested / **observed** 三个真值一次列清。

        `observed` 来自 `GET /api/ps`（**只在显式调用时**发生一次网络往返）。
        失败（服务不可达、模型未加载、响应形状意外）**不抛**：返回
        `observed=None` + `error=<原因>`，绝不影响任何 generation（§68）。

        Returns:
            {"configured": int|None, "requested": int|None, "observed": int|None,
             "model": str, "error": str|None}
        """
        result: dict = {
            "configured": self.configured_context,
            "requested": self.requested_context,
            "observed": None,
            "model": self._model,
            "error": None,
        }
        try:
            response = await self._client.get(f"{self._native_base}/api/ps")
            response.raise_for_status()
            models = response.json().get("models") or []
            result["observed"] = _observed_context(models, self._model)
        except Exception as exc:  # 诊断失败只记原因——不抛、不影响正常路径
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    # ---- 生成入口（generic LLMBackend 契约，签名与语义不变）----

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """非流式对话 — native `/api/chat`（`stream: false`），返回完整文本。"""
        payload = self._build_payload(messages, stream=False, kwargs=kwargs)
        data = await self._post_chat(payload)
        self.last_usage = _usage_from_native(data)
        if "message" not in data:
            # 形状意外的 200（既不是 error 帧、也没有 message）**不**折成空串：通用后端
            # 对空 choices 也是显式抛错（O2 的教训——"空白回复"调用方无法感知）。注意
            # `content: ""` 是合法的（例如只回 tool_calls 的响应），不在此列。
            raise RuntimeError(
                f"Ollama 返回意外响应（无 message 字段）— 模型={self._model} "
                f"keys={sorted(data)}")
        message = data.get("message") or {}
        return message.get("content") or ""

    async def chat_stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        """流式对话 — 逐 chunk 产出文本（tool_calls 由 `stream_think` 消费）。"""
        # `aclosing`：消费者提前 `aclose()` / 任务取消时，**当场**把内层生成器关掉
        # （否则要等 asyncgen finalizer，HTTP 流会晚一拍才释放——Challenger B3）。
        async with aclosing(self._stream_structured(messages, **kwargs)) as stream:
            async for chunk in stream:
                if chunk.get("content"):
                    yield chunk["content"]

    async def stream_think(
        self, messages: list[dict], **kwargs: Any
    ) -> AsyncIterator[dict]:
        """流式思考 — 结构化 chunk（text / tool_calls / 终帧 usage），保持既有契约。"""
        async with aclosing(self._stream_structured(messages, **kwargs)) as stream:
            async for chunk in stream:
                yield chunk

    # ---- 内部：请求构造与传输 ----

    def _build_payload(self, messages: list[dict], *, stream: bool,
                       kwargs: dict) -> dict:
        """构造 native `/api/chat` 请求体（**唯一的 wire 构造点**，便于单测）。

        转写规则（每条都有实测依据，见模块 docstring）：

        - `num_ctx`：**只**来自显式声明的 `context_window`（>0）。未声明/非法一律不发。
        - `max_tokens` → `options.num_predict`、`temperature` → `options.temperature`
          （native 的真实对应字段；native 不认 `max_tokens`/`temperature` 顶层键）。
        - `tools` 原样透传（native 接受 OpenAI function 形状，实测可用）。
        - `tool_choice` / `stream_options` / `response_format` / `extra_body`：
          native **无对应语义**，一律丢弃——不发明语义，也不把 DeepSeek 专属字段
          塞进 Ollama 请求体（丢弃 `extra_body` 时打一次 warning，与通用 backend
          丢弃 `thinking` 的可见性对齐）。
        """
        options: dict[str, Any] = {}
        requested = self.requested_context
        if requested is not None:
            options["num_ctx"] = requested

        if any(m.get("prefix") for m in messages):
            # prefix continuation 是 DeepSeek 专属能力（`providers.yaml::supports_prefix`
            # 对 ollama 为 false）。**显式拒绝**，与 OpenAI 兼容后端同一错误契约——
            # 悄悄丢掉 `prefix` 字段会静默改变语义：调用方要求"接着这段 JSON 续写"，
            # 丢掉后变成"重新回答"，而两者都返回 200。
            raise ValueError(
                f"prefix continuation is not supported by this backend (model={self._model})")

        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        temperature = kwargs.get("temperature")
        if temperature is not None:
            options["temperature"] = temperature

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_native_message(m, model=self._model) for m in messages],
            "stream": stream,
        }
        if options:
            payload["options"] = options
        if self._keep_alive is not None:
            payload["keep_alive"] = self._keep_alive
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = tools

        dropped = [k for k in ("tool_choice", "extra_body", "response_format",
                               "stream_options") if kwargs.get(k)]
        if dropped and not self._extra_drop_warned:
            self._extra_drop_warned = True
            self._logger.warning(
                "Ollama native 协议无对应字段，已丢弃请求参数 %s（model=%s）；"
                "native 的对应语义见 Module/llm/ollama.py 的 _build_payload",
                dropped, self._model)
        return payload

    async def _post_chat(self, payload: dict, *, timeout: httpx.Timeout | float | None = None) -> dict:
        """单次 native `/api/chat`（非流式）。HTTP 状态错误 ⇒ `httpx.HTTPStatusError`。

        **错误契约与通用后端一致**：`HTTPStatusError` 带 `.response.status_code`，
        故 `LLMAdapter._extract_status` 的重试映射（429/5xx 可重试，404 不可）不变；
        连接失败仍是 `httpx.ConnectError`（无 status ⇒ 不重试），与
        `openai.APIConnectionError` 同层。实测 native 错误形态：未知模型 **404**
        `{"error": "model 'x' not found"}`、坏 JSON **400**。
        """
        response = await self._client.post(
            f"{self._native_base}/api/chat",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout if timeout is not None else _TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            # 极少数形态：200 + {"error": ...}（流式错误帧也是这个形状）。
            # 用 httpx 家族异常而**不是**裸 RuntimeError：`core/cognitive/loop.py::_is_llm_layer_error`
            # 按模块前缀把 httpx/openai 判为 provider 层，裸 RuntimeError 会被归成
            # INTERNAL（"内部错误"，不可重试）——那是**归因错误**（§61：映射到既有
            # generic 错误契约，不重做 taxonomy）。语义也贴切：服务端在协议内报了错。
            raise httpx.RemoteProtocolError(f"Ollama 返回错误：{data['error']}")
        return data

    async def _stream_structured(
        self, messages: list[dict], **kwargs: Any
    ) -> AsyncGenerator[dict, None]:
        """NDJSON 流式调用 —— 逐帧翻译成通用 chunk。

        帧契约（实测 0.24.0）：
          - 普通帧：`{"message": {"content": "..."}, "done": false}`
          - 终帧：`done=true` + `done_reason` / `prompt_eval_count` / `eval_count` / 各 duration
          - 工具调用：**完整**对象出现在某一帧（`function.arguments` 是 dict）

        usage **只在终帧发射一次**（OL5）：native 只有终帧带计数，且本函数在终帧后
        立即 `return`，故不存在重复计数；流在终帧前断开时**不编造** usage。
        """
        payload = self._build_payload(messages, stream=True, kwargs=kwargs)
        async with self._client.stream(
            "POST", f"{self._native_base}/api/chat", json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    # 同上：provider 在流内报错 ⇒ provider 层归因（不是 INTERNAL）
                    raise httpx.RemoteProtocolError(
                        f"Ollama 返回错误：{data['error']}")
                message = data.get("message") or {}
                tool_calls = _to_openai_tool_calls(message.get("tool_calls"))
                content = message.get("content") or None
                if data.get("done"):
                    usage = _usage_from_native(data)
                    if usage is not None:
                        self.last_usage = usage
                    if content or tool_calls or usage is not None:
                        frame: dict[str, Any] = {"content": content,
                                                 "tool_calls": tool_calls}
                        if usage is not None:
                            frame["usage"] = usage
                        yield frame
                    return
                if content or tool_calls:
                    yield {"content": content, "tool_calls": tool_calls}

    # ---- 生命周期 ----

    @property
    def raw_client(self) -> Any:
        """OpenAI 兼容**侧车**客户端（`LLMBackend.raw_client` 的实现）。

        消费方是 `WorkflowEngine` Planner/Executor（`chat.completions.create(...)`，
        OpenAI SDK 形状）——那是本批**范围外**的另一个消费面，故这里保持修复前的
        形状与地址（从同一个 native 根派生的 `{root}/v1`）。懒创建：正常 generation
        路径不产生第二个 HTTP client，也不影响 1 generation → 1 provider request。
        """
        if self._closed:
            # `close()` 之后再取 raw_client 不得**新建**一个无人持有的连接
            # （Challenger C4：旧行为会让它一直活到下一次 close）。语义用既有的
            # "不可用 ⇒ None"（`LLMBackend.raw_client` 的文档契约），调用方
            # （DelegationPreparer）据此走既有降级路径。
            return None
        if self._compat_client is None:
            from openai import AsyncOpenAI

            key = self._api_key
            self._compat_client = AsyncOpenAI(
                base_url=f"{self._native_base}/v1",
                api_key=key,
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._compat_client

    async def test(self) -> dict:
        """测试连通性：native 最小请求（`num_predict: 1`），5 秒超时。"""
        return await self._test_fn(self)

    async def close(self) -> None:
        await self._client.aclose()
        if self._compat_client is not None:
            await self._compat_client.close()
            self._compat_client = None
        self._closed = True

    async def reconnect(self) -> None:
        """重建 HTTP 客户端（服务重启后恢复）——幂等，并解除 close 状态。"""
        self._closed = False
        try:
            await self._client.aclose()
        except Exception:
            self._logger.warning("reconnect: 关闭旧 HTTP client 失败", exc_info=True)
        if self._compat_client is not None:
            try:
                await self._compat_client.close()
            except Exception:
                self._logger.warning("reconnect: 关闭兼容侧车失败", exc_info=True)
            self._compat_client = None
        if self._http_client_factory is not None:
            self._injected_client = self._http_client_factory()
        self._create_clients()

    def _create_clients(self) -> None:
        """创建/重建 native httpx client（注入 client 优先，供守卫与测试使用）。"""
        if self._injected_client is not None:
            self._client = self._injected_client
        else:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)


def _observed_context(models: list[dict], model: str) -> int | None:
    """从 `/api/ps` 的 models[] 里取出**这个**模型的 `context_length`（找不到 ⇒ None）。

    匹配规则（Challenger 2026-09-19 的 A2：此前的"去 tag 比家族名"过松——
    `_observed_context([{"name": "qwen2.5:3b", ...}], "qwen2.5:14b")` 会返回 **3b 的窗口**，
    于是一个"看起来匹配/看起来不匹配"的观测值都可能是别人的）：

    - 精确同名 ⇒ 命中；
    - **仅当**请求侧模型名不带 tag 时，接受 `"<同族>:latest"`（Ollama 的默认 tag 语义：
      `qwen2.5` ≡ `qwen2.5:latest`）；
    - 其余一律 None——**宁可没有观测值，也不认错模型**（观测值是唯一能暴露
      configured≠served 的真值，认错模型比没有更糟）。
    """
    family, _, tag = model.partition(":")
    for entry in models:
        name = str(entry.get("name") or "")
        entry_family, _, entry_tag = name.partition(":")
        if name == model or (not tag and entry_family == family and entry_tag == "latest"):
            value = entry.get("context_length")
            return int(value) if value else None
    return None
