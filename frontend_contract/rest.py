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
    """单个已配置 provider（models 为可选用模型列表）。

    批次 5 增 runtime/能力字段衔接批次 4 Desktop Profile：credentialConfigured（Secret Store
    或 env 已配置密钥）、runtime（backend/desktop/local_worker 执行位置，缺省 backend）、
    capabilities（能力描述 dict，如 {auth, execution}）。均为缺省值非 None → 必然上 wire。
    """

    models: list[str] = Field(description="可用模型列表")
    credentialConfigured: bool = Field(default=False, description="密钥是否已配置（Secret Store/env）")
    runtime: str = Field(default="backend", description="Profile 执行位置（backend/desktop/local_worker）")
    capabilities: dict[str, Any] = Field(default_factory=dict, description="能力描述 dict")


class CustomProvider(ContractModel):
    """自定义 provider 条目（custom_providers.yaml 持久化形状）。

    批次 5 增 runtime/credentialConfigured/capabilities（同 ProviderEntry，衔接批次 4 Profile）。
    api_key 不入 YAML——经 Secret Store 承接（credentialConfigured 反映已配置状态）。
    """

    name: str = Field(description="provider 名称")
    base_url: str = Field(description="Base URL")
    api_key_env: Optional[str] = Field(default=None, description="API Key 环境变量名，可缺省")
    models: Optional[list[str]] = Field(default=None, description="模型列表，可缺省")
    runtime: str = Field(default="backend", description="Profile 执行位置（backend/desktop/local_worker）")
    credentialConfigured: bool = Field(default=False, description="密钥是否已配置（Secret Store/env）")
    capabilities: dict[str, Any] = Field(default_factory=dict, description="能力描述 dict")


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


# ────────────────────── TTS 声音（GET /api/tts/voices + POST /api/tts/preview，BE-051） ──────────────────────


class TtsVoiceEntry(ContractModel):
    """TTS 声音条目（`TTSBackend.list_voices()` 协议产物）。

    languages/capabilities 缺省空（后端条目可省略）；缺省值非 None → 必然上 wire（spec §9 接受键新增）。
    """

    id: str = Field(description="声音 ID（provider 内唯一）")
    name: Optional[str] = Field(default=None, description="显示名，可缺省")
    languages: list[str] = Field(default_factory=list, description="支持语言列表")
    capabilities: dict[str, Any] = Field(default_factory=dict, description="能力描述（音色性别/风格等）")


class TtsVoicesResponse(ContractModel):
    """声音列表响应（GET /api/tts/voices；恒作用于活动会话激活 TTS profile）。

    source=static|api（数据源：静态声明 / MiniMax 动态拉取）；后端未启用 → 空列表 + error（200 不炸）。
    """

    voices: list[TtsVoiceEntry] = Field(description="声音列表")
    current: Optional[str] = Field(
        default=None, description="当前声音 ID（活动会话 service_state.tts_voice 或后端默认），可缺省")
    source: str = Field(default="static", description="数据源（static|api）")
    error: Optional[str] = Field(default=None, description="错误信息（后端未启用时带），可缺省")


class TtsPreviewResult(ContractModel):
    """声音试听结果（POST /api/tts/preview；固定短文本合成，不读聊天）。

    成功 → {ok, mime, audio_base64, duration_ms?}；失败 → {ok: False, error}。
    """

    ok: bool = Field(description="试听是否成功")
    mime: Optional[str] = Field(default=None, description="音频 MIME 类型，可缺省")
    duration_ms: Optional[int] = Field(default=None, description="音频时长毫秒，可缺省")
    audio_base64: Optional[str] = Field(default=None, description="base64 音频数据，可缺省")
    error: Optional[str] = Field(default=None, description="失败原因，可缺省")


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


# ────────────────────── 批次 4 v2 契约：Desktop 工具注册表（BE-040/042/043） ──────────────────────


class ToolMetadata(ContractModel):
    """桌面工具注册元数据（GET /api/tools 单工具条目；Electron 校验"只接受已注册工具和版本"）。

    desktop 工具 runtime=desktop、approvalAuthority=client（D-h）；available =
    bridge.is_available()（desktop 未连接时仍列出但 available=false，客户端可知晓存在）。
    """

    name: str = Field(description="工具名（desktop.file.read 等）")
    description: str = Field(description="工具描述")
    runtime: str = Field(description="工具 Runtime（backend/desktop/local_worker）")
    riskLevel: str = Field(description="风险等级（safe/medium/high）")
    permissionRequired: bool = Field(description="是否需要权限（risk != safe 或 requires_human_approval）")
    approvalAuthority: str = Field(description="审批主体（desktop → client，backend 工具恒 backend）")
    version: str = Field(description="工具版本（语义化版本，schema 变更时手动 bump）")
    inputSchema: dict[str, Any] = Field(description="参数 JSON Schema（param_model 派生）")
    outputSchema: dict[str, Any] = Field(description="输出 JSON Schema（声明性，批次 4 内无强校验）")
    available: bool = Field(description="是否可用（desktop 工具 = bridge.is_available()，backend 恒 true）")


