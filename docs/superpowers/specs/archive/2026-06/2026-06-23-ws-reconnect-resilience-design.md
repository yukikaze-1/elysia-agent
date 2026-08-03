# WS 重连恢复 + FSM/CancelledError 防御修复 — 设计文档

**日期**: 2026-06-23
**触发**: .NET 客户端崩溃导致后端 FSM 状态异常 + 审批丢失

---

## 问题链

从 `log.txt` 分析得出的关联故障链：

1. .NET 客户端在审批弹窗时崩溃 → WS 断开
2. `write_file` 工具在后台持续运行，等待审批（无人响应）
3. 用户触发 restart → 旧 cognitive task 的 CancelledError 泄漏到新请求
4. `_handle_interrupt()` 尝试 `FSM → IDLE`，但 FSM 已被 `reset()` 置为 IDLE → `ValueError`
5. 工具在 shutdown 期间 timeout 后仍触发无意义重试

---

## 修复清单

### F1: FSM 同状态幂等

**文件**: `core/agent_fsm.py`
**问题**: `transition(AgentState.IDLE)` 当前已是 IDLE → ValueError
**修改**: `transition()` 加同状态守卫，`to_state == self._state` 时直接 return（no-op）

### F2: `_handle_interrupt` 状态守卫

**文件**: `core/cognitive_loop.py`
**问题**: 重启后旧 task CancelledError 触发 `_handle_interrupt`，FSM 已被 `reset()` 置为 IDLE
**修改**: 方法开头检查状态，已是 IDLE 则 return（防御纵深，F1 兜底）

### F3: HITL 审批重发

**文件**: `Module/tool/safety.py` + `manager.py` + `core/input/server.py`
**问题**: 审批请求通过 `_broadcast_hitl` 发给旧客户端，新客户端重连后不知道有 pending 审批
**修改**:
- `SafetyPipeline.get_pending_approvals()` — 返回 `list[(request_id, tool_name, params)]`
- `ToolManager.get_pending_approvals()` — 委托给 pipeline
- WS connect handler — 添加 channel 后检查 pending approvals，逐条重新广播

### F4: shutdown 期间不重试

**文件**: `core/task_scheduler.py`
**问题**: `_execute_tool` 在 agent.stop() 后 timeout 仍触发重试
**修改**: 重试循环开头检查 `self._running`，False 则返回 fail

### F5: `cancel_current` 清空引用

**文件**: `core/cognitive_loop.py`
**问题**: cancel 后 `self._current_task` 仍指向旧 task，restart 后泄漏
**修改**: cancel 后设 `self._current_task = None`

---

## 验证

1. 模拟场景：启动 agent → 发一条需要审批的消息 → kill 客户端 → 重连 → 确认审批弹窗重新出现
2. 模拟场景：agent 运行中 → 触发 restart → 确认无 ValueError
3. 单元测试：`agent_fsm` — `transition(IDLE)` from IDLE 应 no-op
4. 回归：`python -m pytest Module/task_system/tests/unit/ -v` 全部通过
