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


#: 显式 CLI 覆盖清单：`(argparse dest, config 点路径)`。
#:
#: F4（Batch 1.6）的两个要点都在这张表上：
#: ① 这些 flag 的 argparse `default` 是 `SUPPRESS`（**未传时属性不存在**），
#:    于是"用户没传"与"用户传了等于默认值的值"可以区分——`default=<config 值>`
#:    那种写法两者不可区分（实测三种输入解析结果完全相同）。未传 ⇒ 不进覆盖表
#:    ⇒ YAML/env 的值照常生效（`argparse` 的 default 绝不能被当成显式覆盖）。
#: ② 覆盖表随进程走：`run_agent_service` 每轮重载 YAML 之后重新施加它
#:    （此前首轮重载就把 5 个 flag 整个丢掉，实测 `--port 9999` 仍绑 8765）。
CLI_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("host", "server.host"),
    ("port", "server.port"),
    ("tts", "tts.enabled"),
    ("stt", "stt.enabled"),
    ("character", "agent.character"),
)


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。

    **不接收 config**：覆盖类 flag 的默认值一律是 `SUPPRESS`（见 `CLI_OVERRIDES`），
    所以解析器不需要（也不该）知道任何配置值——那正是"argparse default 冒充显式覆盖"
    的来源。

    Returns:
        配置好的 `ArgumentParser`。
    """
    parser = argparse.ArgumentParser(description="Elysia Agent")
    parser.add_argument("--cli", action="store_true",
                        help="交互终端模式（默认启动 WebSocket 服务器）")
    parser.add_argument("--host", type=str, default=argparse.SUPPRESS,
                        help="Server bind address")
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="Server port")
    parser.add_argument("--tts", action="store_true", default=argparse.SUPPRESS,
                        help="Enable TTS (requires GPT-SoVits)")
    parser.add_argument("--stt", action="store_true", default=argparse.SUPPRESS,
                        help="Enable STT (requires SenseVoice)")
    parser.add_argument("--character", type=str, default=argparse.SUPPRESS,
                        help="角色配置文件路径")
    parser.add_argument("--config", "-c", type=str, default="config.yaml",
                        help="总配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--check", action="store_true",
                        help="环境预检（通过后退出，不启动应用）")
    parser.add_argument("--full", action="store_true",
                        help="预检包含连通性测试（需配合 --check）")
    return parser


def collect_cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """把用户**显式传入**的 flag 收集成 `{config 点路径: 值}`。

    `SUPPRESS` 默认的含义是"没传就没有这个属性"，于是 `hasattr` 就是
    "用户显式传了它"的判据——不需要读 `sys.argv`，也不需要自定义 Action。

    Returns:
        只含显式传入项的 dict（可能为空）。
    """
    return {path: getattr(args, dest) for dest, path in CLI_OVERRIDES
            if hasattr(args, dest)}


def main():
    # 先单独解析 --config：AppConfig 要先加载出来才能交给 `--check` 预检、
    # `--cli` 交互模式与 `run_agent_service`（它是这三条路径的共同输入）。
    # 注意：**不再**用它给覆盖类 flag 当默认值（见 `CLI_OVERRIDES`）。
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", "-c", type=str, default="config.yaml")
    pre_args, _ = pre_parser.parse_known_args()

    # Load AppConfig once via ConfigLoader
    from Module.config.loader import ConfigLoader
    from core.config import AppConfig, apply_config_overrides
    try:
        config = ConfigLoader(AppConfig, yaml_path=pre_args.config).load()
    except Exception as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)

    args = build_parser().parse_args()

    setup_logging(args.verbose)

    # CLI overrides config。**优先级（本批裁定并测试）**：
    #     显式 CLI 覆盖 > env > YAML > dataclass 默认
    # 前两层之间沿用现状语义：`ConfigLoader.load()` 内部按 defaults → YAML → env
    # 覆盖，本行在它之后写字段，故 CLI 赢 env（`--cli` / `--check` 一直如此）。
    # 注意这里只收集**用户真的传了**的 flag——argparse 的 default 不是覆盖。
    overrides = collect_cli_overrides(args)
    apply_config_overrides(config, overrides)

    if args.check:
        from Module.config.preflight import run as preflight
        ok = preflight(config, full=args.full)
        sys.exit(0 if ok else 1)

    if args.cli:
        # 这条路径不经 run_agent_service（没有第二次重载），覆盖已在上面生效
        asyncio.run(run_interactive(config))
    else:
        # S2-A5：**重启 = 重建 Agent 实例**——进程级循环在 core.agent.server，
        # 构造器由本入口注入（避免 core.agent 包内循环导入）。
        # F4：覆盖表一并交下去——循环每一轮都会重读 YAML，覆盖必须在重载**之后**
        # 重新施加，否则 `--port` 这类意图活不过第一轮。
        asyncio.run(run_agent_service(
            config=config, config_path=args.config,
            app_factory=create_app, agent_factory=Agent,
            config_overrides=overrides))


if __name__ == "__main__":
    main()
