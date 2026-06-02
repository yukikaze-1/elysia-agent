from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import pytest

from Module.logger import LoggerFactory


@pytest.fixture
def lf():
    """创建 LoggerFactory，测试结束后必定清理。"""
    factory = LoggerFactory(setup_root=False)
    yield factory
    factory.close()


@pytest.fixture
def logdir():
    """为需要文件输出的测试提供临时目录。"""
    with tempfile.TemporaryDirectory() as d:
        yield d


# ── get() ────────────────────────────────────────────────────────────────


class TestGet:
    def test_returns_logger_with_correct_name(self, lf: LoggerFactory):
        logger = lf.get("TestModule")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "TestModule"

    def test_default_level_is_info(self, lf: LoggerFactory):
        logger = lf.get("TestInfo")
        assert logger.level == logging.INFO

    def test_custom_level(self, logdir: str):
        lf = LoggerFactory(level="DEBUG", log_dir=logdir, setup_root=False)
        logger = lf.get("TestDebug")
        assert logger.level == logging.DEBUG
        lf.close()

    def test_repeated_get_returns_same_logger(self, lf: LoggerFactory):
        a = lf.get("SameName")
        b = lf.get("SameName")
        assert a is b

    def test_handlers_not_duplicated(self, lf: LoggerFactory):
        lf.get("NoDup")
        handler_count = len(logging.getLogger("NoDup").handlers)
        lf.get("NoDup")
        assert len(logging.getLogger("NoDup").handlers) == handler_count


# ── log file output ──────────────────────────────────────────────────────


class TestFileOutput:
    def test_log_writes_to_file(self, logdir: str):
        lf = LoggerFactory(log_dir=logdir, setup_root=False)
        logger = lf.get("FileTest")
        logger.info("hello world")
        lf.close()

        log_file = Path(logdir) / "logger_FileTest.log"
        assert log_file.exists()
        assert "hello world" in log_file.read_text()

    def test_plain_format(self, logdir: str):
        lf = LoggerFactory(log_dir=logdir, json_format=False, setup_root=False)
        logger = lf.get("PlainFormat")
        logger.info("test message")
        lf.close()

        content = (Path(logdir) / "logger_PlainFormat.log").read_text()
        assert "[INFO]" in content
        assert "<PlainFormat>" in content
        assert "test message" in content


# ── JSON format ──────────────────────────────────────────────────────────


class TestJsonFormat:
    def test_json_format_parsable(self, logdir: str):
        lf = LoggerFactory(log_dir=logdir, json_format=True, setup_root=False)
        logger = lf.get("JsonTest")
        logger.info("json message")
        lf.close()

        log_file = Path(logdir) / "logger_JsonTest.log"
        line = log_file.read_text().strip()
        record = json.loads(line)
        assert record["level"] == "INFO"
        assert record["logger"] == "JsonTest"
        assert record["message"] == "json message"
        assert "time" in record


# ── module_levels ────────────────────────────────────────────────────────


class TestModuleLevels:
    def test_prefix_match(self, lf: LoggerFactory):
        lf._module_levels = {"L1": logging.DEBUG}
        l1 = lf.get("L1.BrainLayer")
        assert l1.level == logging.DEBUG

    def test_no_match_uses_default(self, lf: LoggerFactory):
        lf._module_levels = {"L1": logging.DEBUG}
        other = lf.get("SensorLayer")
        assert other.level == logging.INFO

    def test_exact_name_match(self, lf: LoggerFactory):
        lf._module_levels = {"ExactName": logging.ERROR}
        logger = lf.get("ExactName")
        assert logger.level == logging.ERROR

    def test_longer_prefix_wins(self, logdir: str):
        lf = LoggerFactory(
            level="INFO",
            module_levels={"L1": "DEBUG", "L1.Brain": "WARNING"},
            log_dir=logdir,
            setup_root=False,
        )
        logger = lf.get("L1.Brain.something")
        assert logger.level == logging.WARNING
        lf.close()


# ── set_level ────────────────────────────────────────────────────────────


class TestSetLevel:
    def test_runtime_level_change(self, lf: LoggerFactory):
        logger = lf.get("RuntimeLevel")
        assert logger.level == logging.INFO

        lf.set_level("RuntimeLevel", "DEBUG")
        assert logger.level == logging.DEBUG

    def test_runtime_level_with_numeric(self, lf: LoggerFactory):
        logger = lf.get("IntLevel")
        lf.set_level("IntLevel", 10)  # DEBUG
        assert logger.level == logging.DEBUG

    def test_unknown_level_raises(self, lf: LoggerFactory):
        with pytest.raises(ValueError, match="Unknown log level"):
            lf.set_level("Any", "TRACE")

    def test_affects_only_target_logger(self, lf: LoggerFactory):
        a = lf.get("LoggerA")
        b = lf.get("LoggerB")
        lf.set_level("LoggerA", "DEBUG")
        assert a.level == logging.DEBUG
        assert b.level == logging.INFO

    def test_set_level_actually_outputs_debug(self, logdir: str):
        """set_level 降级后 handler 级别同步更新，DEBUG 消息必须写入文件。"""
        lf = LoggerFactory(level="INFO", log_dir=logdir, setup_root=False)
        logger = lf.get("DynamicLevel")

        logger.debug("should not appear")
        lf.set_level("DynamicLevel", "DEBUG")
        logger.debug("should appear now")

        lf.close()
        content = (Path(logdir) / "logger_DynamicLevel.log").read_text()
        assert "should not appear" not in content
        assert "should appear now" in content


