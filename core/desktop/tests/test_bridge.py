"""DesktopToolBridge 单元测试（批次 4 Task 4，marker=unit）。

覆盖（brief Step 1 + spec §4.2/§4.3）：
- 幂等去重：同 (tool, args_hash) 两次 request_and_wait（deliver 挂起不 resolve）
  → 第二次复用 callId、deliver 仅调 1 次；
- 并发去重：asyncio.gather 两个同参请求 → 仅 1 次投递、共享 future；
- 极速响应：deliver 回调内同步 handle_status{result} → 结果不丢（先登记后投递生效）；
- 篡改拒绝：handle_status 带错误 paramHash → return False + future resolve(TOOL_ERROR)
  + repo mark failed + 立即 resolve（不挂超时）；
- 断连：pending call 断连 → 立即 resolve UNAVAILABLE；accepted call 断连 → 不 resolve，
  重连后 handle_status{result} → resolve + repo succeeded（offline→succeeded 合法）；
- 超时：timeout=0.05 的 bridge，deliver 挂起 → wait_for 超时 → ToolResult.fail(TIMEOUT)
  + repo expired；
- future 唯一：超时后晚到 result → 不抛 InvalidStateError、repo 记录 result 不覆盖
  expired 枚举（终态守卫，mark 返回 False 但字段已写）；
- rejected/cancelled 迁移：handle_status{rejected} → ToolResult.fail(CANCELLED) + repo
  rejected；cancel_call → repo cancelled + future resolve(CANCELLED)；
- _map_terminal（批次 8c F8）：output 空时 content 回退 executionSummary（llm_summary
  同步回退）；output 非空时 content 保持 output。

Fake 约定：注入 FakeDesktopRepo（镜像 Task 3 repo 的 mark 迁移守卫语义——终态晚到
result/error 写字段但返回 False、offline→终态合法）；deliver 回调记录投递帧。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.desktop.bridge import DesktopToolBridge
from frontend_contract.ws_message import ToolStatusEnum, ToolStatusMessage
from Module.tool.protocol import ErrorType

pytestmark = pytest.mark.unit


# ============================================================================
# Fake repo — 镜像 DesktopCallRepository 的迁移守卫语义（spec §4.1）
# ============================================================================

class FakeDesktopRepo:
    """进程内 fake：upsert/get/mark/mark_offline_by_connection/query_pending。

    mark 迁移守卫对齐 spec §4.1：终态不可再迁移（晚到 status 仅记录 result/error 并返回
    False）；offline → 终态合法（重传结果闭环）；accepted↔running 双向合法。
    """

    _TERMINAL = frozenset({"succeeded", "failed", "rejected", "cancelled", "expired"})
    _ACTIVE = frozenset({"pending", "accepted", "running"})
    _ALLOWED: dict = {
        "pending": frozenset({
            "accepted", "running", "succeeded", "failed", "rejected",
            "cancelled", "expired", "offline",
        }),
        "accepted": frozenset({
            "running", "succeeded", "failed", "rejected", "cancelled", "expired", "offline",
        }),
        "running": frozenset({
            "accepted", "succeeded", "failed", "rejected", "cancelled", "expired", "offline",
        }),
        "offline": frozenset({"succeeded", "failed", "rejected", "cancelled", "expired"}),
        "succeeded": frozenset(),
        "failed": frozenset(),
        "rejected": frozenset(),
        "cancelled": frozenset(),
        "expired": frozenset(),
    }

    def __init__(self) -> None:
        self.calls: dict[str, dict] = {}

    async def connect(self) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def upsert(self, call: dict) -> None:
        self.calls[call["call_id"]] = dict(call)

    async def get(self, call_id: str) -> dict | None:
        doc = self.calls.get(call_id)
        return dict(doc) if doc is not None else None

    async def mark(self, call_id: str, **fields) -> bool:
        doc = self.calls.get(call_id)
        if doc is None:
            return False
        current = doc.get("status", "")
        new_status = fields.get("status")
        update = dict(fields)
        if new_status is not None and new_status != current:
            allowed = self._ALLOWED.get(current, frozenset())
            if new_status not in allowed:
                if current in self._TERMINAL:
                    # 终态晚到 status：仅记录 result/error（真实结果），返回 False。
                    late = {k: v for k, v in update.items() if k in ("result", "error")}
                    if not late:
                        return False
                    doc.update(late)
                    return False
                return False
        doc.update(update)
        return True

    async def mark_offline_by_connection(self, connection_id: str) -> int:
        n = 0
        for doc in self.calls.values():
            if (doc.get("target_connection") == connection_id
                    and doc.get("status") in self._ACTIVE):
                doc["status"] = "offline"
                n += 1
        return n

    async def query_pending(self, cutoff: str) -> list[dict]:
        return [
            dict(d) for d in self.calls.values()
            if d.get("status") in self._ACTIVE and (d.get("expires_at") or "") > cutoff
        ]

    async def mark_stale_expired(self, cutoff: str) -> int:
        """启动清理：pending/accepted/running 中 updated_at < cutoff 标 expired。"""
        n = 0
        for doc in self.calls.values():
            if (doc.get("status") in self._ACTIVE
                    and (doc.get("updated_at") or "") < cutoff):
                doc["status"] = "expired"
                n += 1
        return n


# ============================================================================
# 构造辅助
# ============================================================================

def _call(**overrides) -> dict:
    """构造 request_and_wait 的 call dict（对齐 core/tools/desktop/plugin.py _delegate）。"""
    call = {
        "tool_name": "desktop.file.read",
        "tool_version": "1.0.0",
        "runtime": "desktop",
        "arguments": {"path": "/tmp/a.txt"},
        "arguments_hash": "sha256-abc",
        "reason": "测试",
        "risk_level": "medium",
        "permission_required": True,
        "approval_authority": "client",
        # B-6-05：插件 _delegate 恒带两字段（source_call_id=ctx.call_id、request_id=trace_id or None）
        "source_call_id": "tc_default",
        "request_id": None,
    }
    call.update(overrides)
    return call


def _status(call_id: str, status: str, *, tool_name: str = "desktop.file.read",
            tool_version: str = "1.0.0", runtime: str = "desktop",
            param_hash: str = "sha256-abc", **extra) -> dict:
    """构造 tool.status wire 帧（camelCase，对齐 ToolStatusMessage）。"""
    frame = {
        "type": "tool.status",
        "callId": call_id,
        "status": status,
        "toolName": tool_name,
        "toolVersion": tool_version,
        "runtime": runtime,
        "paramHash": param_hash,
    }
    frame.update(extra)
    return frame


async def _until(predicate, timeout: float = 2.0) -> None:
    """轮询等待条件成立（测试协调用）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("wait condition not met")
        await asyncio.sleep(0.005)


