"""Desktop 工具插件 — 委托 Electron Main 执行用户电脑本地操作（BE-040）。

这四个插件不接触文件系统/屏幕——一切本地操作由 Electron Main 完成（docs/15 §三.2：
后端不得直接操作用户电脑）。execute() 将请求经 `ctx.services["desktop_bridge"]`
委托给桥（Task 4 实现 DesktopToolBridge），桥负责幂等去重、状态登记与结果回传；
插件层仅做结构化传输 + 参数摘要绑定（BE-043 paramHash）。

信任边界（spec §4.4 显式认定）：`desktop.file.select` 产出路径引用后，后续
`desktop.file.read` 的 path 参数接受任意值——来源追踪属 Electron 本地安全职责，
后端只负责结构化传输 + paramHash 绑定，不做跨调用白名单。
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from Module.tool.protocol import (
    ErrorType, ToolContext, ToolEmitter, ToolPlugin, ToolResult,
)
from Module.tool.safety import SafetyProfile

# 桥往返可能超过 SafetyPipeline 外层默认 30s（用户选文件/授权可能久），
# 显式放大 request_timeout ≥ 桥超时（120s），否则往返会在 30s 被提前取消（spec §4.4）。
_DESKTOP_REQUEST_TIMEOUT = 120.0


def canonical_json(arguments: dict) -> str:
    """规范化 JSON 序列化 — 键排序 + 紧凑分隔 + 保留非 ASCII。

    作为参数摘要 SHA-256（paramHash）的权威依据（BE-043）：
    内容相同的 arguments 无论键插入顺序如何，都得到相同的规范序列。

    Args:
        arguments: 工具参数 dict。

    Returns:
        规范化 JSON 字符串。
    """
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def arguments_hash(arguments: dict) -> str:
    """计算参数摘要 SHA-256 — BE-043「参数摘要变化拒绝」的权威依据。

    Args:
        arguments: 工具参数 dict。

    Returns:
        SHA-256 十六进制摘要。
    """
    return hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


# ============================================================================
# 参数模型（后端侧契约校验，非本地安全授权）
# ============================================================================

class FileSelectParams(BaseModel):
    """无参 — Electron 弹出系统文件选择器。"""


class FileReadParams(BaseModel):
    path: str = Field(
        ..., min_length=1,
        description="要读取的文件路径（用户所选路径引用或任意路径，本地校验由 Electron 兜底）",
    )


class FileWriteParams(BaseModel):
    path: str = Field(..., min_length=1, description="要写入的文件路径")
    content: str = Field(..., min_length=1, description="要写入的文本内容")


class ScreenCaptureParams(BaseModel):
    region: str | None = Field(default=None, description="截屏区域（可选）")


# ============================================================================
# Desktop 插件基类 — 统一 execute 委托桥
# ============================================================================

class _DesktopPluginBase(ToolPlugin):
    """Desktop 插件公共声明 + execute 委托桥（duck-typed，Task 4 实现桥）。

    request_timeout 必须显式声明 ≥ 桥超时（120s），防 SafetyPipeline 外层
    asyncio.wait_for 在 30s 截断桥往返（对照 vision/plugin.py 先例）。
    """

    runtime: str = "desktop"
    version: str = "1.0.0"
    safety = SafetyProfile(request_timeout=_DESKTOP_REQUEST_TIMEOUT)

    async def _delegate(self, ctx: ToolContext, params: Any) -> ToolResult:
        """委托桥执行桌面工具。

        bridge 缺失或不可用 → 立即 UNAVAILABLE（不触发委托路径）；否则组装
        call dict（含 arguments_hash）→ `bridge.request_and_wait(call=call)`。

        Args:
            ctx: 工具调用上下文。
            params: Pydantic 参数模型实例或 None。

        Returns:
            ToolResult：委托结果或 UNAVAILABLE 失败。
        """
        bridge = ctx.services.get("desktop_bridge")
        if bridge is None or not bridge.is_available():
            return ToolResult.fail(
                name=self.name,
                error="无桌面客户端连接",
                error_type=ErrorType.UNAVAILABLE,
            )

        arguments = params.model_dump() if params is not None else {}
        call = {
            "tool_name": self.name,
            "tool_version": self.version,
            "runtime": self.runtime,
            "arguments": arguments,
            "arguments_hash": arguments_hash(arguments),
            "risk_level": self.risk_level,
            "permission_required": self.permission_required,
            "approval_authority": self.approval_authority,
            "source_call_id": ctx.call_id,          # B-6-05：LLM tool_call_id 透传（D6-07）
            "request_id": ctx.trace_id or None,     # B-6-05：请求追踪 ID（空串→None→wire 省略，
                                                    # 防 requestId:""；真实 Agent 回合经 loop.py:355 填非空 trace_id）
        }
        return await bridge.request_and_wait(call=call)


# ============================================================================
# 四个 Desktop 插件（D-g risk 标注）
# ============================================================================

class DesktopFileSelectPlugin(_DesktopPluginBase):
    name = "desktop.file.select"
    description = "打开系统文件选择器，返回用户所选文件路径引用"
    param_model = FileSelectParams
    capabilities = set()
    risk_level = "safe"  # 无副作用，Electron 弹选择器（D-g）

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: Any = None,
    ) -> ToolResult:
        """打开系统文件选择器，委托 Electron 返回用户所选文件路径引用。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 参数模型（无字段）。

        Returns:
            ToolResult：用户所选文件路径引用或 UNAVAILABLE。
        """
        return await self._delegate(ctx, params)


class DesktopFileReadPlugin(_DesktopPluginBase):
    name = "desktop.file.read"
    description = "读取用户所选/指定文件的文本内容"
    param_model = FileReadParams
    capabilities = set()
    risk_level = "medium"  # 读用户电脑文件（D-g）

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: Any = None,
    ) -> ToolResult:
        """读取用户所选/指定文件的文本内容，委托 Electron 执行。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: FileReadParams（path 必填）。

        Returns:
            ToolResult：文件文本内容或 UNAVAILABLE。
        """
        return await self._delegate(ctx, params)


class DesktopFileWritePlugin(_DesktopPluginBase):
    name = "desktop.file.write"
    description = "写入文本到用户指定路径"
    param_model = FileWriteParams
    capabilities = set()
    risk_level = "high"  # 写用户电脑文件（D-g）

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: Any = None,
    ) -> ToolResult:
        """写入文本到用户指定路径，委托 Electron 执行。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: FileWriteParams（path/content 必填）。

        Returns:
            ToolResult：写入结果或 UNAVAILABLE。
        """
        return await self._delegate(ctx, params)


class DesktopScreenCapturePlugin(_DesktopPluginBase):
    name = "desktop.screen.capture"
    description = "截取用户屏幕，返回截图附件"
    param_model = ScreenCaptureParams
    capabilities = set()
    risk_level = "high"  # 截取用户屏幕（D-g）
    output_schema = {"type": "object", "description": "包含 attachments 的媒体结果"}

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: Any = None,
    ) -> ToolResult:
        """截取用户屏幕，委托 Electron 返回截图附件。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: ScreenCaptureParams（region 可选）。

        Returns:
            ToolResult：截图附件引用或 UNAVAILABLE。
        """
        return await self._delegate(ctx, params)
