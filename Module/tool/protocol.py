"""Tool use 模块 — 跨 Provider 的工具定义与执行结果。

核心抽象:
  ToolParameter: 单个参数定义，从 type hints 或 Pydantic model 推导
  ToolDefinition: 工具的完整定义，可导出为 OpenAI/Anthropic/Gemini 格式
  ToolResult:     工具执行结果，统一包装成功/失败/超时/截断
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Coroutine, Protocol, AsyncIterator

from Module._common.error_type import ErrorType

ToolFunc = Callable[..., Coroutine[Any, Any, Any]]


class ToolResultType(StrEnum):
    """工具产出类型——跨 Provider 的中性结果分类（原 ResultType Literal 枚举化）。

    值与原 ResultType Literal 完全对齐，消除裸字符串散落。
    """
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    MIXED = "mixed"


class ToolStatus(StrEnum):
    """工具执行进度状态——ToolProgress.status 枚举化（原 Literal 枚举化）。"""
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# PluginContext — 类型化依赖注入
# ---------------------------------------------------------------------------

@dataclass
class PluginContext:
    """类型化的依赖注入容器 — 插件 setup() 时注入。"""

    session_id: str = ""
    # 数据库
    memory: Any = None             # LongTermMemory | None
    mongodb: Any = None            # DocumentDB | None
    milvus: Any = None             # MilvusClient | None
    # 网络
    http_client: Any = None        # httpx.AsyncClient | None
    # 调度
    scheduler: Any = None          # TaskScheduler | None
    # 配置
    config: Any = None             # AppConfig | None


# ---------------------------------------------------------------------------
# ToolContext — 运行时上下文
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """每次工具调用的运行时上下文 — 支持多 Agent 链路追踪。

    调用方（Agent）创建此对象并传入 execute()，
    工具函数通过 ctx 参数获取所需资源。
    """

    session_id: str = ""
    user_id: str = ""
    trace_id: str = ""              # 一次请求的全局追踪 ID（跨 Agent、跨工具串联）
    agent_id: str = "default"       # 触发该工具的 Agent 实例标识
    services: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        """获取已注册的服务。

        Args:
            name: 服务名称。
            default: 未找到时的默认值。

        Returns:
            已注册的服务实例或 default。
        """
        return self.services.get(name, default)


# ---------------------------------------------------------------------------
# ToolProgress — 长任务进度（预留接口）
# ---------------------------------------------------------------------------

@dataclass
class ToolProgress:
    """工具执行过程中的中间进度。

    通过 ToolEmitter.progress() 或 async generator yield 上报。
    execute_stream() 实时产出这些事件给调用方。

    序列化要求: message 必须是 str，data 必须是可 JSON 序列化的基础类型
    （str/int/float/bool/list/dict），不要传入 Python 对象或 bytes。
    """

    status: ToolStatus = ToolStatus.PROCESSING
    percent: float | None = None   # 0.0 ~ 1.0
    message: str = ""
    data: Any = None               # 附加数据 — 必须可 JSON 序列化


# ---------------------------------------------------------------------------
# ToolEmitter — 进度上报器（对 LLM 隐藏的参数）
# ---------------------------------------------------------------------------

@dataclass
class ToolEmitter:
    """工具进度上报器 — 运行时注入，对 LLM 不可见。

    工具通过 emit.progress() 上报中间进度，执行完成后正常 return ToolResult。

    用法:
        @tool(description="深度扫描", timeout=120.0)
        async def deep_scan(scope: str, emit: ToolEmitter) -> ToolResult:
            await emit.progress(0.3, "检查内存中...")
            await emit.progress(0.6, "扫描进程中...")
            return ToolResult.ok("scan", "扫描完成: 无异常", 1500.0)
    """

    _queue: Any = field(default=None, repr=False)  # asyncio.Queue, set by execute_stream
    _task: Any = field(default=None, repr=False)    # asyncio.Task, set by execute_stream

    async def progress(
        self, percent: float | None = None, message: str = "", data: Any = None,
    ) -> None:
        """向 execute_stream() 调用方发送进度事件。

        如果不在 execute_stream() 上下文中（即普通 execute()），此方法是 no-op。

        Args:
            percent: 进度百分比（0.0 ~ 1.0），可为 None。
            message: 进度描述信息。
            data: 附加数据，必须可 JSON 序列化。

        Returns:
            None
        """
        if self._queue is not None:
            await self._queue.put(ToolProgress(
                status=ToolStatus.PROCESSING, percent=percent,
                message=message, data=data,
            ))

    def is_cancelled(self) -> bool:
        """检查当前工具执行是否已被取消。

        CPU 密集型循环中可定期调用此方法，及时响应外部打断。

        用法:
            for item in large_dataset:
                if emit.is_cancelled():
                    return ToolResult.fail(...)
                process(item)

        Returns:
            True 如果当前工具执行已被取消。
        """
        return self._task is not None and self._task.cancelled()


# ---------------------------------------------------------------------------
# ToolParameter — 单个参数定义
# ---------------------------------------------------------------------------

class ToolParameter:
    """工具参数的中间表示 — 从 type hints 或 Pydantic model 推导而来。

    统一处理两种来源后，由 ToolDefinition._build_json_schema() 组装为标准 JSON Schema。
    使用普通类而非 Pydantic BaseModel，避免对调用方引入 pydantic 依赖。
    """

    __slots__ = (
        "type", "description", "required", "default", "default_set",
        "enum", "properties", "items",
        "minimum", "maximum", "pattern",
    )

    def __init__(
        self,
        type: str = "string",
        description: str = "",
        required: bool = False,
        default: Any = None,
        default_set: bool = False,
        enum: list[str] | None = None,
        properties: dict[str, "ToolParameter"] | None = None,
        items: "ToolParameter | None" = None,
        minimum: int | float | None = None,
        maximum: int | float | None = None,
        pattern: str | None = None,
    ):
        """初始化工具参数定义。

        Args:
            type: JSON Schema 类型（"string", "integer", "number", "boolean", "object", "array" 等）。
            description: 参数描述，会展示给 LLM。
            required: 是否为必填参数。
            default: 默认值。
            default_set: True 表示 default=None 是有意设置的（Optional 参数），而非未提供默认。
            enum: 允许的枚举值列表。
            properties: object 类型的子属性映射。
            items: array 类型的元素类型定义。
            minimum: 数值最小值。
            maximum: 数值最大值。
            pattern: 字符串正则约束。
        """
        self.type = type
        self.description = description
        self.required = required
        self.default = default
        self.default_set = default_set       # True 表示 default=None 是有意设置的（Optional 参数）
        self.enum = enum
        self.properties = properties          # object type 的子属性
        self.items = items                    # array type 的元素类型
        self.minimum = minimum
        self.maximum = maximum
        self.pattern = pattern

    def to_json_schema_property(self) -> dict:
        """转换为单个 JSON Schema property（不含 name key）。"""
        schema: dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.enum:
            schema["enum"] = self.enum
        if self.default_set:
            schema["default"] = self.default
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.pattern:
            schema["pattern"] = self.pattern
        if self.type == "object" and self.properties:
            schema["properties"] = {
                k: v.to_json_schema_property() for k, v in self.properties.items()
            }
            required = [k for k, v in self.properties.items() if v.required]
            if required:
                schema["required"] = required
        if self.type == "array" and self.items:
            schema["items"] = self.items.to_json_schema_property()
        return schema


# ---------------------------------------------------------------------------
# ToolDefinition — 工具定义
# ---------------------------------------------------------------------------

@dataclass
class ToolDefinition:
    """工具的完整定义 — 跨 Provider 的中性格式。

    to_openai() / to_anthropic() / to_gemini() 按需转换为各 provider 格式。
    func=None 表示未绑定可执行函数（仅用于 schema 导出）。
    """

    name: str
    description: str
    parameters: dict[str, ToolParameter] = field(default_factory=dict)
    required: list[str] | None = None
        # None = 自动从参数推导 required。[] = 全部可选。非空 = 显式指定。
    func: ToolFunc | None = None          # 异步 callable，None 表示未绑定
    timeout: float = 30.0                 # 执行超时（秒）
    max_result_len: int = 4000            # 结果最大字符数

    # 工具属性声明
    result_type: ToolResultType = ToolResultType.TEXT      # 工具产出类型
    requires: list[str] = field(default_factory=list)
        # 声明所需资源/能力: "web_search", "database:read", "network", "filesystem"
    side_effects: bool = False
        # True → 会修改外部状态，不可安全 cancel/重试
    progress_timeout: float = 0.0
        # 流式执行时 emit.progress() 间最大沉默间隔（秒）。0 = 不启用。
        # 超过此时间无 progress 事件 → 认为工具死锁，取消执行。

    # 内部使用
    _hidden_params: set[str] = field(default_factory=set)
        # 对 LLM 隐藏的参数名（如 ctx）— 不输出到 JSON Schema
    _ctx_required: bool = False
        # True → ctx 参数无默认值，execute() 时必须提供，否则 Fail Fast
    _schema: dict | None = field(default=None, repr=False)
        # 预构建的 JSON Schema — 设置后 _build_json_schema() 直接返回，跳过 ToolParameter 推导

    # ---- Provider-specific export ----

    def to_openai(self) -> dict:
        """OpenAI function-calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._build_json_schema(),
            },
        }

    def to_anthropic(self) -> dict:
        """Anthropic tool use 格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._build_json_schema(),
        }

    def to_gemini(self) -> dict:
        """Google Gemini function declaration 格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._build_json_schema(),
        }

    # ---- Internal ----

    def _build_json_schema(self) -> dict:
        """将 parameters + required 组装为标准 JSON Schema object。

        _hidden_params 中的参数不出现在 schema 中（对 LLM 不可见）。
        若 _schema 已设置则直接返回（Pydantic 模型路径）。

        Returns:
            标准 JSON Schema object（type="object", properties, required）。
        """
        if self._schema is not None:
            return self._schema

        properties: dict[str, dict] = {}
        required_list: list[str] = []

        for name, param in self.parameters.items():
            if name in self._hidden_params:
                continue
            properties[name] = param.to_json_schema_property()
            if param.required and self.required is None:
                required_list.append(name)

        # self.required: None=自动推导, []=全部可选, 非空=显式指定
        if self.required is not None:
            required_list = list(self.required)

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required_list:
            schema["required"] = required_list
        return schema


