"""Timer and TimingClock unit tests."""

import time

import pytest

from core.cognitive.timing import Timer, TimingClock

pytestmark = pytest.mark.service


# ============================================================================
# Timer
# ============================================================================


class TestTimer:
    """Timer context manager tests."""

    def test_context_manager_records_elapsed_time(self):
        """Context manager records elapsed time (> 0ms)."""
        with Timer("test") as t:
            time.sleep(0.01)
        assert t.elapsed_ms > 0

    def test_elapsed_ms_is_int(self):
        """elapsed_ms is an int (cast from float milliseconds)."""
        with Timer("test") as t:
            time.sleep(0.01)
        assert isinstance(t.elapsed_ms, int)

    def test_elapsed_ms_increases_after_sleep(self):
        """elapsed_ms increases after sleep (sleep 0.02s -> at least 10ms)."""
        with Timer("test") as t:
            time.sleep(0.02)
        assert t.elapsed_ms >= 10

    def test_label_is_stored(self):
        """Timer stores the label passed at construction."""
        t = Timer("my-label")
        assert t.label == "my-label"

    def test_default_label_is_empty_string(self):
        """Timer default label is empty string."""
        t = Timer()
        assert t.label == ""

    def test_elapsed_ms_zero_before_enter(self):
        """elapsed_ms is 0 before the context manager is entered."""
        t = Timer("test")
        assert t.elapsed_ms == 0


# ============================================================================
# TimingClock
# ============================================================================


class TestTimingClock:
    """TimingClock tests."""

    def test_span_records_duration(self):
        """span() context manager records duration in clock._spans dict."""
        clock = TimingClock()
        with clock.span("prompt"):
            time.sleep(0.01)
        assert clock._spans["prompt"] > 0
        summary = clock.summary()
        assert "prompt" in summary
        assert "ms" in summary

    def test_multiple_spans_same_name_accumulate(self):
        """Multiple spans with same name accumulate durations."""
        clock = TimingClock()
        with clock.span("test"):
            time.sleep(0.01)
        with clock.span("test"):
            time.sleep(0.01)
        # Two spans of ~10ms each should total >= 15ms
        assert clock._spans["test"] >= 15
        assert clock._counts["test"] == 2
        # Summary includes count for repeated labels
        assert "(2)" in clock.summary()
        assert "test" in clock.summary()

    def test_total_ms_after_start_stop(self):
        """total_ms reflects wall-clock time from start() to stop()."""
        clock = TimingClock()
        clock.start()
        time.sleep(0.01)
        clock.stop()
        assert clock.total_ms > 0

    def test_summary_contains_span_names_and_ms(self):
        """summary() returns a string containing span names and 'ms'."""
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
        """reset() clears all spans and total."""
        clock = TimingClock()
        with clock.span("test"):
            time.sleep(0.01)
        assert clock._spans  # non-empty before reset
        clock.reset()
        assert len(clock._spans) == 0
        assert len(clock._counts) == 0
        assert clock.total_ms == 0

    def test_empty_clock_total_ms_zero(self):
        """Empty clock has total_ms == 0."""
        clock = TimingClock()
        assert clock.total_ms == 0

    def test_start_stop_lifecycle(self):
        """start()/stop() lifecycle works without error."""
        clock = TimingClock()
        clock.start()
        time.sleep(0.01)
        clock.stop()
        # No exception raised; total_ms reflects elapsed wall-clock time
        assert clock.total_ms > 0
        assert isinstance(clock.total_ms, int)

    def test_summary_sorted_descending(self):
        """summary() lists spans sorted by duration descending."""
        clock = TimingClock()
        with clock.span("short"):
            time.sleep(0.01)
        with clock.span("long"):
            time.sleep(0.03)
        s = clock.summary()
        # "long" should appear before "short" in the output
        assert s.index("long") < s.index("short")

    def test_reset_then_reuse(self):
        """After reset(), clock can be reused for new spans."""
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
        """start()/stop() with interleaved spans — total >= sum of spans."""
        clock = TimingClock()
        clock.start()
        with clock.span("a"):
            time.sleep(0.01)
        with clock.span("b"):
            time.sleep(0.01)
        clock.stop()
        span_sum = sum(clock._spans.values())
        # Wall-clock total should be at least the sum of individual spans
        assert clock.total_ms >= span_sum
