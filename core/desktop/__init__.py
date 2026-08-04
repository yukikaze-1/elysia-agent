"""core.desktop — Desktop 工具桥包（批次 4 BE-041/043）。

DesktopToolBridge：桌面工具「请求 → 幂等去重 → 定向投递 → 结果回传」的核心并发组件
（锁 + future + 状态机 + 断连规则）。实现见 bridge.py；配套调用注册表在
core/storage/desktop_call_repo.py。
"""

from core.desktop.bridge import DesktopToolBridge

__all__ = ["DesktopToolBridge"]