# ---------------------------------------------------------------------------
# ToolPlugin — 工具插件基类
# ---------------------------------------------------------------------------

class ToolPlugin:
    """工具插件基类 — 每个工具一个子类。

    插件开发约束:
    - 模块级无状态（Stateless at Module Level）：所有持久化状态必须依托于
      PluginContext 或 self 属性，禁止模块级全局变量或静态类属性。
    - execute_stream 的 finally 块禁止耗时异步清理（如 HTTP 请求），
      此类清理应在 teardown() 中完成。
    """

    # 元数据（子类必须定义）
    name: str = ""
    description: str = ""
    param_model: type | None = None  # Pydantic BaseModel subclass
    prompt_hint: str = ""  # 注入 prompt 的用法提示（何时用/不要用于/特殊注意）

    # 能力与安全声明（子类覆盖）
    capabilities: set = set()
    safety: Any = None  # SafetyProfile — 子类覆盖

    # Runtime 感知元数据（BE-040，批次 4 新增）
    runtime: str = "backend"  # "backend" | "desktop" | "local_worker"（local_worker 无实现，枚举保留）
    version: str = "1.0.0"    # 显式版本，schema 变更时手动 bump（D-f）
    risk_level: str = "safe"  # "safe" | "medium" | "high"（D-g）
    output_schema: dict = {"type": "string", "description": "工具公开输出"}  # 声明性描述，批次 4 内无强校验

    # ---- 派生属性（BE-040） ----

    @property
    def approval_authority(self) -> str:
        """审批权威 — Desktop/Local Worker 工具由客户端本地授权，其余走后端（D-h）。

        Returns:
            "client"（runtime 为 desktop/local_worker）或 "backend"。
        """
        return "client" if self.runtime in ("desktop", "local_worker") else "backend"

    @property
    def permission_required(self) -> bool:
        """是否需要权限确认 — 非 safe 风险或声明了人工审批（D-i）。

        Returns:
            True 当 risk_level != "safe" 或 safety.requires_human_approval 为真。
        """
        return self.risk_level != "safe" or bool(
            self.safety and self.safety.requires_human_approval
        )

    # ---- 生命周期 ----

    async def setup(self, ctx: PluginContext) -> None:
        """初始化插件。

        Args:
            ctx: 依赖注入容器，包含 memory、mongodb、milvus、http_client 等资源。

        Raises:
            Exception: 初始化失败时抛出异常，工具将不会被注册。
        """

    async def teardown(self) -> None:
        """清理资源 — 关闭连接池、释放锁等。

        Returns:
            None
        """

    # ---- 执行 ----

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: Any = None,
    ) -> Any:
        """执行工具（非流式）。

        Args:
            ctx: 运行时上下文，包含 session_id、trace_id 等。
            emit: 进度上报器（ToolEmitter），用于 report 中间进度。
            params: 工具参数，通常为 Pydantic Model 实例或 None。

        Returns:
            ToolResult: 工具执行结果。

        Raises:
            NotImplementedError: 子类必须实现。
        """
        raise NotImplementedError

    async def execute_stream(
        self, *, ctx: ToolContext, emit: ToolEmitter, params: Any = None,
    ) -> AsyncIterator[ToolProgress | ToolResult]:
        """流式执行工具。

        Args:
            ctx: 运行时上下文，包含 session_id、trace_id 等。
            emit: 进度上报器（ToolEmitter），用于 report 中间进度。
            params: 工具参数，通常为 Pydantic Model 实例或 None。

        Yields:
            ToolProgress | ToolResult: 中间进度事件或最终执行结果。
        """
        raise NotImplementedError
        yield  # make this an async generator


