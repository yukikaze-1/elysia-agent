"""agent/ 测试共享 fixtures。"""

import asyncio
from collections import deque
from unittest.mock import MagicMock, AsyncMock
import pytest

from core.schema import AgentState


@pytest.fixture
def fake_agent():
    """返回一个属性可动态设置的 mock agent——模拟 30+ 子系统。

    测试只需设自己关心的属性，其余保持默认。
    """
    a = MagicMock()
    # 持久化
    a._consolidator = None
    a._character_engine = None
    a._memory_reflector = None
    # session（带 deque messages）
    a._session = MagicMock()
    a._session.save = AsyncMock()
    a._session.messages = deque(maxlen=20)
    # event bus + fsm + status
    a._bus = MagicMock()
    a._bus.post = MagicMock()
    a._fsm = MagicMock()
    a._fsm.state = AgentState.IDLE  # 真实枚举值，避免 MagicMock 比较恒真
    a.status = MagicMock()
    a.status.is_accepting_input = True
    a.status.current_name.return_value = "idle"
    # 后台任务
    a._background_tasks = {}
    a._task_system = MagicMock()
    a._task_system.list_active_tasks = AsyncMock(return_value=[])
    a._task_system.get_task_mailbox = MagicMock()
    # config（banner 测试用）
    a._config = MagicMock()
    a._character = MagicMock()
    a._llm = MagicMock()
    a._long_term = MagicMock()
    a._tool_adapter = MagicMock()
    return a
