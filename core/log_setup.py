"""Logging setup — routes noisy library logs to files, keeps terminal clean.

Noisy libraries (pymongo, pymilvus, httpx, openai, etc.) write to log/*.log.
Core application logs go to stderr at INFO (or DEBUG with --verbose).
Terminal stdout is reserved for conversation output only.

Warning suppression happens in demo.py before imports.
"""

import contextvars
import logging
import logging.handlers
import os
import sys
from pathlib import Path


LOG_DIR = Path("log")

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
        record.request_id = _request_id_var.get()
        return True


def setup(verbose: bool = False) -> None:
    """Configure logging: libraries → files, app → stderr, stdout clean."""
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

    # ---- Application loggers → stderr (INFO by default, DEBUG if --verbose) ----

    app_level = logging.DEBUG if verbose else logging.INFO
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(app_level)
    stderr_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    req_filter = _RequestIdFilter()
    stderr_handler.addFilter(req_filter)

    # ---- Application log → file (persistent) ----
    app_file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log", encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    app_file_handler.setLevel(app_level)
    app_file_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s: [%(request_id)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    app_file_handler.addFilter(req_filter)

    for name in ("core", "Module", "asyncio"):
        logger = logging.getLogger(name)
        logger.setLevel(app_level)
        logger.handlers.clear()
        logger.addHandler(stderr_handler)
        logger.addHandler(app_file_handler)
        # Keep propagate=True so child loggers' messages flow up through these
        # handlers. Child loggers (core.cognitive_loop etc.) have no own handlers.

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
    """Route a logger to a file, detached from root."""
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
