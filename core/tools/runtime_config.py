"""运行时配置工具插件 — 查看 agent 配置和状态。"""

import os
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class RuntimeConfigParams(BaseModel):
    query: str | None = Field(
        default=None,
        description="可选：查询特定项，如 'model', 'tts', 'memory'。留空返回全部。",
    )


class RuntimeConfigPlugin(ToolPlugin):
    name = "get_runtime_config"
    description = "查看当前运行时配置：模型名、TTS/STT 状态、内存使用、已加载工具"
    param_model = RuntimeConfigParams
    capabilities = {Capability.PROCESS}
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: RuntimeConfigParams,
    ) -> str:
        config = ctx.get("config")
        tools = ctx.get("_tool_names", [])
        lines = []

        if config is not None:
            llm_cfg = getattr(config, "llm", None)
            tts_cfg = getattr(config, "tts", None)
            stt_cfg = getattr(config, "stt", None)
            server_cfg = getattr(config, "server", None)

            if llm_cfg:
                lines.append(f"LLM 模型: {getattr(llm_cfg, 'model', 'unknown')}")
            if tts_cfg:
                lines.append(
                    f"TTS: {'启用' if getattr(tts_cfg, 'enabled', False) else '禁用'}"
                )
            if stt_cfg:
                lines.append(
                    f"STT: {'启用' if getattr(stt_cfg, 'enabled', False) else '禁用'}"
                )
            if server_cfg:
                lines.append(
                    f"服务: {getattr(server_cfg, 'host', '?')}:{getattr(server_cfg, 'port', '?')}"
                )

        lines.append(
            f"已加载工具 ({len(tools)}): {', '.join(tools[:20])}"
        )

        if HAS_PSUTIL:
            proc = psutil.Process()
            mem = proc.memory_info()
            lines.append(
                f"进程内存: RSS={mem.rss / 1024 / 1024:.1f}MB, "
                f"VMS={mem.vms / 1024 / 1024:.1f}MB, "
                f"线程数={proc.num_threads()}"
            )

        env_vars = ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY"]
        for var in env_vars:
            val = os.getenv(var)
            lines.append(f"环境变量 {var}: {'已设置' if val else '未设置'}")

        return "\n".join(lines)