# ---------------------------------------------------------------------------
# ToolExecutor — 接口抽象（CognitiveLoop 依赖此 Protocol）
# ---------------------------------------------------------------------------

class ToolExecutor(Protocol):
    """CognitiveLoop 通过此接口使用工具系统 — 接口抽象，不依赖具体实现。"""

    async def execute(
        self, name: str, *, ctx: ToolContext, **raw_params: Any,
    ) -> ToolResult: ...

    async def execute_stream(
        self, name: str, *, ctx: ToolContext, emit: ToolEmitter, **raw_params: Any,
    ) -> AsyncIterator[Any]: ...

    def definitions(self, format: str = "openai") -> list[dict]: ...

    async def cancel_all(self) -> None: ...

    def execute_now(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# MediaItem — 工具返回的媒体数据
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """工具返回的媒体数据 — URL 或 bytes 二选一（URL 优先）。

    用于 ToolResult.images / .audio / .files 字段。
    """

    url: str = ""               # 远程 URL（优先使用）
    data: bytes = b""           # 原始二进制数据（小文件）
    mime_type: str = ""         # "image/png", "audio/wav", "application/pdf"
    label: str = ""             # 人类可读标签


# ---------------------------------------------------------------------------
# ToolResult — 执行结果
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """工具执行结果。success=False 时 error 包含失败原因。

    文本工具（向后兼容）: content 可直接注入 LLM messages。
    多模态工具: content 展示给用户，llm_summary 注入 LLM context。
    """

    success: bool
    content: str = ""
    error: str | None = None
    error_type: ErrorType | None = None  # 错误分类，帮助认知循环决策
    duration_ms: float = 0.0
    truncated: bool = False
    tool_name: str = ""

    # 结果类型 + 媒体数据
    result_type: ToolResultType = ToolResultType.TEXT
    images: list[MediaItem] = field(default_factory=list)
    audio: MediaItem | None = None
    files: list[MediaItem] = field(default_factory=list)

    # 给 LLM 看的文本描述（result_type != "text" 时使用）
    # 例: 图片工具 → llm_summary="图表显示 Q1 营收增长 30%" 注入 LLM
    llm_summary: str = ""

    # 异步任务 ID（submit_task 等 fire-and-forget 工具）
    # 非空表示此工具调用了后台任务，CognitiveLoop 检测后结束当前回合
    async_task_id: str | None = None

    # ---- Factory methods ----

    @classmethod
    def ok(
        cls, name: str, content: str, duration_ms: float, truncated: bool = False,
        result_type: ToolResultType = ToolResultType.TEXT, llm_summary: str = "",
        images: list[MediaItem] | None = None,
        audio: MediaItem | None = None,
        files: list[MediaItem] | None = None,
        async_task_id: str | None = None,
    ) -> "ToolResult":
        """创建成功结果。

        非 text 类型的结果应提供 llm_summary，
        确保不支持多模态的模型也能从文本描述理解结果。
        """
        return cls(
            success=True, content=content, tool_name=name,
            duration_ms=duration_ms, truncated=truncated,
            result_type=result_type, llm_summary=llm_summary,
            images=images or [], audio=audio, files=files or [],
            async_task_id=async_task_id,
        )

    @classmethod
    def fail(
        cls, name: str, error: str, duration_ms: float = 0.0,
        error_type: ErrorType | None = None,
    ) -> "ToolResult":
        """创建失败结果。"""
        return cls(
            success=False, tool_name=name, error=error,
            duration_ms=duration_ms, error_type=error_type,
        )

    # ---- Helpers ----

    @property
    def llm_content(self) -> str:
        """返回应注入 LLM messages 的内容。

        success=True:
          result_type="text" → content
          其他类型 → llm_summary（若为空则 fallback 到 content）

        success=False — 自动组装 LLM 纠错提示:
          INVALID_ARGUMENT → "...invalid arguments...review...and retry"
          TIMEOUT          → "...timed out...retry with a smaller scope"
          UNAVAILABLE      → "...unavailable...Do not retry this action"
          other            → "...failed. Do not retry with the same arguments"

        Returns:
            应注入 LLM context 的文本内容。
        """
        if not self.success:
            return self._format_error_for_llm()

        base = self.content if self.result_type == "text" else (self.llm_summary or self.content)

        if self.truncated:
            truncated_notice = (
                f"[结果已截断，原文 {len(self.content)} 字符，仅展示前 {len(base)} 字符。"
                f"如需完整数据，请使用更精确的参数重试。]\n\n"
            )
            return truncated_notice + base
        return base

    def _format_error_for_llm(self) -> str:
        """将错误信息格式化为 LLM 可理解的提示。

        Returns:
            根据 error_type 生成的 LLM 纠错指引文本。
        """
        if self.error_type == ErrorType.INVALID_ARGUMENT:
            return (
                f"Action '{self.tool_name}' failed due to invalid arguments. "
                f"Error: {self.error}. "
                f"Please review the tool's parameter schema and retry with corrected arguments. "
                f"If this error persists after 2 retries, abandon this approach and try a different tool or inform the user."
            )
        if self.error_type == ErrorType.TIMEOUT:
            return (
                f"Action '{self.tool_name}' timed out. "
                f"Error: {self.error}. "
                f"You may retry with a smaller scope, or try a different approach."
            )
        if self.error_type == ErrorType.UNAVAILABLE:
            return (
                f"Action '{self.tool_name}' cannot be completed because a required service "
                f"is currently unavailable. Error: {self.error}. "
                f"Do not retry this action. Try an alternative approach or inform the user."
            )
        # TOOL_ERROR、CANCELLED、UNKNOWN_TOOL，或没有 error_type
        return (
            f"Action '{self.tool_name}' failed. "
            f"Error: {self.error or 'Unknown error'}. "
            f"Do not retry with the same arguments."
        )
