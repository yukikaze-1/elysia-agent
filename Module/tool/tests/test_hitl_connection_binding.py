# 测试文件：Module/tool/tests/test_hitl_connection_binding.py
"""HITL 审批绑定发起连接（CR-13）。

缺陷原状：`submit_approval` 只查 `request_id` 是否存在，而该 ID 由
`ToolManager._broadcast_hitl` **广播到全部通道**。多客户端共享产品 token 的远程
部署下，B 从自己收到的广播帧里**直接拿到 request_id**（无需猜测），即可批准/
否决 A 触发的高危工具调用（`write_file` risk=high、桌面写、截屏）。

对照：孪生路径 `tool.status` 早已有 `sender_connection_id == self._connection_id`
绑定（批次 8a/D2）——同一个信任模型没有施加到审批上。

覆盖三层：
1. 安全管道：绑定校验（发起连接 / 他人连接 / 无绑定回退）；
2. 广播面：发起连接已知时**只发给它**（request_id 不再外泄给无关客户端）；
3. 端到端：ToolContext.services → 安全管道登记 → WS handler 提交。
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from Module.tool.safety import SafetyPipeline, _origin_connection_id

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# 1. 安全管道：绑定校验
# ═══════════════════════════════════════════════════════════════════

def _register(pipeline: SafetyPipeline, request_id: str, origin: str) -> None:
    """按 _run_hitl 的登记形状写入一条待审批请求。"""
    pipeline._hitl_requests[request_id] = (      # noqa: SLF001
        asyncio.Event(), "write_file", {"path": "/etc/x"}, origin)


def test_approval_from_origin_connection_accepted():
    p = SafetyPipeline()
    _register(p, "r1", "conn_a")
    assert p.submit_approval("r1", True, "conn_a") is True


def test_approval_from_other_connection_rejected(caplog):
    """核心：B 拿到 request_id 也批不了 A 的高危调用。"""
    import logging
    p = SafetyPipeline()
    _register(p, "r1", "conn_a")
    with caplog.at_level(logging.WARNING, logger="Module.tool.safety"):
        assert p.submit_approval("r1", True, "conn_b") is False
    assert "r1" not in p._hitl_results, "被拒的审批不得写入结果"       # noqa: SLF001
    assert not p._hitl_requests["r1"][0].is_set(), "被拒的审批不得唤醒等待方"  # noqa: SLF001
    assert any("发起连接" in r.getMessage() for r in caplog.records), caplog.text


def test_approval_without_approver_id_rejected_when_bound():
    """提交方没报身份（None）也不能绕过绑定。"""
    p = SafetyPipeline()
    _register(p, "r1", "conn_a")
    assert p.submit_approval("r1", True, None) is False
    assert p.submit_approval("r1", True) is False


def test_unbound_request_accepts_but_warns(caplog):
    """发起方无连接身份（v1/REST）→ 只能放行，但必须告警留痕，不假装有归属。"""
    import logging
    p = SafetyPipeline()
    _register(p, "r1", "")
    with caplog.at_level(logging.WARNING, logger="Module.tool.safety"):
        assert p.submit_approval("r1", True, "conn_b") is True
    assert any("无连接绑定" in r.getMessage() for r in caplog.records), caplog.text


def test_unknown_request_still_returns_false():
    """回归网：request_id 不存在仍返回 False（原语义不变）。"""
    p = SafetyPipeline()
    assert p.submit_approval("nope", True, "conn_a") is False


def test_deny_from_origin_is_recorded():
    p = SafetyPipeline()
    _register(p, "r1", "conn_a")
    assert p.submit_approval("r1", False, "conn_a") is True
    assert p._hitl_results["r1"] == (False, "denied")      # noqa: SLF001


def test_pending_approvals_shape_unchanged():
    """重连重播接口的对外形状不变（四元组内部化）。"""
    p = SafetyPipeline()
    _register(p, "r1", "conn_a")
    assert p.get_pending_approvals() == [("r1", "write_file", {"path": "/etc/x"})]


# ═══════════════════════════════════════════════════════════════════
# 2. 工具侧：从 ToolContext 取发起连接
# ═══════════════════════════════════════════════════════════════════

def test_origin_helper_reads_services():
    ctx = MagicMock()
    ctx.services = {"_origin_connection_id": "conn_a"}
    assert _origin_connection_id(ctx) == "conn_a"


def test_origin_helper_defaults_to_empty():
    ctx = MagicMock()
    ctx.services = {}
    assert _origin_connection_id(ctx) == ""
    assert _origin_connection_id(object()) == ""
    assert _origin_connection_id(MagicMock(services=None)) == ""


# ═══════════════════════════════════════════════════════════════════
# 3. 广播面：request_id 不外泄给无关客户端
# ═══════════════════════════════════════════════════════════════════

class _Chan:
    def __init__(self, conn_id: str | None, version: int = 2) -> None:
        self.connection_id = conn_id
        self.protocol_version = version




def test_channels_for_origin_filters_to_origin():
    from Module.tool.manager import ToolManager

    mgr = ToolManager.__new__(ToolManager)
    a = _Chan("conn_a")
    b = _Chan("conn_b")
    got = mgr._channels_for_origin(([], [a, b]), "conn_a")   # noqa: SLF001
    assert got is not None
    v1, v2 = got
    assert v1 == [] and v2 == [a]


def test_channels_for_origin_returns_none_when_offline():
    """发起连接已断开 → 退回广播（由调用方告警），不静默丢弃审批弹窗。"""
    from Module.tool.manager import ToolManager

    mgr = ToolManager.__new__(ToolManager)
    assert mgr._channels_for_origin(([], [_Chan("conn_b")]), "conn_a") is None  # noqa: SLF001
    assert mgr._channels_for_origin(([], [_Chan("conn_b")]), "") is None        # noqa: SLF001


@pytest.mark.asyncio
async def test_broadcast_only_targets_origin_channel(caplog):
    """端到端：审批帧只发给发起连接，其余客户端收不到 request_id。"""
    import logging

    from Module.tool.manager import ToolManager

    mgr = ToolManager.__new__(ToolManager)
    a, b = _Chan("conn_a"), _Chan("conn_b")

    sent: list[dict] = []

    class _Out:
        def _split_versions(self):
            return [], [a, b]

        async def command(self, name, params=None, channels=None):
            sent.append({"name": name, "params": params, "channels": channels})

    mgr._plugins = {}                                 # noqa: SLF001
    mgr._pipeline = SafetyPipeline()                  # noqa: SLF001
    with caplog.at_level(logging.WARNING, logger="Module.tool.manager"):
        mgr.set_hitl_broadcast(_Out())
        await mgr._pipeline._broadcast_hitl("r1", "write_file", {}, "conn_a")  # noqa: SLF001

    assert len(sent) == 1
    assert sent[0]["channels"] == [a], "审批帧不得发给非发起连接"
    assert not any("已不在线" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_broadcast_warns_when_origin_offline(caplog):
    import logging

    from Module.tool.manager import ToolManager

    mgr = ToolManager.__new__(ToolManager)
    sent: list[dict] = []
    b = _Chan("conn_b")

    class _Out:
        def _split_versions(self):
            return [], [b]

        async def command(self, name, params=None, channels=None):
            sent.append({"channels": channels})

    mgr._plugins = {}                                 # noqa: SLF001
    mgr._pipeline = SafetyPipeline()                  # noqa: SLF001
    with caplog.at_level(logging.WARNING, logger="Module.tool.manager"):
        mgr.set_hitl_broadcast(_Out())
        await mgr._pipeline._broadcast_hitl("r1", "write_file", {}, "conn_gone")  # noqa: SLF001

    assert any("已不在线" in r.getMessage() for r in caplog.records), caplog.text
    assert sent and sent[0]["channels"] == [b], "退回广播，审批弹窗仍要能弹出"


# ═══════════════════════════════════════════════════════════════════
# 4. WS handler：提交方身份来自连接
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ws_handler_passes_channel_connection_id():
    """handler 拿到 channel 就必须把 connection_id 传下去（原先拿到不用）。"""
    from core.input.ws_handlers.tool import _dispatch_tool_approval

    ctx = MagicMock()
    ctx.agent.tool_adapter.submit_approval = MagicMock(return_value=True)
    channel = _Chan("conn_a")
    msg = MagicMock()
    msg.request_id = "r1"
    msg.approved = True

    await _dispatch_tool_approval(ctx, {}, msg, channel)

    ctx.agent.tool_adapter.submit_approval.assert_called_once_with("r1", True, "conn_a")


@pytest.mark.asyncio
async def test_ws_handler_passes_none_without_channel():
    """无 channel（异常路径）→ 传 None，由安全管道按"有绑定则拒"处理。"""
    from core.input.ws_handlers.tool import _dispatch_tool_approval

    ctx = MagicMock()
    ctx.agent.tool_adapter.submit_approval = MagicMock(return_value=False)
    msg = MagicMock()
    msg.request_id = "r1"
    msg.approved = False

    await _dispatch_tool_approval(ctx, {}, msg, None)

    ctx.agent.tool_adapter.submit_approval.assert_called_once_with("r1", False, None)


# ═══════════════════════════════════════════════════════════════════
# 4. S3-G2：取消后不得留下**幽灵审批**
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cancelled_hitl_request_is_cleaned_up():
    """取消在途审批 → 登记必须出清，禁止幽灵弹窗跨连接重播。

    可达路径（活进程内，不需要重启）：用户新输入/断线 → `cancel_current` →
    调度器取消子任务 → 这里被取消。此后每次 WS 连接都会
    `get_pending_approvals()` 重播那个 box，而它**已经没有等待方**。
    """
    p = SafetyPipeline()
    p._hitl_timeout = 30.0                      # noqa: SLF001 —— 只要不靠超时收场

    async def _broadcast(*a, **kw):
        return None
    p.set_hitl_broadcast(_broadcast)

    plugin = MagicMock()
    task = asyncio.create_task(p._run_hitl(plugin, "write_file", {"path": "/etc/x"}))  # noqa: SLF001
    for _ in range(20):                          # 等登记出现
        if p.get_pending_approvals():
            break
        await asyncio.sleep(0.01)
    assert p.get_pending_approvals(), "前置条件：审批请求已登记"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert p.get_pending_approvals() == [], "被取消的审批仍挂在待办里（幽灵弹窗）"
    assert p._hitl_requests == {} and p._hitl_results == {}   # noqa: SLF001


@pytest.mark.asyncio
async def test_timeout_still_cleans_up():
    """回归网：超时路径原本就会出清——别在改成 finally 时弄坏它。"""
    p = SafetyPipeline()
    p._hitl_timeout = 0.01                      # noqa: SLF001

    async def _broadcast(*a, **kw):
        return None
    p.set_hitl_broadcast(_broadcast)

    approved, status = await p._run_hitl(MagicMock(), "write_file", {})  # noqa: SLF001
    assert (approved, status) == (False, "timeout")
    assert p.get_pending_approvals() == []
