"""Timer 与 TimingClock 单元测试。"""

import time

import pytest

from core.cognitive.timing import Timer, TimingClock

pytestmark = pytest.mark.service


# ============================================================================
# Timer（计时器）
# ============================================================================


class TestTimer:
    """Timer 上下文管理器测试。"""

    def test_context_manager_records_elapsed_time(self):
        """上下文管理器记录耗时（> 0ms）。"""
        with Timer("test") as t:
            time.sleep(0.01)
        assert t.elapsed_ms > 0

    def test_elapsed_ms_is_int(self):
        """elapsed_ms 是整数（由浮点毫秒转换而来）。"""
        with Timer("test") as t:
            time.sleep(0.01)
        assert isinstance(t.elapsed_ms, int)

    def test_elapsed_ms_increases_after_sleep(self):
        """sleep 后 elapsed_ms 增大（sleep 0.02s → 至少 10ms）。"""
        with Timer("test") as t:
            time.sleep(0.02)
        assert t.elapsed_ms >= 10

    def test_label_is_stored(self):
        """Timer 保存构造时传入的 label。"""
        t = Timer("my-label")
        assert t.label == "my-label"

    def test_default_label_is_empty_string(self):
        """Timer 默认 label 为空字符串。"""
        t = Timer()
        assert t.label == ""

    def test_elapsed_ms_zero_before_enter(self):
        """进入上下文管理器前 elapsed_ms 为 0。"""
        t = Timer("test")
        assert t.elapsed_ms == 0


# ============================================================================
# TimingClock（计时时钟）
# ============================================================================


class TestTimingClock:
    """TimingClock 测试。"""

    def test_span_records_duration(self):
        """span() 上下文管理器将时长记录到 clock._spans 字典。"""
        clock = TimingClock()
        with clock.span("prompt"):
            time.sleep(0.01)
        assert clock._spans["prompt"] > 0
        summary = clock.summary()
        assert "prompt" in summary
        assert "ms" in summary

    def test_multiple_spans_same_name_accumulate(self):
        """同名多个 span 的时长会累加。"""
        clock = TimingClock()
        with clock.span("test"):
            time.sleep(0.01)
        with clock.span("test"):
            time.sleep(0.01)
        # 两个约 10ms 的 span 合计应 >= 15ms
        assert clock._spans["test"] >= 15
        assert clock._counts["test"] == 2
        # summary 包含重复标签的计数
        assert "(2)" in clock.summary()
        assert "test" in clock.summary()

    def test_total_ms_after_start_stop(self):
        """total_ms 反映从 start() 到 stop() 的墙钟时间。"""
        clock = TimingClock()
        clock.start()
        time.sleep(0.01)
        clock.stop()
        assert clock.total_ms > 0

    def test_summary_contains_span_names_and_ms(self):
        """summary() 返回包含 span 名称与 'ms' 的字符串。"""
        clock = TimingClock()
        with clock.span("alpha"):
            time.sleep(0.01)
        with clock.span("beta"):
            time.sleep(0.01)
        s = clock.summary()
        assert "alpha" in s
        assert "beta" in s
        assert "ms" in s

    def test_reset_clears_all_spans(self):
        """reset() 清空所有 span 与 total。"""
        clock = TimingClock()
        with clock.span("test"):
            time.sleep(0.01)
        assert clock._spans  # non-empty before reset
        clock.reset()
        assert len(clock._spans) == 0
        assert len(clock._counts) == 0
        assert clock.total_ms == 0

    def test_empty_clock_total_ms_zero(self):
        """空 clock 的 total_ms == 0。"""
        clock = TimingClock()
        assert clock.total_ms == 0

    def test_start_stop_lifecycle(self):
        """start()/stop() 生命周期应无错运行。"""
        clock = TimingClock()
        clock.start()
        time.sleep(0.01)
        clock.stop()
        # 未抛异常；total_ms 反映经过的墙钟时间
        assert clock.total_ms > 0
        assert isinstance(clock.total_ms, int)

    def test_summary_sorted_descending(self):
        """summary() 按时长降序列出 span。"""
        clock = TimingClock()
        with clock.span("short"):
            time.sleep(0.01)
        with clock.span("long"):
            time.sleep(0.03)
        s = clock.summary()
        # "long" should appear before "short" in the output
        assert s.index("long") < s.index("short")

    def test_reset_then_reuse(self):
        """reset() 之后，clock 可复用于新的 span。"""
        clock = TimingClock()
        with clock.span("first"):
            time.sleep(0.01)
        clock.reset()
        with clock.span("second"):
            time.sleep(0.01)
        assert "second" in clock.summary()
        assert "first" not in clock.summary()
        assert clock._spans["second"] > 0

    def test_start_stop_with_spans(self):
        """start()/stop() 带交错 span —— total >= 各 span 之和。"""
        clock = TimingClock()
        clock.start()
        with clock.span("a"):
            time.sleep(0.01)
        with clock.span("b"):
            time.sleep(0.01)
        clock.stop()
        span_sum = sum(clock._spans.values())
        # 墙钟总时长应至少等于各 span 之和
        assert clock.total_ms >= span_sum
