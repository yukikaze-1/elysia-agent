"""可复用计时工具 — context manager + 请求级计时聚合。

用法:
    # 单次计时
    with Timer("prompt") as t:
        ...
    logger.info("prompt: %d ms", t.elapsed_ms)

    # 请求级聚合
    clock = TimingClock()
    with clock.span("prompt"):
        ...
    with clock.span("ttft"):
        ...
    logger.info("%s", clock.summary())  # prompt:45ms | ttft:380ms
"""

from __future__ import annotations

import time
from collections import defaultdict


class Timer:
    """单次计时 — context manager."""

    __slots__ = ("_label", "_t0", "_elapsed_ms")

    def __init__(self, label: str = "") -> None:
        self._label = label
        self._t0: float = 0.0
        self._elapsed_ms: int = 0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self._elapsed_ms = int((time.perf_counter() - self._t0) * 1000)

    @property
    def elapsed_ms(self) -> int:
        return self._elapsed_ms

    @property
    def label(self) -> str:
        return self._label


class TimingClock:
    """请求级计时聚合 — 收集多个 span，输出汇总。

    span(label) 返回 Timer context manager，同时自动记录到内部字典。
    同名 label 多次调用会自动累计。
    """

    def __init__(self) -> None:
        self._spans: dict[str, int] = defaultdict(int)
        self._counts: dict[str, int] = defaultdict(int)
        self._t_total: int = 0

    def span(self, label: str) -> Timer:
        """开始一个计时段。返回 Timer context manager。
        结束时自动将耗时累计到 label 名下。
        """
        clock = self  # 捕获 TimingClock 引用供内部类访问

        class _AccumTimer(Timer):
            def __exit__(_self, *args: object) -> None:
                super().__exit__(*args)
                clock._spans[label] += _self._elapsed_ms
                clock._counts[label] += 1

        return _AccumTimer(label)

    def start(self) -> None:
        """标记请求开始时间。"""
        self._t_total = time.perf_counter_ns()

    def stop(self) -> None:
        """标记请求结束，计算总耗时。"""
        self._t_total = int((time.perf_counter_ns() - self._t_total) / 1_000_000)

    @property
    def total_ms(self) -> int:
        return self._t_total

    def summary(self) -> str:
        """生成单行汇总，按耗时降序排列。
        格式: "total_ms | label1:XXms | label2(N):XXms"
        """
        parts = [f"{self._t_total}ms"]
        for label, elapsed in sorted(self._spans.items(), key=lambda x: -x[1]):
            count = self._counts[label]
            if count > 1:
                parts.append(f"{label}({count}):{elapsed}ms")
            else:
                parts.append(f"{label}:{elapsed}ms")
        return " | ".join(parts)

    def reset(self) -> None:
        self._spans.clear()
        self._counts.clear()
        self._t_total = 0
