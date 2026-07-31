"""日志工厂 — 基于 Python logging 的配置化 logger 创建。

支持 plain / JSON 双格式、按模块差异化日志级别、root logger 兜底。
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LEVEL_MAP: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _resolve_level(level: str | int) -> int:
    """将字符串或 int 日志级别转为 int。

    Args:
        level: 日志级别字符串（如 "DEBUG"、"INFO"）或整数。

    Returns:
        对应的 logging 级别整数。

    Raises:
        ValueError: level 为无效值。
    """
    if isinstance(level, int):
        if level <= 0:
            raise ValueError(f"Invalid log level: {level}")
        return level
    upper = level.upper()
    if upper in _LEVEL_MAP:
        return _LEVEL_MAP[upper]
    raise ValueError(f"Unknown log level: {level!r}")


def get_caller_logger() -> logging.Logger:
    """返回调用方模块的 logger。

    遍历调用栈找到调用方所在模块，返回 logging.getLogger(module_name)。
    供调用方在未显式传入 logger 时注入。

    调用点是直接调用（如 Module/output/websocket.py __init__ 内），
    中间无 Factory.create() 层——因此只需一层 f_back 即可取到调用方模块。
    之前双跳 f_back.f_back 会把 logger 名归属到调用方的调用者，
    导致 %(name)s 来源错乱、module_levels 前缀匹配失效。
    """
    frame = inspect.currentframe()
    try:
        caller = frame.f_back  # type: ignore[union-attr]
        module_name = caller.f_globals.get("__name__", "__main__")  # type: ignore[union-attr]
        return logging.getLogger(module_name)
    finally:
        del frame


class _JsonFormatter(logging.Formatter):
    """JSON 行格式化器 — 每条日志一行 JSON。"""

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 格式化为 JSON 字符串。

        Args:
            record: Python logging 的日志记录。

        Returns:
            单行 JSON 字符串。
        """
        data: dict[str, object] = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


_PLAIN_FMT = "%(asctime)s - [%(levelname)s] - <%(name)s>: %(message)s"
_PLAIN_DATEFMT = "%Y-%m-%d %H:%M:%S"


class LoggerFactory:
    """日志工厂 — 创建并管理 logger 实例，支持多模块差异化级别。

    Args:
        log_dir: 日志文件目录。
        level: 默认日志级别（字符串或 int）。
        json_format: 是否使用 JSON 格式输出。
        module_levels: 按模块前缀设置差异级别，如 {"Module.llm": "DEBUG"}。
        setup_root: 是否初始化 root logger handler。
        backup_count: 日志文件备份天数。
    """

    def __init__(
        self,
        *,
        log_dir: str = "log",
        level: str | int = "INFO",
        json_format: bool = False,
        module_levels: dict[str, str] | None = None,
        setup_root: bool = True,
        backup_count: int = 30,
    ):
        self._log_dir = Path(log_dir)
        self._default_level = _resolve_level(level)
        self._json_format = json_format
        self._module_levels: dict[str, int] = {
            prefix: _resolve_level(lv) for prefix, lv in (module_levels or {}).items()
        }
        self._backup_count = backup_count
        self._created: set[str] = set()
        self._root_handler: logging.Handler | None = None

        if json_format:
            self._formatter = _JsonFormatter()
            self._formatter.default_msec_format = "%s.%03d"  # type: ignore[attr-defined]
        else:
            self._formatter = logging.Formatter(_PLAIN_FMT, datefmt=_PLAIN_DATEFMT)

        if setup_root:
            self._setup_root_handler()

    # ── public API ──────────────────────────────────────────────────

    def get(self, name: str) -> logging.Logger:
        """获取或创建指定名称的 logger。

        Args:
            name: logger 名称（通常是模块路径）。

        Returns:
            配置好的 Logger 实例。若已存在同名 handler 则直接返回。
        """
        logger = logging.getLogger(name)
        if logger.handlers:
            return logger

        level = self._resolve_module_level(name)
        logger.setLevel(level)

        logger.addHandler(self._make_stream_handler(level))
        logger.addHandler(self._make_file_handler(name, level))

        self._created.add(name)
        return logger

    def set_level(self, name: str, level: str | int) -> None:
        """动态修改指定 logger 及其所有 handler 的日志级别。

        Args:
            name: logger 名称。
            level: 新日志级别（字符串或整数）。
        """
        lv = _resolve_level(level)
        logger = logging.getLogger(name)
        logger.setLevel(lv)
        for h in logger.handlers:
            h.setLevel(lv)

    def close(self) -> None:
        """关闭所有由本工厂创建的 logger handler 及 root handler。"""
        for name in list(self._created):
            logger = logging.getLogger(name)
            for h in list(logger.handlers):
                logger.removeHandler(h)
                h.close()
        self._created.clear()

        if self._root_handler is not None:
            root = logging.getLogger()
            root.removeHandler(self._root_handler)
            self._root_handler.close()
            self._root_handler = None

    # ── internal ────────────────────────────────────────────────────

    def _resolve_module_level(self, name: str) -> int:
        """最长前缀匹配模块日志级别，无匹配则使用默认级别。

        Args:
            name: 模块名（如 "Module.llm.openai_compat"）。

        Returns:
            匹配的日志级别整数。
        """
        for prefix, lv in sorted(self._module_levels.items(), key=lambda x: -len(x[0])):
            if name == prefix or name.startswith(prefix + "."):
                return lv
        return self._default_level

    def _make_stream_handler(self, level: int) -> logging.Handler:
        """创建 stdout 流 handler。

        Args:
            level: 日志级别。

        Returns:
            配置好的 StreamHandler 实例。
        """
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(level)
        h.setFormatter(self._formatter)
        return h

    def _make_file_handler(self, name: str, level: int) -> logging.Handler:
        """创建按天轮转的文件 handler。

        Args:
            name: logger 名称（用于生成文件名）。
            level: 日志级别。

        Returns:
            配置好的 TimedRotatingFileHandler 实例。
        """
        self._log_dir.mkdir(parents=True, exist_ok=True)
        fpath = self._log_dir / f"logger_{name}.log"
        h = TimedRotatingFileHandler(
            str(fpath), when="midnight", backupCount=self._backup_count, encoding="utf-8"
        )
        h.setLevel(level)
        h.setFormatter(self._formatter)
        return h

    def _setup_root_handler(self) -> None:
        """为 root logger 安装兜底 handler（仅 WARNING 级别）。"""
        root = logging.getLogger()
        if not root.handlers:
            h = logging.StreamHandler(sys.stdout)
            h.setLevel(logging.WARNING)
            h.setFormatter(self._formatter)
            root.addHandler(h)
            self._root_handler = h
