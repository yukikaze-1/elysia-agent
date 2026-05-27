"""Logging setup — routes noisy library logs to files, keeps terminal clean.

Noisy libraries (pymongo, pymilvus, httpx, openai, etc.) write to log/*.log.
Core application logs go to stderr at INFO (or DEBUG with --verbose).
Terminal stdout is reserved for conversation output only.

Warning suppression happens in demo.py before imports.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path


LOG_DIR = Path("log")


def setup(verbose: bool = False) -> None:
    """Configure logging: libraries → files, app → stderr, stdout clean."""
    LOG_DIR.mkdir(exist_ok=True)

    # Root logger: quiet, don't propagate to stdout
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.handlers.clear()

    # ---- Library loggers → files (DEBUG level, rotating would be overkill for now) ----

    _file_logger("pymongo", "mongo.log", logging.DEBUG)
    _file_logger("pymilvus", "milvus.log", logging.DEBUG)
    _file_logger("milvus_client", "milvus.log", logging.DEBUG)
    _file_logger("httpx", "http.log", logging.DEBUG)
    _file_logger("httpcore", "http.log", logging.DEBUG)
    _file_logger("openai", "http.log", logging.DEBUG)
    _file_logger("urllib3", "http.log", logging.DEBUG)
    _file_logger("sentence_transformers", "embedding.log", logging.DEBUG)
    _file_logger("transformers", "embedding.log", logging.WARNING)
    _file_logger("huggingface_hub", "embedding.log", logging.WARNING)
    # Also route core.memory.long_term INFO to embedding log (model loading messages)
    _file_logger("core.memory.long_term", "embedding.log", logging.DEBUG)

    # ---- Application loggers → stderr (INFO by default, DEBUG if --verbose) ----

    app_level = logging.DEBUG if verbose else logging.INFO
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(app_level)
    stderr_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    for name in ("core", "Module", "asyncio"):
        logger = logging.getLogger(name)
        logger.setLevel(app_level)
        logger.handlers.clear()
        logger.addHandler(stderr_handler)
        # Keep propagate=True so child loggers' messages flow up through this
        # handler. Child loggers (core.cognitive_loop etc.) have no own handlers.

    # Torch warnings → file
    _file_logger("torch", "torch.log", logging.WARNING)

    # Motor (MongoDB async driver) → mongo log
    _file_logger("motor", "mongo.log", logging.DEBUG)


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
        "%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.propagate = False
