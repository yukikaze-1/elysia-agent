# core/prompt_recorder.py
"""LLM Prompt 记录器 — 内存环形缓冲区，捕获每次 LLM 调用的完整 messages 数组。"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.schema.types import IsoTimestamp


@dataclass
class PromptRecord:
    """一次 LLM 调用的完整 prompt 记录。"""
    index: int
    timestamp: IsoTimestamp
    round_num: int
    model: str
    messages: list[dict]
    tools: list[dict] | None = None
    message_count: int = 0
    system_prompt_len: int = 0
    total_chars: int = 0


def _content_len(content) -> int:
    """计算单条消息 content 的字符数，兼容 str 和 list[dict] 格式。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += len(part.get("text", "") or part.get("content", "") or "")
                elif part.get("type") == "image_url":
                    total += 20  # 图片粗略计数
        return total
    return 0


def _compute_derived_fields(record: PromptRecord) -> None:
    """计算并填充派生字段（message_count, system_prompt_len, total_chars）。"""
    record.message_count = len(record.messages)
    record.total_chars = 0
    record.system_prompt_len = 0
    for msg in record.messages:
        clen = _content_len(msg.get("content", ""))
        record.total_chars += clen
        if msg.get("role") == "system":
            record.system_prompt_len = clen


class PromptRecorder:
    """内存环形缓冲区，保存最近 N 次 LLM 调用的完整 prompt。

    用法:
        recorder = PromptRecorder(max_records=20)
        recorder.record(messages=messages, tools=tool_defs, round_num=0, model="deepseek/deepseek-chat")
        summaries = recorder.list_summaries()  # 轻量摘要
        full = recorder.get_full(index=5)      # 完整记录
    """

    def __init__(self, max_records: int = 20):
        self._records: deque[PromptRecord] = deque(maxlen=max_records)
        self._counter: int = 0
        self._max_records = max_records

    def record(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        round_num: int,
        model: str,
    ) -> PromptRecord:
        """保存一次 LLM 调用的 prompt。"""
        self._counter += 1
        record = PromptRecord(
            index=self._counter,
            timestamp=datetime.now(timezone.utc).isoformat(),
            round_num=round_num,
            model=model,
            messages=messages,
            tools=tools,
        )
        _compute_derived_fields(record)
        self._records.append(record)
        return record

    def list_summaries(self) -> dict:
        """返回轻量摘要列表，供 DebugInfo 使用。不包含完整 messages。"""
        records = []
        for r in self._records:
            records.append({
                "index": r.index,
                "timestamp": r.timestamp,
                "round_num": r.round_num,
                "message_count": r.message_count,
                "system_prompt_len": r.system_prompt_len,
                "total_chars": r.total_chars,
                "model": r.model,
            })
        return {
            "records": records,
            "total": len(records),
            "max": self._max_records,
        }

    def get_full(self, index: int) -> PromptRecord | None:
        """按序号获取完整记录。"""
        for r in self._records:
            if r.index == index:
                return r
        return None
