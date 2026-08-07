"""Elysia Agent 版本号——单一事实来源（server_info.server_version 用）。

语义化版本；每次发版递增。后端协议兼容窗口以此为准。
`get_build_sha`/`build_server_version`：git build SHA 上 wire——
server_version 带 `+build_sha[:7]` 后缀，独立 `build_sha` 字段给前端展示/反馈定位。
"""

import subprocess

from paths import get_project_root

__version__ = "0.1.0"

# 模块级缓存——get_build_sha 每进程只跑一次 git（build_server_info 每次握手都构造，
# 缓存避免每次 spawn subprocess；Agent.start() 预热保证首次握手前已就绪）。
# _build_sha_tried 标志使失败路径也缓存：git 不可用（非 git 仓库/命令失败）时不再重试。
_build_sha: str | None = None
_build_sha_tried: bool = False


def get_build_sha() -> str | None:
    """获取当前构建的 git commit SHA（HEAD）；非 git 仓库/失败返回 None。

    无论成败每进程只跑一次 git：成功缓存 SHA，失败缓存 None 不重跑。

    Returns:
        git HEAD 完整 SHA（40 位 hex），非 git 仓库或 subprocess 失败时 None。
    """
    global _build_sha, _build_sha_tried
    if not _build_sha_tried:
        _build_sha_tried = True
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                check=False,  # 失败不抛，returncode != 0 → None
                cwd=get_project_root(),
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                _build_sha = result.stdout.decode("utf-8", errors="replace").strip()
        except (subprocess.SubprocessError, OSError):
            _build_sha = None
    return _build_sha


def build_server_version() -> str:
    """组装 server_info.server_version：__version__ + 可选 +build_sha[:7] 后缀。

    Returns:
        形如 ``"0.1.0"``（无 git）或 ``"0.1.0+abc1234"``（git 可用）。
    """
    sha = get_build_sha()
    return f"{__version__}+{sha[:7]}" if sha else __version__
