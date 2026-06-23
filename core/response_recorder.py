# core/response_recorder.py
"""LLM Response 记录器 — 内存环形缓冲区，捕获每次 LLM 调用的原始回复文本。"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ResponseRecord:
    """一次 LLM 调用的原始回复记录。"""
    index: int
    timestamp: str
    round_num: int
    raw_text: str         # 完整原始文本（含 <inner_voice> 标签）
    inner_voice: str | None  # 剥离的 inner_voice 内容
    text_len: int = 0
    inner_voice_len: int = 0


class ResponseRecorder:
    """内存环形缓冲区，保存最近 N 次 LLM 调用的原始回复。

    用法:
        recorder = ResponseRecorder(max_records=20)
        recorder.record(raw_text=acc_text, inner_voice=iv_think_buf, round_num=0)
        summaries = recorder.list_summaries()
        full = recorder.get_full(index=5)
    """

    def __init__(self, max_records: int = 20):
        self._records: deque[ResponseRecord] = deque(maxlen=max_records)
        self._counter: int = 0
        self._max_records = max_records

    def record(
        self,
        raw_text: str,
        inner_voice: str | None,
        round_num: int = 0,
    ) -> ResponseRecord:
        """保存一次 LLM 调用的原始回复。"""
        self._counter += 1
        record = ResponseRecord(
            index=self._counter,
            timestamp=datetime.now(timezone.utc).isoformat(),
            round_num=round_num,
            raw_text=raw_text,
            inner_voice=inner_voice.strip() if inner_voice else None,
            text_len=len(raw_text),
            inner_voice_len=len(inner_voice.strip()) if inner_voice and inner_voice.strip() else 0,
        )
        self._records.append(record)
        return record

    def list_summaries(self) -> dict:
        """返回轻量摘要列表，供 DebugInfo 使用。不包含完整 raw_text。"""
        records = []
        for r in self._records:
            records.append({
                "index": r.index,
                "timestamp": r.timestamp,
                "round_num": r.round_num,
                "text_len": r.text_len,
                "inner_voice_len": r.inner_voice_len,
            })
        return {
            "records": records,
            "total": len(records),
            "max": self._max_records,
        }

    def get_full(self, index: int) -> ResponseRecord | None:
        """按序号获取完整记录。"""
        for r in self._records:
            if r.index == index:
                return r
        return None
