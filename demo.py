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


async def run_once(message: str, character_path: str | None, config_path: str | None,
                   tts_enabled: bool | None = None) -> None:
    """Single-shot: process one message and exit."""
    agent = DemoAgent(character_path=character_path, config_path=config_path,
                      tts_enabled=tts_enabled)
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


async def run_interactive(character_path: str | None, config_path: str | None,
                          tts_enabled: bool | None = None) -> None:
    """Interactive REPL mode."""
    agent = DemoAgent(character_path=character_path, config_path=config_path,
                      tts_enabled=tts_enabled)
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


def _load_config_defaults(config_path: str) -> dict:
    """Load config.yaml to use as CLI defaults. Returns empty dict on failure."""
    try:
        import yaml
        p = Path(config_path)
        if p.is_file():
            cfg = yaml.safe_load(p.read_text("utf-8")) or {}
            sv = cfg.get("Server", {})
            tts = cfg.get("TTS", {})
            stt = cfg.get("STT", {})
            return {
                "host": sv.get("host", "0.0.0.0"),
                "port": sv.get("port", 8765),
                "tts": tts.get("enabled", False),
                "stt": stt.get("enabled", False),
            }
    except Exception:
        pass
    return {}


def _run_and_diagnose(coro):
    """Replicate asyncio.run() with step-by-step diagnostic output."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        print("[diagnose] 1. running main coroutine...")
        loop.run_until_complete(coro)
        print("[diagnose] 2. main coroutine done, cancelling all tasks...")
        import asyncio.tasks as _tasks
        pending = _tasks.all_tasks(loop)
        print(f"[diagnose]    {len(pending)} pending tasks: {[t.get_name() for t in pending]}")
        for t in pending:
            t.cancel()
        loop.run_until_complete(
            _tasks.gather(*pending, return_exceptions=True))
        print("[diagnose] 3. shutting down async generators...")
        loop.run_until_complete(loop.shutdown_asyncgens())
        print("[diagnose] 4. shutting down default executor (wait=False)...")
        try:
            # 用 wait=False 代替 loop.shutdown_default_executor() 的 wait=True，
            # 因为 embedding 预加载线程可能还在下载模型，无法被 asyncio 取消。
            executor = loop._default_executor
            if executor is not None:
                executor.shutdown(wait=False)
        except Exception:
            pass
        print("[diagnose] 5. done — closing loop")
    finally:
        print("[diagnose] finally: closing loop...")
        loop.close()
        print("[diagnose] finally: loop closed, clearing...")
        asyncio.set_event_loop(None)
        print("[diagnose] finally: all done")
        # 所有 cleanup 已完成（tasks cancelled, asyncgens closed, executor shut down）。
        # 不走 Python exit sequence — 库（pymilvus/aiohttp/sentence_transformers）
        # 的 atexit/__del__ 可能在已关闭的 event loop 上死锁。
        import os as _os
        _os._exit(0)


def main():
    # Parse --config first so we can read defaults from it
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", "-c", type=str, default="config.yaml")
    pre_args, _ = pre_parser.parse_known_args()

    cfg_defaults = _load_config_defaults(pre_args.config)

    parser = argparse.ArgumentParser(description="Elysia Agent Demo")
    parser.add_argument("--once", type=str, metavar="MSG", help="单次对话模式")
    parser.add_argument("--server", "-s", action="store_true", help="WebSocket 服务器模式")
    parser.add_argument("--host", type=str, default=cfg_defaults.get("host", "0.0.0.0"),
                        help="Server bind address")
    parser.add_argument("--port", type=int, default=cfg_defaults.get("port", 8765),
                        help="Server port")
    parser.add_argument("--tts", action="store_true", default=cfg_defaults.get("tts", False),
                        help="Enable TTS (requires GPT-SoVits)")
    parser.add_argument("--stt", action="store_true", default=cfg_defaults.get("stt", False),
                        help="Enable STT (requires SenseVoice)")
    parser.add_argument("--character", type=str, default=None, help="角色配置文件路径")
    parser.add_argument("--config", "-c", type=str, default="config.yaml", help="总配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.server:
        agent = DemoAgent(character_path=args.character, config_path=args.config,
                          tts_enabled=args.tts)
        _run_and_diagnose(agent.run_server(
            host=args.host, port=args.port, stt_enabled=args.stt,
        ))
    elif args.once:
        asyncio.run(run_once(args.once, args.character, args.config, tts_enabled=args.tts))
    else:
        asyncio.run(run_interactive(args.character, args.config, tts_enabled=args.tts))


if __name__ == "__main__":
    main()
