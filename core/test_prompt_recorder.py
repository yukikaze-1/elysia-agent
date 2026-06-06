# core/test_prompt_recorder.py
"""PromptRecorder 单元测试"""
import pytest
from datetime import datetime
from core.prompt_recorder import PromptRecorder, PromptRecord


class TestPromptRecord:
    def test_record_derived_fields_simple_text(self):
        """纯文本 messages 的派生字段计算"""
        record = PromptRecord(
            index=1,
            timestamp="2026-06-06T15:30:00",
            round_num=0,
            model="deepseek/deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
            tools=None,
        )
        # 派生字段由 record() 方法计算，这里验证直接构造后的计算逻辑
        from core.prompt_recorder import _compute_derived_fields
        _compute_derived_fields(record)
        assert record.message_count == 2
        assert record.system_prompt_len == 28  # len("You are a helpful assistant.")
        assert record.total_chars == 28 + 5  # system + "Hello"

    def test_record_derived_fields_multimodal_content(self):
        """多模态 content（list[dict]）的字符数计算"""
        record = PromptRecord(
            index=2,
            timestamp="2026-06-06T15:30:01",
            round_num=1,
            model="deepseek/deepseek-chat",
            messages=[
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                    {"type": "text", "text": "描述这张图"},
                ]},
            ],
            tools=None,
        )
        from core.prompt_recorder import _compute_derived_fields
        _compute_derived_fields(record)
        assert record.message_count == 2
        assert record.system_prompt_len == 13  # len("System prompt")
        # total_chars = system(13) + image(20) + user text "描述这张图"(5)
        assert record.total_chars == 38


class TestPromptRecorder:
    def test_record_and_retrieve(self):
        """记录一条 prompt 后可通过 index 检索"""
        recorder = PromptRecorder(max_records=20)
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        recorder.record(messages=messages, tools=None, round_num=0, model="test/model")

        summaries = recorder.list_summaries()
        assert summaries["total"] == 1
        assert summaries["max"] == 20
        assert len(summaries["records"]) == 1
        s = summaries["records"][0]
        assert s["index"] == 1
        assert s["message_count"] == 2
        assert s["round_num"] == 0
        assert s["model"] == "test/model"
        # summaries 不应包含完整 messages
        assert "messages" not in s

        full = recorder.get_full(1)
        assert full is not None
        assert full.index == 1
        assert len(full.messages) == 2

    def test_get_full_returns_none_for_missing_index(self):
        """查询不存在的 index 返回 None"""
        recorder = PromptRecorder(max_records=20)
        assert recorder.get_full(999) is None

    def test_ring_buffer_eviction(self):
        """超过 max_records 时旧记录被淘汰"""
        recorder = PromptRecorder(max_records=3)
        for i in range(5):
            recorder.record(
                messages=[{"role": "user", "content": f"msg{i}"}],
                tools=None, round_num=0, model="test/model",
            )

        summaries = recorder.list_summaries()
        assert summaries["total"] == 3
        # 最旧的 index=1,2 被淘汰，保留 index=3,4,5
        indices = [r["index"] for r in summaries["records"]]
        assert indices == [3, 4, 5]
        assert recorder.get_full(1) is None
        assert recorder.get_full(5) is not None

    def test_list_summaries_empty(self):
        """无记录时返回空列表"""
        recorder = PromptRecorder(max_records=20)
        summaries = recorder.list_summaries()
        assert summaries == {"records": [], "total": 0, "max": 20}

    def test_index_monotonic(self):
        """index 全局自增，不受淘汰影响"""
        recorder = PromptRecorder(max_records=2)
        recorder.record(messages=[], tools=None, round_num=0, model="a")
        recorder.record(messages=[], tools=None, round_num=0, model="b")
        recorder.record(messages=[], tools=None, round_num=0, model="c")
        assert recorder.get_full(1) is None
        assert recorder.get_full(3) is not None
        assert recorder._counter == 3
