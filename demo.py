#!/usr/bin/env python3
"""Elysia Agent Demo — 桌面虚拟角色完整流水线演示。

基于 Architecture.md 三层架构:
  L0 (Module/): LLM, TTS, Output, Config
  L1 (core/):  EventBus, AgentFSM, CognitiveLoop, PromptAssembler
  L2 (core/memory/): Session, LongTerm, ToolRegistry

用法:
  python demo.py              # 交互模式 (stdin)
  python demo.py --once 你好   # 单次对话
  python demo.py --verbose     # 详细日志
"""

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

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

from dotenv import load_dotenv
load_dotenv()

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from core.log_setup import setup as setup_logging
from core.demo_agent import DemoAgent


async def run_once(message: str, character_path: str | None, config_path: str | None) -> None:
    """Single-shot: process one message and exit."""
    agent = DemoAgent(character_path=character_path, config_path=config_path)
    await agent.start()
    print(f"\n你: {message}")
    print(f"{agent._character.name}: ", end="", flush=True)
    await agent.process_message(message)
    # Wait for processing to start, then finish
    while agent._fsm.state == "idle":
        await asyncio.sleep(0.1)
    while agent._fsm.state != "idle":
        await asyncio.sleep(0.1)
    await agent.stop()


async def run_interactive(character_path: str | None, config_path: str | None) -> None:
    """Interactive REPL mode."""
    agent = DemoAgent(character_path=character_path, config_path=config_path)
    await agent.start()

    print("输入消息开始对话，/quit 退出，Ctrl+C 打断\n")

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
            await asyncio.sleep(0.3)
            while agent._fsm.state != "idle":
                await asyncio.sleep(0.1)
        except (EOFError, KeyboardInterrupt):
            print("\n")
            break

    await agent.stop()
    print("\n再见！")


def main():
    parser = argparse.ArgumentParser(description="Elysia Agent Demo")
    parser.add_argument("--once", type=str, metavar="MSG", help="单次对话模式")
    parser.add_argument("--server", "-s", action="store_true", help="WebSocket 服务器模式")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Server bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--stt", action="store_true", help="Enable STT for audio input (requires SenseVoice)")
    parser.add_argument("--character", type=str, default=None, help="角色配置文件路径 (默认 config.yaml)")
    parser.add_argument("--config", "-c", type=str, default="config.yaml", help="总配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.server:
        agent = DemoAgent(character_path=args.character, config_path=args.config)
        asyncio.run(agent.run_server(
            host=args.host, port=args.port, stt_enabled=args.stt,
        ))
    elif args.once:
        asyncio.run(run_once(args.once, args.character, args.config))
    else:
        asyncio.run(run_interactive(args.character, args.config))


if __name__ == "__main__":
    main()