def _make_bridge(timeout: float = 120.0) -> tuple[FakeDesktopRepo, DesktopToolBridge, list]:
    """构造 fake repo + bridge + 投递帧记录列表。

    deliver 记录投递帧后立即返回（不 resolve）——call 停留 pending，由测试手动
    handle_status 收尾或依赖超时。
    """
    repo = FakeDesktopRepo()
    bridge = DesktopToolBridge(repo, timeout=timeout)
    frames: list[dict] = []

    async def deliver(_target_connection: str | None, frame: dict) -> None:
        frames.append(frame)

    bridge.bind_deliver(deliver)
    return repo, bridge, frames


# ============================================================================
# 幂等去重
# ============================================================================

class TestDedup:
    @pytest.mark.asyncio
    async def test_idempotent_dedup_reuses_call_single_deliver(self):
        """同 (tool, hash) 两次 request_and_wait → 复用 callId、deliver 仅调 1 次。"""
        repo, bridge, frames = _make_bridge()
        call = _call()
        t1 = asyncio.create_task(bridge.request_and_wait(call=call))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]

        t2 = asyncio.create_task(bridge.request_and_wait(call=call))
        await asyncio.sleep(0.02)  # 让第二次进入 dedup 路径
        assert len(frames) == 1                    # 不二次投递
        # 确认第二个请求复用同一 callId：给首个 future 解结果后两次都应成功
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1.success and r2.success
        assert r1.content == "ok" and r2.content == "ok"
        assert repo.calls[call_id]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_concurrent_dedup_single_deliver_shared_future(self):
        """asyncio.gather 两个同参请求 → 仅 1 次投递、共享 future。"""
        repo, bridge, frames = _make_bridge()
        call = _call()
        tasks = asyncio.gather(
            bridge.request_and_wait(call=call),
            bridge.request_and_wait(call=call),
        )
        await _until(lambda: len(frames) == 1)
        await asyncio.sleep(0.02)  # 给两个请求都走到 dedup 检查的时间
        assert len(frames) == 1                    # 并发也只投递一次
        call_id = frames[0]["callId"]
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        r1, r2 = await asyncio.wait_for(tasks, 2.0)
        assert r1.success and r2.success
        assert r1.content == "ok" and r2.content == "ok"
        # 同一 callId 只建了一次
        assert list(repo.calls) == [call_id]

    @pytest.mark.asyncio
    async def test_different_params_are_distinct_calls(self):
        """不同 (tool, hash) → 各自独立 callId、各投递一次。"""
        repo, bridge, frames = _make_bridge()
        t1 = asyncio.create_task(bridge.request_and_wait(call=_call(arguments_hash="hash-1")))
        await _until(lambda: len(frames) == 1)
        t2 = asyncio.create_task(bridge.request_and_wait(call=_call(arguments_hash="hash-2")))
        await _until(lambda: len(frames) == 2)
        assert frames[0]["callId"] != frames[1]["callId"]
        assert len(repo.calls) == 2
        # 各自 resolve（status 必须回传与登记一致的 arguments_hash）
        await bridge.handle_status(_status(frames[0]["callId"], "result", param_hash="hash-1", output="a"))
        await bridge.handle_status(_status(frames[1]["callId"], "result", param_hash="hash-2", output="b"))
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1.content == "a" and r2.content == "b"