class ToolRegistryResponse(ContractModel):
    """工具注册表响应（GET /api/tools：完整元数据 + 桌面客户端可用性）。"""

    tools: list[ToolMetadata] = Field(description="工具元数据列表")
    desktopAvailable: bool = Field(description="是否有 desktop 客户端连接可执行")


class DesktopCallStatus(ContractModel):
    """桌面调用状态查询响应（GET /api/tools/calls/{callId}，BE-043）。

    status 用 repo 枚举（pending/accepted/running/succeeded/failed/rejected/cancelled/
    expired/offline），与 wire `tool.status` 六态翻译固定（spec §9 映射表：result→succeeded、
    error→failed、rejected/cancelled 同名；accepted/running 同名；pending/expired/offline
    仅服务端）；toolVersion/runtime/paramHash 补齐供「相同 ID 工具/Runtime/版本/参数摘要变化时拒绝」经 REST 复核。
    """

    callId: str = Field(description="调用 ID（call_ 前缀）")
    toolName: str = Field(description="工具名")
    toolVersion: str = Field(description="工具版本")
    runtime: str = Field(description="工具 Runtime（desktop）")
    paramHash: str = Field(description="参数摘要（SHA-256）")
    status: Literal[
        "pending", "accepted", "running", "succeeded", "failed",
        "rejected", "cancelled", "expired", "offline",
    ] = Field(description="repo 状态枚举（wire 六态翻译固定 + 服务端态）")
    createdAt: str = Field(description="创建时间（UTC ISO）")
    expiresAt: str = Field(description="过期时间（UTC ISO）")
    result: Optional[dict[str, Any]] = Field(
        default=None, description="结果（{output,attachments,executionSummary}），可缺省；attachments[] 键 {url,mimeType,label}")
    error: Optional[dict[str, Any]] = Field(default=None, description="错误（{code,message}），可缺省")


# ────────────────────── 批次 7 附件/表情（BE-070/071，P7-1/2/6） ──────────────────────


class AttachmentInfo(ContractModel):
    """上传响应中的附件条目（POST /api/uploads 响应 attachments[]）。

    `signedUrl` 可缺省（fail-closed 省略——签发失败时不带签名 URL，前端渲染层
    对稳定路径统一调签发端点解析）；`filename` 脱敏后的原始文件名
    （去路径分隔符/截断 255/剥控制字符）。
    """

    attachmentId: str = Field(description="附件 ID（att_ 前缀）")
    kind: str = Field(description="附件类别（image/video/audio/file）")
    mime: str = Field(description="MIME 类型")
    size: int = Field(description="附件字节数")
    url: str = Field(description="稳定访问路径（/api/attachments/{id}，不随签名过期）")
    signedUrl: Optional[str] = Field(
        default=None, description="短期签名 URL（签发失败 fail-closed 省略），可缺省")
    filename: str = Field(default="", description="脱敏后的原始文件名（去路径分隔符/截断 255/剥控制字符）")


class AttachmentUploadResponse(ContractModel):
    """附件上传成功响应（POST /api/uploads）。"""

    attachments: list[AttachmentInfo] = Field(description="上传成功附件列表")


class AttachmentSignResponse(ContractModel):
    """签发端点响应（GET /api/attachments/{id}/sign，C-1 渲染层签名解析）。"""

    attachmentId: str = Field(description="附件 ID（att_ 前缀）")
    url: str = Field(description="短期签名 URL（?sig=...&exp=...）")
    expiresAt: str = Field(description="签名过期时间（UTC ISO）")


class StickerEntry(ContractModel):
    """内置表情条目（/api/stickers 列表项；契约单源，禁止 plain dict）。

    表情是公共资源，走公开静态 `/static/stickers/{id}.png`——不落 attachments
    集合、不 ref_count、不参与 GC（P7-6 四轮 review 定案）。
    """

    stickerId: str = Field(description="表情 ID（st_ 前缀）")
    src: str = Field(description="静态资源路径（/static/stickers/{id}.png）")


class StickerListResponse(ContractModel):
    """表情列表响应（GET /api/stickers）。"""

    stickers: list[StickerEntry] = Field(description="内置表情列表")
