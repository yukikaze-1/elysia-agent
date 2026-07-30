"""Tests for core/utils — set_request_id / get_request_id ContextVar propagation."""
import asyncio
import pytest
from core.utils import set_request_id, get_request_id

pytestmark = pytest.mark.unit


def test_set_and_get_request_id():
    """设置并读取 request_id。"""
    set_request_id("001")
    assert get_request_id() == "001"


def test_request_id_default():
    """未设置时返回空字符串。"""
    set_request_id("")
    assert get_request_id() == ""


def test_request_id_isolation_across_tasks():
    """不同 asyncio task 应有独立 request_id（ContextVar 隔离）。"""
    async def task_a():
        set_request_id("A")
        await asyncio.sleep(0.01)
        return get_request_id()

    async def task_b():
        set_request_id("B")
        await asyncio.sleep(0.01)
        return get_request_id()

    async def main():
        return await asyncio.gather(task_a(), task_b())

    results = asyncio.new_event_loop().run_until_complete(main())
    assert results == ["A", "B"]