# ============================================================================
# 极速响应（先登记后投递顺序）
# ============================================================================

class TestFastResponse:
    @pytest.mark.asyncio
    async def test_fast_result_in_deliver_not_lost(self):
        """deliver 回调内同步 handle_status{result} → 结果不丢（先登记后投递生效）。"""
        repo = FakeDesktopRepo()
        bridge = DesktopToolBridge(repo, timeout=1.0)
        resolved: list = []

        async def deliver(_target_connection, frame):
            # 投递帧里立即回 result——若先投递后登记，handle_status 找不到 future → 超时
            resolved.append(await bridge.handle_status(_status(frame["callId"], "result", output="fast")))

        bridge.bind_deliver(deliver)
        result = await bridge.request_and_wait(call=_call())
        assert result.success
        assert result.content == "fast"
        assert resolved == [True]

    @pytest.mark.asyncio
    async def test_fast_error_in_deliver_not_lost(self):
        """deliver 内立即回 error → 立即 TOOL_ERROR 而非挂超时。"""
        repo = FakeDesktopRepo()
        bridge = DesktopToolBridge(repo, timeout=1.0)

        async def deliver(_target_connection, frame):
            await bridge.handle_status(_status(
                frame["callId"], "error", error={"code": "E", "message": "boom"}))

        bridge.bind_deliver(deliver)
        result = await bridge.request_and_wait(call=_call())
        assert not result.success
        assert result.error_type == ErrorType.TOOL_ERROR
        assert result.error == "boom"


# ============================================================================
# 精确目标投递 / 快速失败
# ============================================================================

class TestTargetedDelivery:
    @pytest.mark.asyncio
    async def test_delivery_receives_snapshotted_target_connection(self):
        """投递回调拿到 call 登记的目标连接，失败立即 UNAVAILABLE 而非等待超时。"""
        repo = FakeDesktopRepo()
        bridge = DesktopToolBridge(repo, timeout=120.0)
        await bridge.on_client_connect("conn_latest")
        delivered: list[tuple[str | None, dict]] = []

        async def deliver(target_connection: str | None, frame: dict) -> None:
            delivered.append((target_connection, frame))
            raise RuntimeError("目标连接已消失")

        bridge.bind_deliver(deliver)
        result = await bridge.request_and_wait(call=_call())

        assert not result.success
        assert result.error_type == ErrorType.UNAVAILABLE
        assert delivered[0][0] == "conn_latest"
        call_id = delivered[0][1]["callId"]
        assert repo.calls[call_id]["target_connection"] == "conn_latest"
        assert repo.calls[call_id]["status"] == "failed"
        assert repo.calls[call_id]["error"]["code"] == "DESKTOP_UNAVAILABLE"


# ============================================================================
# 篡改拒绝（BE-043）
# ============================================================================

class TestTamperRejection:
    @pytest.mark.asyncio
    async def test_paramhash_mismatch_rejected_immediately(self):
        """错误 paramHash → return False + 立即 resolve TOOL_ERROR + repo failed。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]

        ok = await bridge.handle_status(_status(call_id, "result", param_hash="TAMPERED", output="x"))
        assert ok is False
        result = await asyncio.wait_for(task, 2.0)
        assert not result.success
        assert result.error_type == ErrorType.TOOL_ERROR
        assert result.error == "参数与请求不一致，已拒绝"
        doc = repo.calls[call_id]
        assert doc["status"] == "failed"
        assert doc["error"]["message"] == "参数与请求不一致，已拒绝"

    @pytest.mark.asyncio
    async def test_toolname_mismatch_rejected(self):
        """错误 toolName → 同样拒绝（篡改校验覆盖 tool/version/runtime/hash）。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        ok = await bridge.handle_status(_status(call_id, "result", tool_name="evil.tool", output="x"))
        assert ok is False
        result = await asyncio.wait_for(task, 2.0)
        assert not result.success
        assert result.error_type == ErrorType.TOOL_ERROR

    @pytest.mark.asyncio
    async def test_unknown_call_returns_false(self):
        """未知 callId → return False（不 resolve、不写）。"""
        repo, bridge, frames = _make_bridge()
        ok = await bridge.handle_status(_status("call_unknown", "result"))
        assert ok is False
        assert repo.calls == {}


