"""文件写入工具插件 — 在 sandbox 目录下安全写入文件（需 HITL 审批）。"""

from pathlib import Path
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


class WriteFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对路径，自动限定在 sandbox 目录下）")
    content: str = Field(..., description="要写入的内容")


class WriteFilePlugin(ToolPlugin):
    name = "write_file"
    description = "在安全目录下写入文件（需用户确认）"
    param_model = WriteFileParams
    capabilities = {Capability.FILESYSTEM_WRITE}
    safety = SafetyProfile(
        filesystem_allowed=True,
        write_dir="data/sandbox/{session_id}",
        max_file_bytes=10 * 1024 * 1024,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: WriteFileParams,
    ) -> str:
        session_id = ctx.session_id or "default"
        write_dir = Path(
            self.safety.write_dir.replace("{session_id}", session_id)
        ).resolve()
        write_dir.mkdir(parents=True, exist_ok=True)

        file_path = write_dir / params.path
        resolved = file_path.resolve()

        if not str(resolved).startswith(str(write_dir)):
            return f"拒绝写入: 路径 '{params.path}' 越过了 sandbox 边界"

        try:
            resolved.write_text(params.content, encoding="utf-8")
            size = resolved.stat().st_size
            return f"已写入 {resolved.name} ({size} bytes)"
        except Exception as e:
            return f"写入失败: {e}"
