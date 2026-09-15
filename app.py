#!/usr/bin/env python3
"""Elysia Agent — 桌面虚拟角色完整流水线。

基于 Architecture.md 三层架构:
  L0 (Module/): LLM, TTS, Output, Config
  L1 (core/):  EventBus, AgentFSM, CognitiveLoop, PromptAssembler
  L2 (core/memory/): Session, LongTerm, ToolRegistry

用法:
  python app.py              # WebSocket 服务器模式（默认）
  python app.py --cli        # 交互终端模式
  python app.py --check      # 环境预检
"""

import argparse
import asyncio
import sys
import warnings
from pathlib import Path
from typing import Any

# Suppress noisy library warnings BEFORE any imports that trigger them
warnings.filterwarnings("ignore", ".*pynvml.*", category=FutureWarning)
warnings.filterwarnings("ignore", ".*cuda capability.*", category=UserWarning)
warnings.filterwarnings("ignore", ".*Minimum and Maximum.*", category=UserWarning)
warnings.filterwarnings("ignore", ".*Please install PyTorch.*", category=UserWarning)
warnings.filterwarnings("ignore", ".*Tesla M40.*", category=UserWarning)
warnings.filterwarnings("ignore", ".*Found GPU.*", category=UserWarning)
warnings.filterwarnings("ignore", ".*encoder_attention_mask.*", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Disable CUDA GPU detection — GPU is incompatible (sm_52) and loading torch
# with CUDA support triggers UserWarning spam in thread-pool workers.
import os as _os
_os.environ["CUDA_VISIBLE_DEVICES"] = ""

# 使用 conda 环境的 SSL 证书，避免系统证书权限问题
_conda_prefix = _os.environ.get("CONDA_PREFIX", "")
_conda_cert = _os.path.join(_conda_prefix, "ssl/cert.pem")
if _os.path.exists(_conda_cert):
    _os.environ["SSL_CERT_FILE"] = _conda_cert

from dotenv import load_dotenv
load_dotenv()

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from core.utils import setup as setup_logging
from core.agent import Agent
from core.agent.server import run_agent_service
from core.input.server import create_app


async def run_interactive(config: Any) -> None:
    """交互终端模式 (--cli)。"""
    agent = Agent(config=config)
    await agent.start()

    print("输入消息开始对话，/quit 退出，Ctrl+C 打断")

    while True:
        try:
            line = await asyncio.to_thread(input, "你: ")
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("/quit", "/exit", "/q"):
                break
            await agent.process_message(line)
            # Wait for idle
            await agent.status.wait_until_idle()
        except (EOFError, KeyboardInterrupt):
            print()
            break

    await agent.stop()
    print("再见！")


def main():
    # Parse --config first so we can load AppConfig for CLI defaults
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", "-c", type=str, default="config.yaml")
    pre_args, _ = pre_parser.parse_known_args()

    # Load AppConfig once via ConfigLoader
    from Module.config.loader import ConfigLoader
    from core.config import AppConfig
    try:
        config = ConfigLoader(AppConfig, yaml_path=pre_args.config).load()
    except Exception as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Elysia Agent")
    parser.add_argument("--cli", action="store_true",
                        help="交互终端模式（默认启动 WebSocket 服务器）")
    parser.add_argument("--host", type=str, default=config.server.host,
                        help="Server bind address")
    parser.add_argument("--port", type=int, default=config.server.port,
                        help="Server port")
    parser.add_argument("--tts", action="store_true", default=config.tts.enabled,
                        help="Enable TTS (requires GPT-SoVits)")
    parser.add_argument("--stt", action="store_true", default=config.stt.enabled,
                        help="Enable STT (requires SenseVoice)")
    parser.add_argument("--character", type=str, default=config.agent.character,
                        help="角色配置文件路径")
    parser.add_argument("--config", "-c", type=str, default="config.yaml",
                        help="总配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--check", action="store_true",
                        help="环境预检（通过后退出，不启动应用）")
    parser.add_argument("--full", action="store_true",
                        help="预检包含连通性测试（需配合 --check）")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # CLI overrides config (priority: CLI > config.yaml > defaults)
    config.server.host = args.host
    config.server.port = args.port
    config.tts.enabled = args.tts
    config.stt.enabled = args.stt
    config.agent.character = args.character

    if args.check:
        from Module.config.preflight import run as preflight
        ok = preflight(config, full=args.full)
        sys.exit(0 if ok else 1)

    if args.cli:
        asyncio.run(run_interactive(config))
    else:
        # S2-A5：**重启 = 重建 Agent 实例**——进程级循环在 core.agent.server，
        # 构造器由本入口注入（避免 core.agent 包内循环导入）。
        asyncio.run(run_agent_service(
            config=config, config_path=args.config,
            app_factory=create_app, agent_factory=Agent))


if __name__ == "__main__":
    main()
