"""业务 REST 契约建模：/api/providers、/api/images、/api/chat、/api/messages 响应模型（零依赖）。

对照 `core/input/api_routes.py` 实际 wire 逐字段对齐：
- /api/providers：provider 名是动态 key → `dict[str, ProviderEntry]`；custom/unconfigured 顶层 dict。
- /api/providers/test 两 level 形状不同：
    usable → `{ok, latency_ms, detail}`；connectivity → `backend.test()` 的 `{ok, latency_ms, error?}`
  （error 仅失败带、成功缺省）→ `ProviderTestResult{ok, latency_ms, error?, detail?}` 并集——
  error/detail 均建模为可缺省，否则某 level 的键会被 pydantic 忽略 → wire 丢字段。
- /api/images 错误响应是 `{"error": ...}`（**无 ok 键**，400/413/415/500）→ `ImageUploadError` 独立模型，
  不可套用 ApiFail。
- 通用成功/失败：`ApiOk{ok: Literal[True]=True, data?}` / `ApiFail{ok: Literal[False], error}`。
- /api/messages：`MessagesResponse{messages, has_more, total, error?}`；契约视图 `Message` wire 形状自建
  （不引 core.schema.Message），发送点把 core.schema.Message 经 `_archive_to_client` 转成客户端 dict 后
  构造本模型（block_index → content_type 归一化，对齐 session 回放 wire）。
"""
from typing import Any, Literal, Optional

from pydantic import ConfigDict, Field

from frontend_contract.base import ContractModel


# ────────────────────── Provider 管理（/api/providers） ──────────────────────


class ProviderEntry(ContractModel):
    """单个已配置 provider（models 为可选用模型列表）。"""

    models: list[str] = Field(description="可用模型列表")


class CustomProvider(ContractModel):
    """自定义 provider 条目（custom_providers.yaml 持久化形状）。"""

    name: str = Field(description="provider 名称")
    base_url: str = Field(description="Base URL")
    api_key_env: Optional[str] = Field(default=None, description="API Key 环境变量名，可缺省")
    models: Optional[list[str]] = Field(default=None, description="模型列表，可缺省")


class UnconfiguredEntry(ContractModel):
    """未配置 provider 引导条目（env_vars 列出缺失的环境变量）。"""

    name: str = Field(description="provider 名称")
    env_vars: list[str] = Field(description="缺失的环境变量列表")


class ProvidersResponse(ContractModel):
    """/api/providers 响应：已配置 provider（动态 key）+ 自定义端点 + 未配置引导。"""

    llm: dict[str, ProviderEntry] = Field(description="已配置 LLM provider（名 → 条目）")
    stt: dict[str, ProviderEntry] = Field(description="已配置 STT provider（名 → 条目）")
    tts: dict[str, ProviderEntry] = Field(description="已配置 TTS provider（名 → 条目）")
    custom: dict[str, list[CustomProvider]] = Field(description="自定义端点（service → 条目列表）")
    unconfigured: dict[str, list[UnconfiguredEntry]] = Field(
        description="未配置 provider（service → 引导条目列表）")


# ────────────────────── 图片上传（/api/images） ──────────────────────


class ImageUploadResponse(ContractModel):
    """图片上传成功响应。"""

    url: str = Field(description="可访问的图片 URL 路径")
    mime: str = Field(description="MIME 类型")
    size: int = Field(description="图片字节数")


class ImageUploadError(ContractModel):
    """图片上传错误响应（无 ok 键，与 ApiFail 形状不同，不可套用）。"""

    error: str = Field(description="错误描述")


# ────────────────────── 通用成功/失败包装 ──────────────────────


class ApiOk(ContractModel):
    """通用成功响应（ok 恒 True）。"""

    ok: Literal[True] = Field(default=True, description="恒为 true")
    data: Optional[object] = Field(default=None, description="附加数据（更新后的配置等），可缺省")


class ApiFail(ContractModel):
    """通用失败响应（ok 恒 False + error 描述）。"""

    ok: Literal[False] = Field(default=False, description="恒为 false")
    error: str = Field(description="错误描述")


# ────────────────────── 统一错误信封（批次 1，BE-080） ──────────────────────


class ProtocolError(ContractModel):
    """统一错误信封（认证/限流/连接上限等新引入错误类，以及后续批次 generation 失败复用）。

    与 v1 `ApiFail`/`ImageUploadError` 并存——它们是不动现有 v1 契约模型；
    本模型是新错误类的标准形状。`error` 字段 = `message` 兼容别名（v1 前端读 `err.error`）。
    """

    code: str = Field(description="稳定错误码（见错误码目录）")
    message: str = Field(description="用户可理解的公开说明")
    retryable: bool = Field(default=False, description="是否可重试")
    requestId: str = Field(description="关联请求 ID（每请求独立 UUID，非三位计数器）")
    error: str = Field(description="兼容别名，构造时恒等于 message")
    details: dict[str, Any] = Field(default_factory=dict, description="公开安全详情（禁密钥/正文/路径）")


# ────────────────────── Provider 测试（/api/providers/test） ──────────────────────