# ============================================================================
# 断连规则（D-o）
# ============================================================================

class TestDisconnect:
    @pytest.mark.asyncio
    async def test_pending_disconnect_resolves_unavailable(self):
        """pending（未接手）call 断连 → 立即 resolve UNAVAILABLE + repo offline。"""
        repo, bridge, frames = _make_bridge()
        await bridge.on_client_connect("conn_1")
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]

        await bridge.on_client_disconnect("conn_1")
        result = await asyncio.wait_for(task, 2.0)
        assert not result.success
        assert result.error_type == ErrorType.UNAVAILABLE
        assert result.error == "桌面客户端已断开"
        assert bridge.is_available() is False
        assert repo.calls[call_id]["status"] == "offline"

    @pytest.mark.asyncio
    async def test_accepted_disconnect_hangs_then_reconnect_result(self):
        """accepted call 断连 → 不 resolve；重连后重传 result → offline→succeeded。"""
        repo, bridge, frames = _make_bridge()
        await bridge.on_client_connect("conn_1")
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]

        # Electron 已接手
        assert await bridge.handle_status(_status(call_id, "accepted")) is True
        # 断连：已接手 call 不 resolve（挂起），repo 标 offline
        await bridge.on_client_disconnect("conn_1")
        await asyncio.sleep(0.02)
        assert not task.done()
        assert repo.calls[call_id]["status"] == "offline"
        assert bridge.is_available() is False

        # 重连（不自动重投，仅新 connection_id）
        await bridge.on_client_connect("conn_2")
        # 晚到结果（BE-043「重传结果」闭环）：offline → succeeded
        ok = await bridge.handle_status(_status(call_id, "result", output="resumed"))
        assert ok is True
        result = await asyncio.wait_for(task, 2.0)
        assert result.success
        assert result.content == "resumed"
        assert repo.calls[call_id]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_disconnect_of_unrelated_connection_ignored(self):
        """其他连接断开不 resolve 本桥 pending call。"""
        repo, bridge, frames = _make_bridge()
        await bridge.on_client_connect("conn_1")
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        await bridge.on_client_disconnect("conn_other")
        await asyncio.sleep(0.02)
        assert not task.done()
        assert bridge.is_available() is True        # 本连接不受影响
        # 收尾
        call_id = frames[0]["callId"]
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        assert (await asyncio.wait_for(task, 2.0)).success


# ============================================================================
# 超时 / future 唯一
# ============================================================================

