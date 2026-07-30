"""Logging setup — double-file output, clean terminal.

Noisy libraries write to log/*.log (mongo.log, milvus.log, http.log, etc.).
Core app: stderr (INFO, or DEBUG with --verbose) + app.log (INFO+) + app-debug.log (DEBUG).
Stdout reserved for conversation output only.
"""

import contextvars
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from paths import ProjectPath


LOG_DIR = ProjectPath.LOG.resolve()

# 请求 ID — CognitiveLoop 在每轮开始时设置，日志 Filter 自动注入到 LogRecord
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# 对话日志 — 独立于 stderr，只写 log/conversation.log
_conv_logger: logging.Logger | None = None


def get_request_id() -> str:
    """获取当前请求 ID（供 logger 之外使用）。"""
    return _request_id_var.get()


def set_request_id(rid: str) -> None:
    """设置当前请求 ID。"""
    _request_id_var.set(rid)


def log_conversation(role: str, name: str, text: str) -> None:
    """记录对话内容到 log/conversation.log，与后台日志分离。

    Args:
        role: "user" 或 "assistant"
        name: 说话者名称（用户昵称或角色名）
        text: 完整对话文本
    """
    if _conv_logger is None:
        return
    prefix = ">>>" if role == "user" else "<<<"
    _conv_logger.info("%s %s: %s", prefix, name, text)


class _RequestIdFilter(logging.Filter):
    """将 ContextVar 中的请求 ID 注入 LogRecord，供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """将当前请求 ID 注入 LogRecord。

        Args:
            record: 日志记录

        Returns:
            bool: 始终返回 True，不过滤任何记录
        """
        record.request_id = _request_id_var.get()
        return True


def setup(verbose: bool = False) -> None:
    """配置日志系统：库日志 → 文件，应用日志 → stderr + 文件，stdout 保持干净。

    Args:
        verbose: 若为 True，stderr 输出 DEBUG 级别日志
    """
    LOG_DIR.mkdir(exist_ok=True)

    # Root logger: quiet, don't propagate to stdout
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.handlers.clear()

    # ---- Library loggers → files (DEBUG level) ----

    _file_logger("pymongo", "mongo.log", logging.WARNING)
    _file_logger("pymilvus", "milvus.log", logging.WARNING)
    _file_logger("milvus_client", "milvus.log", logging.WARNING)
    _file_logger("httpx", "http.log", logging.WARNING)
    _file_logger("httpcore", "http.log", logging.WARNING)
    _file_logger("openai", "http.log", logging.WARNING)
    _file_logger("urllib3", "http.log", logging.WARNING)
    _file_logger("sentence_transformers", "embedding.log", logging.WARNING)
    _file_logger("transformers", "embedding.log", logging.WARNING)
    _file_logger("huggingface_hub", "embedding.log", logging.WARNING)
    _file_logger("core.memory.long_term", "embedding.log", logging.DEBUG)

    # ---- Application loggers → stderr + app.log (INFO) + app-debug.log (DEBUG) ----

    # Logger 级别设为 DEBUG，让所有消息都能产生。各 handler 独立控制输出阈值。
    app_level = logging.DEBUG if verbose else logging.INFO
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(app_level)
    stderr_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    req_filter = _RequestIdFilter()
    stderr_handler.addFilter(req_filter)

    # ---- Application log → file (persistent, INFO+) ----
    app_file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log", encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    app_file_handler.setLevel(logging.INFO)
    app_file_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    app_file_handler.addFilter(req_filter)

    # ---- Debug log → file (always DEBUG, dev-only) ----
    debug_file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app-debug.log", encoding="utf-8", maxBytes=20 * 1024 * 1024, backupCount=5
    )
    debug_file_handler.setLevel(logging.DEBUG)
    debug_file_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    debug_file_handler.addFilter(req_filter)

    for name in ("core", "Module", "asyncio"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)  # allow all messages; handlers filter by level
        logger.handlers.clear()
        logger.addHandler(stderr_handler)
        logger.addHandler(app_file_handler)
        logger.addHandler(debug_file_handler)
        # Keep propagate=True so child loggers' messages flow up through these
        # handlers. Child loggers (core.cognitive_loop etc.) have no own handlers.

    # ---- Database operation log → mongo.log (app-level, not library) ----
    _mongo_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "mongo.log", encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    _mongo_handler.setLevel(logging.DEBUG)
    _mongo_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _mongo_handler.addFilter(req_filter)
    for name in ("core.storage.session_repo", "core.storage.message_repo",
                 "core.storage.token_repo", "core.storage.character_version_repo",
                 "core.memory.session", "core.monitoring.token_tracker"):
        db_logger = logging.getLogger(name)
        db_logger.addHandler(_mongo_handler)

    # ---- Vector DB operation log → milvus.log (app-level) ----
    _milvus_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "milvus.log", encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    _milvus_handler.setLevel(logging.DEBUG)
    _milvus_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _milvus_handler.addFilter(req_filter)
    for name in ("core.memory.long_term", "core.storage.memory_repo"):
        milvus_logger = logging.getLogger(name)
        milvus_logger.addHandler(_milvus_handler)

    # Torch warnings → file
    _file_logger("torch", "torch.log", logging.WARNING)

    # Motor (MongoDB async driver) → mongo log
    _file_logger("motor", "mongo.log", logging.WARNING)

    # ---- Conversation content → log/conversation.log ----
    global _conv_logger
    _conv_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "conversation.log", encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    _conv_handler.setLevel(logging.INFO)
    _conv_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _conv_handler.addFilter(req_filter)
    _conv_logger = logging.getLogger("core.conversation")
    _conv_logger.setLevel(logging.INFO)
    _conv_logger.handlers.clear()
    _conv_logger.addHandler(_conv_handler)
    _conv_logger.propagate = False  # 不混入 stderr


def _file_logger(name: str, filename: str, level: int) -> None:
    """将指定 logger 路由到文件，与 root logger 分离。

    Args:
        name: logger 名称
        filename: 日志文件名（相对于 LOG_DIR）
        level: 日志级别
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / filename, encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.propagate = False