class ProviderTestResult(ContractModel):
    """Provider 连通/可用测试结果（usable 与 connectivity 两 level 形状并集）。

    error/detail 均可缺省：usable 只带 detail、connectivity 只带 error（error 仅失败带）——
    若某字段建模为必填，另一 level 响应构造会校验失败；反之缺省省略保证 wire 不丢键。
    latency_ms 亦缺省：超时/异常响应只带 {ok: False, error}，不带耗时。
    """

    ok: bool = Field(description="测试是否通过")
    latency_ms: Optional[int] = Field(default=None, description="耗时毫秒，可缺省")
    error: Optional[str] = Field(default=None, description="失败原因，可缺省")
    detail: Optional[str] = Field(default=None, description="成功详情（识别文本/音频字节数等），可缺省")


# ────────────────────── 消息归档（/api/messages） ──────────────────────


class ArchivePart(ContractModel):
    """归档消息中的纯文本 part（_archive_to_client 从 text part 提取）。"""

    content: str = Field(description="文本内容")


class Message(ContractModel):
    """归档消息契约视图（wire 形状自建，不引 core.schema.Message）。

    发送点把 core.schema.Message 经 _archive_to_client 转成客户端 dict 后构造本模型：
    block_index → content_type 归一化（对齐 session 回放 wire 命名）。
    除前端消费字段（role/parts/content/content_type/inner_voice/tool_calls/token_usage）外，
    保留 _id/message_id/timestamp/session_id/metadata 及 tool 消息字段——wire 字节等价，不丢键。
    """

    model_config = ConfigDict(populate_by_name=True)  # _id(alias) 与 mongo_id 均可构造

    role: str = Field(description="消息角色（user/assistant/tool）")
    parts: list[ArchivePart] = Field(default_factory=list, description="纯文本 parts 列表")
    content: Any = Field(description="原始内容（parts dict 列表或纯文本）")
    content_type: Optional[dict[str, list[int]]] = Field(
        default=None, description="内容类型 → 序号映射，可缺省")
    inner_voice: Optional[str] = Field(default=None, description="内心独白，可缺省")
    tool_calls: Optional[list[dict[str, Any]]] = Field(
        default=None, description="工具调用列表，可缺省")
    token_usage: Optional[dict[str, Any]] = Field(default=None, description="token 统计，可缺省")
    message_id: str = Field(description="消息 ID")
    timestamp: str = Field(description="ISO 时间戳")
    session_id: str = Field(default="", description="会话 ID")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据（_mongo_id 已弹出）")
    mongo_id: Optional[str] = Field(
        default=None, validation_alias="_id", serialization_alias="_id",
        description="Mongo 归档 ID，可缺省（wire 键为 _id，Python 字段名不能以下划线开头）")
    # tool 消息字段（User/Assistant 消息缺省省略）
    tool_call_id: Optional[str] = Field(default=None, description="工具调用 ID（tool 角色），可缺省")
    tool_name: Optional[str] = Field(default=None, description="工具名（tool 角色），可缺省")
    success: Optional[bool] = Field(default=None, description="工具是否成功，可缺省")
    error: Optional[str] = Field(default=None, description="工具错误信息，可缺省")
    error_type: Optional[str] = Field(default=None, description="工具错误类型，可缺省")
    duration_ms: Optional[float] = Field(default=None, description="工具耗时毫秒，可缺省")
    truncated: Optional[bool] = Field(default=None, description="内容是否截断，可缺省")


class MessagesResponse(ContractModel):
    """消息归档分页响应。"""

    messages: list[Message] = Field(description="消息列表")
    has_more: bool = Field(description="是否还有更多")
    total: int = Field(description="总消息数")
    error: Optional[str] = Field(default=None, description="错误信息，可缺省")


# ────────────────────── 批次 3 v2 契约：多会话/历史/搜索/资源/状态（BE-030/031/032 + BE-024） ──────────────────────


class SessionSummary(ContractModel):
    """会话摘要（/api/sessions 列表条目 / 创建 / 重命名响应）。

    conversationId = 会话唯一标识（默认 "default"，新建 conv_<uuid4 hex[:12]>）；
    title 可空（创建后可无标题，首条用户消息自动取前 20 字，可被重命名覆盖）。
    """

    conversationId: str = Field(description="会话 ID")
    title: Optional[str] = Field(default=None, description="标题，可空（未命名时缺省省略）")
    messageCount: int = Field(description="会话消息条数")
    updatedAt: str = Field(description="最近更新时间（UTC ISO）")
    active: bool = Field(description="是否为当前活动会话")


class SessionListResult(ContractModel):
    """会话列表响应（命名避开 debug.SessionListResponse——同名不同类型 gen_ts 抛 ValueError）。

    cursor = keyset 游标 base64("{{updated_at}}|{{session_id}}")，无更多时缺省省略。
    """

    sessions: list[SessionSummary] = Field(description="会话摘要列表")
    has_more: bool = Field(description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="下一页游标，可缺省")


class SessionDetail(ContractModel):
    """会话详情（session + 第一页消息 + active）。"""

    session: SessionSummary = Field(description="会话摘要")
    messages: list[Message] = Field(description="会话历史第一页（倒序）")
    active: bool = Field(description="是否为当前活动会话")