class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_marks_expired(self):
        """deliver 挂起 → wait_for 超时 → TIMEOUT + repo expired。

        附带钉住**来源标注**：桥合成的超时会以"插件返回的 ToolResult"形态到达调度器，
        不标 `synthesised_timeout` 就等于绕过 `idempotent` 门控——`desktop.file.write` /
        `file.select` / `screen.capture` 会被重复执行。今天靠"桥超时 == 插件预算、管道
        计时器先触发"侥幸不成立，但那是竞态，不是机制。
        """
        repo, bridge, frames = _make_bridge(timeout=0.05)
        result = await bridge.request_and_wait(call=_call())
        assert not result.success
        assert result.error_type == ErrorType.TIMEOUT
        assert result.error == "桌面工具调用超时"
        assert result.synthesised_timeout is True, \
            "桥合成的超时必须标注来源，否则会绕过调度器的 idempotent 门控"
        call_id = frames[0]["callId"]
        assert repo.calls[call_id]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_late_result_after_timeout_no_crash(self):
        """超时后晚到 result → 不抛 InvalidStateError、repo 记录 result 不覆盖 expired。"""
        repo, bridge, frames = _make_bridge(timeout=0.05)
        result = await bridge.request_and_wait(call=_call())
        assert not result.success            # TIMEOUT
        call_id = frames[0]["callId"]

        # 晚到 result：终态守卫——不 resolve（future 已清理），repo 仅记录 result/error
        ok = await bridge.handle_status(_status(call_id, "result", output="late"))
        assert ok is True
        doc = repo.calls[call_id]
        assert doc["status"] == "expired"    # 终态枚举不被覆盖
        assert doc["result"]["output"] == "late"   # 真实结果被记录

    @pytest.mark.asyncio
    async def test_late_error_after_cancel_no_crash(self):
        """cancel 后晚到 error → 不抛、repo 仅记录 error 字段。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        await bridge.cancel_call(call_id)
        assert not (await asyncio.wait_for(task, 2.0)).success
        # 晚到 error：future 已 resolve，repo cancelled 终态不被覆盖，error 字段被记录
        ok = await bridge.handle_status(_status(
            call_id, "error", error={"code": "E", "message": "late"}))
        assert ok is True
        doc = repo.calls[call_id]
        assert doc["status"] == "cancelled"
        assert doc["error"]["message"] == "late"


# ============================================================================
# 终态迁移（rejected/error/cancelled + accepted 不 resolve）
# ============================================================================

class TestTerminalMigrations:
    @pytest.mark.asyncio
    async def test_rejected_migration(self):
        """handle_status{rejected} → ToolResult.fail(CANCELLED) + repo 标记为 rejected。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        ok = await bridge.handle_status(_status(call_id, "rejected"))
        assert ok is True
        result = await asyncio.wait_for(task, 2.0)
        assert not result.success
        assert result.error_type == ErrorType.CANCELLED
        assert result.error == "用户拒绝了该工具调用"
        assert repo.calls[call_id]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_error_migration(self):
        """handle_status{error} → TOOL_ERROR + repo failed + error 字段。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        ok = await bridge.handle_status(_status(
            call_id, "error", error={"code": "FILE_READ_FAILED", "message": "permission denied"}))
        assert ok is True
        result = await asyncio.wait_for(task, 2.0)
        assert not result.success
        assert result.error_type == ErrorType.TOOL_ERROR
        assert result.error == "permission denied"
        assert repo.calls[call_id]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_cancel_call_migration(self):
        """cancel_call → repo 标记为 cancelled + future resolve(CANCELLED)。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        await bridge.cancel_call(call_id)
        result = await asyncio.wait_for(task, 2.0)
        assert not result.success
        assert result.error_type == ErrorType.CANCELLED
        assert result.error == "客户端取消了该调用"
        assert repo.calls[call_id]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_accepted_running_do_not_resolve(self):
        """accepted → 仅更新 repo，不 resolve future。"""
        repo, bridge, frames = _make_bridge()
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        assert await bridge.handle_status(_status(call_id, "accepted")) is True
        await asyncio.sleep(0.02)
        assert not task.done()
        assert repo.calls[call_id]["status"] == "accepted"
        # 收尾
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        assert (await asyncio.wait_for(task, 2.0)).success


# ============================================================================
# 元数据 / 连接状态 / 查询
# ============================================================================

