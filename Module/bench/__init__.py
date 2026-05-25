"""
统一延迟基准测试模块。

覆盖 LLM、TTS、STT 三个模块的流式和非流式路径，
测量总耗时 / 首 token / 吞吐率 / 实时率 (RTF) 等指标。

运行: python -m Module.bench
"""

from .bench import LatencyBench

__all__ = ["LatencyBench"]