class SessionActivateResult(ContractModel):
    """会话激活响应（REST /activate 只切 active，回放走 WS activate_session）。"""

    activeConversationId: str = Field(description="激活后的活动会话 ID")


class MessagesPageResponse(ContractModel):
    """会话内历史分页响应（before/after 两种游标复用同形状）。"""

    messages: list[Message] = Field(description="消息列表")
    has_more: bool = Field(description="是否还有更多")
    total: int = Field(description="会话消息总数")


class MessageContextResponse(ContractModel):
    """消息上下文窗口响应（跳回来源消息定位锚点）。"""

    anchor: Message = Field(description="锚点消息")
    before: list[Message] = Field(default_factory=list, description="锚点前窗口（时间正序）")
    after: list[Message] = Field(default_factory=list, description="锚点后窗口（时间正序）")
    conversationId: str = Field(description="会话 ID（必填回填）")


class SearchMessageItem(ContractModel):
    """搜索结果项（消息级；responseId best-effort，segmentId 恒 null 由缺省省略表达）。"""

    conversationId: str = Field(description="会话 ID")
    messageId: str = Field(description="消息 ID")
    responseId: Optional[str] = Field(default=None, description="最近一条 generation ID（best-effort），可缺省")
    segmentId: Optional[str] = Field(default=None, description="分段 ID，消息级搜索恒 null，可缺省")
    role: Literal["user", "assistant", "tool"] = Field(description="消息角色")
    timestamp: str = Field(description="ISO 时间戳")
    snippet: str = Field(description="内容摘要（前 100 字）")


class SearchMessagesResponse(ContractModel):
    """消息搜索响应（cursor = base64("{{timestamp}}|{{_id}}")）。"""

    items: list[SearchMessageItem] = Field(description="搜索结果项列表")
    has_more: bool = Field(description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="下一页游标，可缺省")


class ResourceItem(ContractModel):
    """会话资源索引项（按需扫描提取，url 即下载引用/缩略图）。"""

    messageId: str = Field(description="来源消息 ID")
    timestamp: str = Field(description="ISO 时间戳")
    kind: str = Field(description="资源类别（image/link/video/audio/file/sticker）")
    url: str = Field(description="资源引用（URL 或 src）")


class ResourceListResponse(ContractModel):
    """资源索引分页响应（cursor 指向最后扫描消息 _id，下页回传为 before）。"""

    items: list[ResourceItem] = Field(description="资源项列表")
    has_more: bool = Field(description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="下一页游标（最后扫描 _id），可缺省")


class GenerationStatus(ContractModel):
    """generation 状态查询响应（BE-024；与 generations 存储文档对称）。"""

    generationId: str = Field(description="生成实例 ID（gen_ 前缀）")
    conversationId: str = Field(description="会话 ID")
    messageId: str = Field(description="assistant 父消息 ID")
    userMessageId: str = Field(description="触发该 generation 的用户消息 ID")
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"] = Field(
        description="generation 在途/终态状态")
    model: Optional[str] = Field(default=None, description="实际模型，可缺省")
    finishReason: Optional[Literal["stop", "tool_calls"]] = Field(
        default=None, description="完成原因，可缺省")
    error: Optional[dict[str, Any]] = Field(
        default=None, description="失败详情（{code,message,retryable}），可缺省")
    createdAt: str = Field(description="创建时间（UTC ISO）")
    finishedAt: Optional[str] = Field(default=None, description="完成/失败时间（UTC ISO），可缺省")


class GenerationListResponse(ContractModel):
    """generation 列表响应（cursor = base64("{{created_at}}|{{generation_id}}")）。"""

    generations: list[GenerationStatus] = Field(description="generation 状态列表")
    has_more: bool = Field(description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="下一页游标，可缺省")


class PendingMessageItem(ContractModel):
    """pending 用户消息项（重连后判断"我发过但未处理"）。"""

    messageId: str = Field(description="消息 ID")
    snippet: str = Field(description="内容摘要（前 50 字）")
    createdAt: str = Field(description="创建时间（UTC ISO）")


class PendingApprovalItem(ContractModel):
    """pending 工具审批项（全局返回，不按会话过滤——D-b 全局工具语义）。"""

    requestId: str = Field(description="审批请求 ID")
    tool: str = Field(description="工具名")
    params: dict[str, Any] = Field(default_factory=dict, description="工具调用参数")


class PendingQueryResponse(ContractModel):
    """pending 查询响应（BE-024：pendingMessages 按会话 + pendingApprovals 全局）。"""

    pendingMessages: list[PendingMessageItem] = Field(
        default_factory=list, description="未处理用户消息列表（按会话）")
    pendingApprovals: list[PendingApprovalItem] = Field(
        default_factory=list, description="未处理工具审批列表（全局）")


class ConversationUsage(ContractModel):
    """会话级 token 累计（generation.completed 可选字段；形状对齐 TokenStats，wire 用 byModel）。"""

    total: int = Field(description="会话累计 token 数")
    byModel: dict[str, int] = Field(description="按模型拆分的累计 token 数")