class TestLifecycleAndQuery:
    @pytest.mark.asyncio
    async def test_connect_and_is_available(self):
        repo, bridge, frames = _make_bridge()
        assert await bridge.connect_db() is True
        assert bridge.is_available() is False
        await bridge.on_client_connect("conn_1")
        assert bridge.is_available() is True
        assert await bridge.health_check() is True
        await bridge.on_client_disconnect("conn_1")
        assert bridge.is_available() is False

    @pytest.mark.asyncio
    async def test_connect_db_cleans_stale_active_calls(self):
        """connect_db 成功后启动清理：遗留 running/accepted 超时调用标 expired。

        模拟「后端重启前遗留」的调用（reload 入缓存后仍 active），启动时经
        mark_stale_expired（cutoff = now - timeout）批量标 expired（spec §4.1 启动
        清理超时孤儿，对齐 generation mark_stale_interrupted 先例）。
        """
        repo = FakeDesktopRepo()
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(seconds=300)).isoformat()
        fresh = now.isoformat()
        repo.calls["call_stale_running"] = {
            "call_id": "call_stale_running", "tool_name": "desktop.file.read",
            "status": "running", "updated_at": stale, "expires_at": stale,
        }
        repo.calls["call_stale_accepted"] = {
            "call_id": "call_stale_accepted", "tool_name": "desktop.file.read",
            "status": "accepted", "updated_at": stale, "expires_at": stale,
        }
        repo.calls["call_fresh"] = {
            "call_id": "call_fresh", "tool_name": "desktop.file.read",
            "status": "running", "updated_at": fresh, "expires_at": fresh,
        }
        bridge = DesktopToolBridge(repo, timeout=120.0)
        ok = await bridge.connect_db()
        assert ok is True
        assert repo.calls["call_stale_running"]["status"] == "expired"
        assert repo.calls["call_stale_accepted"]["status"] == "expired"
        assert repo.calls["call_fresh"]["status"] == "running"  # 未超时不动

    @pytest.mark.asyncio
    async def test_target_connection_filled_in_doc(self):
        """request_and_wait 组装 call doc 必须填 target_connection（Task 8 附件校验依赖）。"""
        repo, bridge, frames = _make_bridge()
        await bridge.on_client_connect("conn_9")
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        assert repo.calls[call_id]["target_connection"] == "conn_9"
        # 投递帧含关键字段（ToolRequestMessage wire）
        frame = frames[0]
        assert frame["callId"] == call_id
        assert frame["toolName"] == "desktop.file.read"
        assert frame["paramHash"] == "sha256-abc"
        assert frame["expiresAt"] > "2020"
        # 收尾
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        await asyncio.wait_for(task, 2.0)

    @pytest.mark.asyncio
    async def test_build_request_frame_carries_source_call_id(self):
        """_build_request_frame wire 含 sourceCallId/requestId，与 repo doc 一致（D6-07）。"""
        repo, bridge, frames = _make_bridge()
        await bridge.on_client_connect("conn_9")
        task = asyncio.create_task(bridge.request_and_wait(
            call=_call(source_call_id="tc_abc123", request_id="trace_xyz")))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        frame = frames[0]
        doc = repo.calls[call_id]
        # 必填 sourceCallId 贯通：非 None、与登记 source_call_id 一致
        assert doc["source_call_id"] == "tc_abc123"
        assert doc["request_id"] == "trace_xyz"
        assert frame["sourceCallId"] == "tc_abc123"
        assert frame["requestId"] == "trace_xyz"
        # 收尾
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        await asyncio.wait_for(task, 2.0)

    @pytest.mark.asyncio
    async def test_get_call_and_pending_calls(self):
        repo, bridge, frames = _make_bridge()
        await bridge.on_client_connect("conn_1")
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        doc = await bridge.get_call(call_id)
        assert doc is not None and doc["status"] == "pending"
        pending = await bridge.pending_calls()
        assert any(c["call_id"] == call_id for c in pending)
        await bridge.handle_status(_status(call_id, "result", output="ok"))
        await asyncio.wait_for(task, 2.0)
        assert (await bridge.get_call(call_id))["status"] == "succeeded"
        assert not any(c["call_id"] == call_id for c in await bridge.pending_calls())

    @pytest.mark.asyncio
    async def test_save_all_noop(self):
        repo, bridge, frames = _make_bridge()
        await bridge.save_all()


# ============================================================================
# _map_terminal output 空回退 executionSummary（批次 8c F8）
# ============================================================================

def test_map_terminal_empty_output_falls_back_to_summary():
    """output 空 → content 回退 executionSummary（llm_summary 同步回退）。"""
    msg = ToolStatusMessage(toolName="desktop.screen.capture", status=ToolStatusEnum.RESULT,
                            callId="call_1", toolVersion="1", runtime="desktop", paramHash="h",
                            output="", executionSummary="截图已保存")
    _s, result_field, _e, tool_result = DesktopToolBridge._map_terminal(msg)
    assert tool_result.content == "截图已保存"
    assert tool_result.llm_summary == "截图已保存"


def test_map_terminal_nonempty_output_keeps_output():
    """output 非空 → content 保持 output（不回退 executionSummary）。"""
    msg = ToolStatusMessage(toolName="t", status=ToolStatusEnum.RESULT,
                            callId="call_1", toolVersion="1", runtime="desktop", paramHash="h",
                            output="o", executionSummary="s")
    _s, _rf, _e, tool_result = DesktopToolBridge._map_terminal(msg)
    assert tool_result.content == "o"


# ============================================================================
# 取消路径的登记清理（#33c / S2-R2-2）
# ============================================================================

