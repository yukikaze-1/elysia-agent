"""项目路径统一管理。

所有项目级文件系统路径的唯一定义点。
项目根目录: ELYSIA_ROOT 环境变量 > paths.py 所在目录。

Usage:
    from paths import ProjectPath, get_project_root

    log_dir = ProjectPath.LOG.resolve()        # -> /path/to/project/log
    prompts = ProjectPath.PROMPTS.resolve()    # -> /path/to/project/core/prompts
    sandbox = ProjectPath.DATA_SANDBOX.resolve(session_id="abc")
    root = get_project_root()                  # -> /path/to/project
"""

import os
from enum import Enum
from pathlib import Path
from typing import Any


def _detect_project_root() -> Path:
    """ELYSIA_ROOT 环境变量优先，fallback 到 paths.py 所在目录。"""
    if env_root := os.getenv("ELYSIA_ROOT"):
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent


_project_root = _detect_project_root()


class ProjectPath(str, Enum):
    """项目路径枚举。resolve() 返回基于项目根的绝对 Path。

    每个成员可用 ELYSIA_PATH_{NAME} 环境变量覆盖。
    """

    # -- 目录 --
    DATA = "data"
    DATA_UPLOADS = "data/uploads"
    DATA_UPLOADS_IMAGES = "data/uploads/images"
    DATA_UPLOADS_DESKTOP = "data/uploads/desktop"
    DATA_SANDBOX = "data/sandbox/{session_id}"
    DATA_CACHE_VISION = "data/cache/vision"
    DATA_REF = "data/ref"
    DATA_SECRETS = "data/secrets"
    LOG = "log"
    STATIC = "static"
    LIVE2D_ASSETS = "data/live2d-assets"
    PROMPTS = "core/prompts"
    SYSTEM_PROMPT_TEMPLATES = "Module/system_prompt/templates"
    CHARACTERS = "character"
    MODEL_CACHE = ".model_cache"

    # -- 文件 --
    CONFIG_YAML = "config.yaml"
    DOTENV = ".env"
    CUSTOM_PROVIDERS_YAML = "custom_providers.yaml"
    STT_SAMPLE_WAV = "Module/stt/tests/fixtures/sample.wav"
    TOKEN_PENDING = "data/token_usage_pending.jsonl"
    SESSION_PENDING = "data/session_{session_id}.json"
    SESSION_PENDING_CHECK = "data/session_pending.jsonl"
    SCHEDULER_CHECKPOINT = "data/scheduler_checkpoint.json"
    MEMORY_PENDING_DELETES = "data/memory_pending_deletes.json"
    # 未同步到 Milvus 的待写条目 journal（review 复核 #6）：Milvus 离线时 insert 的
    # 记忆此前仅在内存 _dirty_ids，进程退出即丢失；本 journal 与 memory_pending_deletes
    # 同模式持久化，重启/重连后恢复补写。
    MEMORY_PENDING_SYNCS = "data/memory_pending_syncs.json"
    # 会话删除提交后清理失败 journal（review 复核 #5 D2）：doc 已删但 refs/归档
    # 未清，健康循环按此重试（与 memory_pending_deletes 同模式）
    SESSION_PENDING_CLEANUP = "data/session_pending_cleanup.json"

    def resolve(self, **kwargs: Any) -> Path:
        """返回绝对路径。

        Args:
            **kwargs: 填充 value 中的 {placeholder}

        Returns:
            基于 _project_root 的绝对 Path

        Note:
            不检查路径存在性 — 纯计算，不引入 I/O
        """
        value = self.value.format(**kwargs) if kwargs else self.value
        override = os.getenv(f"ELYSIA_PATH_{self.name}")
        if override:
            value = override
        return _project_root / value


def get_project_root() -> Path:
    """获取项目根目录（只读）。"""
    return _project_root