# ── root logger ──────────────────────────────────────────────────────────


class TestRootLogger:
    def test_root_handler_added_by_default(self):
        lf = LoggerFactory()
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        lf.close()

    def test_root_not_touched_when_setup_root_false(self):
        root_before = list(logging.getLogger().handlers)
        lf = LoggerFactory(setup_root=False)
        root_after = list(logging.getLogger().handlers)
        assert len(root_after) == len(root_before)

    def test_root_not_duplicated(self):
        lf1 = LoggerFactory()
        handler_count = len(logging.getLogger().handlers)
        lf2 = LoggerFactory()
        assert len(logging.getLogger().handlers) == handler_count


# ── close ────────────────────────────────────────────────────────────────


class TestClose:
    def test_close_removes_handlers(self, lf: LoggerFactory):
        logger = lf.get("CloseTest")
        assert len(logger.handlers) == 2
        lf.close()
        assert len(logger.handlers) == 0

    def test_close_clears_created_set(self, lf: LoggerFactory):
        lf.get("CloseSet")
        assert len(lf._created) == 1
        lf.close()
        assert len(lf._created) == 0


# ── constructor ──────────────────────────────────────────────────────────


class TestConstructor:
    def test_unknown_level_raises(self):
        with pytest.raises(ValueError, match="Unknown log level"):
            LoggerFactory(level="TRACE")

    def test_numeric_level(self):
        lf = LoggerFactory(level=10, setup_root=False)  # DEBUG
        assert lf._default_level == logging.DEBUG

    def test_level_zero_raises(self):
        with pytest.raises(ValueError, match="Invalid log level"):
            LoggerFactory(level=0)

    def test_level_negative_raises(self):
        with pytest.raises(ValueError, match="Invalid log level"):
            LoggerFactory(level=-1)

    def test_log_dir_is_created(self, logdir: str):
        log_dir = Path(logdir) / "nested" / "logs"
        lf = LoggerFactory(log_dir=str(log_dir), setup_root=False)
        lf.get("DirCreate")
        lf.close()
        assert log_dir.exists()


# ── exc_info / exception traceback ───────────────────────────────────────


class TestExcInfo:
    def _trigger_exception(self, logger: logging.Logger):
        try:
             _ = 1 / 0
        except ZeroDivisionError:
            logger.exception("Math error")

    def test_plain_format_includes_traceback(self, logdir: str):
        lf = LoggerFactory(log_dir=logdir, json_format=False, setup_root=False)
        logger = lf.get("ExcPlain")
        self._trigger_exception(logger)
        lf.close()

        content = (Path(logdir) / "logger_ExcPlain.log").read_text()
        assert "Math error" in content
        assert "ZeroDivisionError" in content
        assert "Traceback" in content

    def test_json_format_includes_exc_info(self, logdir: str):
        lf = LoggerFactory(log_dir=logdir, json_format=True, setup_root=False)
        logger = lf.get("ExcJson")
        self._trigger_exception(logger)
        lf.close()

        content = (Path(logdir) / "logger_ExcJson.log").read_text().strip()
        record = json.loads(content)
        assert record["message"] == "Math error"
        assert "exc_info" in record
        assert "ZeroDivisionError" in record["exc_info"]


# ============================================================
# CRITICAL level
# ============================================================

class TestCriticalLevel:
    def test_set_level_to_critical(self, lf):
        lg = lf.get("test_crit_set")
        lf.set_level("test_crit_set", "CRITICAL")
        assert lg.level == logging.CRITICAL


# ============================================================
# get_caller_logger
# ============================================================

class TestGetCallerLogger:
    def test_returns_logger_with_module_name(self):
        from .factory import get_caller_logger
        def _inner():
            return get_caller_logger()
        lg = _inner()
        assert "test_logger" in lg.name


# ============================================================
# Unicode in JSON
# ============================================================

class TestJsonUnicode:
    def test_json_format_with_unicode(self, logdir):
        import json
        lf = LoggerFactory(json_format=True, log_dir=str(logdir), setup_root=False)
        lg = lf.get("test_unicode")
        lg.info("中文测试 😀")
        lf.close()
        fpath = Path(logdir) / "logger_test_unicode.log"
        content = fpath.read_text().strip()
        record = json.loads(content)
        assert "中文测试" in record["message"]


# ============================================================
# Thread safety (smoke test)
# ============================================================

class TestConcurrency:
    def test_concurrent_get_is_safe(self, lf):
        import threading
        import queue as qmod

        results: qmod.Queue = qmod.Queue()

        def _get():
            try:
                lg = lf.get("concurrent_test")
                results.put(("ok", lg.name))
            except Exception as e:
                results.put(("error", str(e)))

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for _ in range(10):
            status, _ = results.get_nowait()
            assert status == "ok"