class TestCancellationCleanup:
    """取消（含调度器外层超时 cancel）也必须释放 `_dedup`/`_pending`。

    修前：登记只在成功/超时/投递失败三条路径上清理，且**没有 `finally`**——取消会让
    `CancelledError` 从 `await self._await_with_timeout(future)` 那一帧抛出、跳过全部
    清理；而那个 future 被 `asyncio.shield` 保护（既不取消、也不 resolve）。
    后果不止"泄漏一个对象"：同参重试会**命中 `_dedup` 并复用那个永不落定的
    future**，于是不再向 Electron 投递任何 `tool.request`，直到进程重启。
    """

    @pytest.mark.asyncio
    async def test_cancel_releases_registrations_and_marks_repo(self):
        repo, bridge, frames = _make_bridge(timeout=120.0)
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bridge._pending == {}, "取消后不得残留 pending future"  # noqa: SLF001
        assert bridge._dedup == {}, "取消后不得残留 dedup 键"          # noqa: SLF001
        # repo 的 cancelled 标记是**尽力而为**（shield 出去、不等结果）
        await _until(lambda: repo.calls[call_id]["status"] == "cancelled")

    @pytest.mark.asyncio
    async def test_cancel_does_not_terminalise_accepted_call(self):
        """取消**已接手**的调用不得把它标成 `cancelled`（D-o / BE-043）。

        已接手（accepted/running/offline）的调用可能已在 Electron 侧产生副作用，必须
        留出"重连重传 result → offline → succeeded"的闭环。把它标 `cancelled` 会撞上
        终态守卫，让合法的晚到结果**永久**被拒——`core/input/tests/
        test_desktop_roundtrip.py::test_accepted_disconnect_reconnect_result_recovery`
        就是这么抓到本机制第一版的（当时标记无条件执行）。
        """
        repo, bridge, frames = _make_bridge(timeout=120.0)
        task = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        call_id = frames[0]["callId"]
        assert await bridge.handle_status(_status(call_id, "accepted")) is True

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)          # 让 shield 出去的标记协程跑完
        assert repo.calls[call_id]["status"] == "accepted", \
            "已接手的调用不得被取消标记终结"

        # 晚到结果仍须能闭环 offline/accepted → succeeded
        await bridge.handle_status(
            _status(call_id, "result", output="晚到结果", executionSummary="s"))
        assert repo.calls[call_id]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_retry_after_cancel_redelivers(self):
        """核心后果：取消后同参重试必须**重新投递**，而不是复用陈旧 future。"""
        _repo, bridge, frames = _make_bridge(timeout=120.0)
        call = _call()

        first = asyncio.create_task(bridge.request_and_wait(call=call))
        await _until(lambda: len(frames) == 1)
        first_call_id = frames[0]["callId"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        retry = asyncio.create_task(bridge.request_and_wait(call=call))
        await _until(lambda: len(frames) == 2)   # 修前：停在 1，永久等待
        assert frames[1]["callId"] != first_call_id, "重试必须是新 call（重新投递）"
        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry


# ============================================================================
# #33c 另一半：`_locks` 有界化（条纹锁）
# ============================================================================

class TestStripedLocks:
    """`_locks` 此前按 key 无界增长（只在 `setdefault` 处增加、从不清理）。

    条纹化后：同 key 恒同锁（互斥语义不变）、空间 O(桶数)。
    """

    @pytest.mark.asyncio
    async def test_same_key_always_maps_to_the_same_lock(self):
        """互斥语义的前提：同一个 key 必须恒定拿到同一把锁。"""
        _repo, bridge, _frames = _make_bridge()
        a = bridge._lock_for(("desktop.file.read", "sha256-abc"))  # noqa: SLF001
        b = bridge._lock_for(("desktop.file.read", "sha256-abc"))  # noqa: SLF001
        assert a is b

    @pytest.mark.asyncio
    async def test_lock_space_is_bounded(self):
        """核心：大量不同 key 不得让锁表增长（修前是随 key 无界增长的 dict）。"""
        from core.desktop.bridge import _LOCKS_STRIPES
        _repo, bridge, _frames = _make_bridge()
        for i in range(_LOCKS_STRIPES * 20):
            bridge._lock_for(("tool", f"hash-{i}"))          # noqa: SLF001
        created = [lock for lock in bridge._locks if lock is not None]  # noqa: SLF001
        assert len(created) <= _LOCKS_STRIPES, \
            f"锁表必须 ≤ {_LOCKS_STRIPES} 把，实际 {len(created)}"

    @pytest.mark.asyncio
    async def test_dedup_mutual_exclusion_still_holds(self):
        """行为级对照：条纹化后同参并发仍只投递一次（既有语义的回归网）。"""
        repo, bridge, frames = _make_bridge(timeout=120.0)
        t1 = asyncio.create_task(bridge.request_and_wait(call=_call()))
        t2 = asyncio.create_task(bridge.request_and_wait(call=_call()))
        await _until(lambda: len(frames) == 1)
        await asyncio.sleep(0.05)
        assert len(frames) == 1, "同参并发仍必须只投递一次"
        for t in (t1, t2):
            t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


class TestReuserDoesNotOwnTheFirstPartyRegistration:
    """S2-G2 残余：**复用者**（命中去重的那次调用）不得接管**首方**的登记。

    机制：复用分支原先 `owner_id = existing_id`（首方的 callId），于是复用者被取消/超时时
    会走 `#33c` 的清理与 `_mark_cancelled_best_effort(existing_id)`，把**首方**的
    `_pending`/`_dedup` 释放掉、并把首方的调用标成终态。后果两条：
      ① 首方的 future 从此无人 resolve（客户端回帧时 `_pending` 里找不到登记）
         ⇒ 首方**挂到桥超时**；
      ② dedup 键被清掉 ⇒ 同参重试**重新投递**（非幂等桌面动作可能执行两遍）。
    正确的所有权：登记属于**登记它的那次调用**，复用者只是等同一个 future。

    **只钉取消这一路，不单独钉"复用者超时"**：两者的终态会收敛（首方自己的超时路径
    同样把它标 EXPIRED），构造不出能区分新旧行为的判据——**宁可少一条用例，
    也不留一条在两种实现下都绿的**（那正是本仓反复出现的假绿）。
    """

    @pytest.mark.asyncio
    async def test_reuser_cancel_leaves_first_party_intact(self):
        repo, bridge, frames = _make_bridge(timeout=120.0)
        call = _call()

        first = asyncio.create_task(bridge.request_and_wait(call=call))
        await _until(lambda: len(frames) == 1)
        first_id = frames[0]["callId"]

        second = asyncio.create_task(bridge.request_and_wait(call=call))
        await asyncio.sleep(0.02)                       # 让第二次进入复用分支
        assert len(frames) == 1, "复用分支不该二次投递"

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

        assert first_id in bridge._pending, "复用者取消了，却把**首方**的 pending 登记释放了"  # noqa: SLF001
        assert bridge._dedup.get(("desktop.file.read", "sha256-abc")) == first_id, (  # noqa: SLF001
            "复用者把首方的 dedup 键清掉了 → 同参重试会重新投递")
        assert repo.calls[first_id]["status"] == "pending", (
            f"首方的调用被复用者改了状态：{repo.calls[first_id]['status']}")

        # 首方仍能被正常 resolve（不挂到超时）——这是"future 无人 resolve"的直接反证
        await bridge.handle_status(_status(first_id, "result", output="ok"))
        result = await asyncio.wait_for(first, timeout=2.0)
        assert result.success and result.content == "ok"


# ============================================================================
# S2-H6：投递失败必须 fail-fast（不得白等满超时）
# ============================================================================

class TestDeliveryFailureIsFast:
    """定向投递失败 → 立刻 UNAVAILABLE/FAILED，而不是等满 timeout 报 TIMEOUT/EXPIRED。

    缺陷形状（S2-H6）：`_deliver_tool_request` 的文档契约要求"投递失败要抛，让 bridge
    立即收敛为 DESKTOP_UNAVAILABLE"，但它唯一能用的发送调用 `WebSocketChannel.send_json`
    按**广播**语义吞掉异常只打 warning ⇒ 契约无法被满足，bridge 的超时分支先触发。
    实测对照（报告）：目标行为 0.00s success=False UNAVAILABLE；生产路径 0.60s（真实
    120s）TIMEOUT + repo 记 EXPIRED。

    这里用**真实**的 `_deliver_tool_request` + 真实 `WebSocketChannel`（底层 ws 抛错），
    把两条路径接起来测——只测 bridge 或只测 send_json 都看不出这个缺口。
    """

    @pytest.mark.asyncio
    async def test_send_failure_yields_unavailable_not_timeout(self):
        from unittest.mock import AsyncMock, MagicMock

        from Module.output.websocket import WebSocketChannel
        from core.agent._lifecycle_tools import _LifecycleTools

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=ConnectionResetError("link down"))
        channel = WebSocketChannel(ws, protocol_version=2, client_platform="electron")
        channel.connection_id = "conn_target"
        agent = MagicMock()
        agent._output.channels = [channel]

        repo = FakeDesktopRepo()
        bridge = DesktopToolBridge(repo, timeout=5.0)
        bridge.bind_deliver(
            lambda target, frame: _LifecycleTools._deliver_tool_request(agent, target, frame))
        await bridge.on_client_connect("conn_target")

        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await bridge.request_and_wait(call=_call())
        elapsed = loop.time() - started

        assert not result.success
        assert result.error_type is ErrorType.UNAVAILABLE, (
            f"投递失败应报 UNAVAILABLE，实际 {result.error_type}"
        )
        assert elapsed < 1.0, (
            f"应立刻收敛（<1s），实际 {elapsed:.2f}s——说明退回了'等满超时'"
        )
        assert repo.calls[list(repo.calls)[0]]["status"] == "failed", (
            "终态应为 failed（投递失败），不是 expired（超时）"
        )
