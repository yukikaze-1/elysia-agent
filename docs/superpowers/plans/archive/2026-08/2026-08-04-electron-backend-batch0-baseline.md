# Electron 后端改造 — 批次 0：基线确认（Plan）

**Plan 版本：** v0.1
**依据 Spec：** `docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch0-baseline.md`
**执行环境：** Elysia conda（`source $(conda info --base)/etc/profile.d/conda.sh && conda activate Elysia`）

---

## 概览

批次 0 是验证 + 文档批次。产出：`docs/electron-backend-baseline.md` + 三份脱敏 WS JSONL 轨迹。**不修改生产代码。**

## 任务

### Task 1: 契约与测试基线确认

- [ ] 运行 `python -m frontend_contract --check`（Elysia conda）——记录"无漂移"输出（F2）
- [ ] 运行 `make check-types`（F3，与 --check 同命令，显式挂 F3）
- [ ] 运行 `make test-quick` —— 记录基线（当前实测 1883 passed / 2 skipped / 58 deselected）（F4）
- [ ] 记录 HEAD / 工作树状态 / **branch（=main）**（F1，已验证：HEAD=1e62b58，clean，branch=main）

**证据记录到** `docs/electron-backend-baseline.md` §契约与测试基线。

### Task 2: 时间戳事实确认

- [ ] 已核：核心数据走 `now_iso()`（`core/schema/utils.py:28-34`，UTC）；naive `datetime.now()` 共 **4 处**，均在展示/通知/工具路径（F5）：`idle_monitor.py:120`、**`loop.py:1044`（_build_wake_up_message 文案）**、`system_prompt/sections/sensory.py:51`、`Module/tool/__main__.py:32`
- [ ] 在基线文档 §时间戳记录结论：核心数据已 UTC，v2 沿用；展示路径不落库，不在批次 0 修改

### Task 3: 事件面 / 消息落库 / 会话结构确认

- [ ] 已核：ServerMessage 判别联合 18 成员 + Client→Server 6 类（`frontend_contract/ws_message.py:331-350`）；无版本/ID/ACK 字段（F9）
- [ ] 已核：Message 字段结构（`core/schema/message.py`：message_id/role/content/timestamp/metadata，timestamp 走 now_iso）+ `messages_for_client()` 回放结构（`session.py:149`）（F10）
- [ ] 已核：SessionMemory 单实例（session_id="default"）；MessageRepository 按 session_id 索引 + query_by_session 已存在（F10/F11）
- [ ] 已核：12 插件类（`core/tools/__init__.py`）；认证范围（`server.py` `_check_auth` 仅护 admin/debug）；`/uploads/*` 匿名静态；STT 自动提交（`ws_handler.py:352-356`）；审批截断 `str(params)[:200]` **两站**：`ws_handler.py:127`（pending 重播）+ `Module/tool/manager.py:326`（正常广播）（F13）
- [ ] 在基线文档 §事件面 / §消息与会话 / §语音与工具记录

### Task 4: Backend/Desktop 映射核对

- [ ] 已核：`docs/14` §3.1/§5 与 `docs/15` §三 对沙盒 read_file/write_file 标注 Backend，无"误标 Desktop"问题
- [ ] 基线文档 §工具与 Runtime 记录此结论

### Task 5: 真实 WS 抓包（F15）

- [ ] 新建 `scripts/capture_ws_trace.py`：
  - 构造 Agent（镜像 `core/tests/fake_llm.py` `FakeLLMBackend`，响应串含 `<inner_voice>…</inner_voice>` + `<mood>…|强度</mood>` + 至少三句标点）
  - **TTS 必须禁用**（`config.tts.enabled=false`），否则句子边界事件不发且真调 TTS
  - **FakeLLMWithUsage 必须实现 `stream_think`**（否则 usage 永不触发）：LLMAdapter 对无 stream_think 的 backend 走 `chat()` + 逐字符分支，不发 usage chunk（llm_adapter.py:129-138）；stream_think 需 yield 内容 token 后收尾 `{"content": None, "tool_calls": None, "usage": {...}}`（对应 loop.py:502 `"usage" in chunk`）
  - 脚本需 `await agent.start()`/`stop()` 生命周期（test_server.py 模式）；`frontend-handoff-v2/traces/` 目录 `os.makedirs(exist_ok=True)`
  - `create_app(agent, serve_static=False)` + aiohttp WS 客户端连接 `/ws`
  - 发 `{"type":"text","content":"…"}`，逐帧记录到 `state:"idle"`
  - 去敏：删除环境变量/路径/真实文本 → 占位符
- [ ] 取消轨迹：发 text 后紧跟 `{"type":"interrupt"}`（拉长 fake 响应保证可打断），记录打断事件序列
- [ ] 工具审批轨迹：子类 FakeLLM 加 `stream_think`（参照 `test_phase3.py:1243-1311`），yield 指向 `save_memory`/`write_file` 的 `tool_calls` delta → 收到 `command tool_approval` → 脚本回 `{"type":"tool_approval","request_id":...,"approved":true}` → 记录 tool_done。若此路径受阻，改为合成轨迹并在文档明确标注"模拟"，**不伪造为真实抓包**
- [ ] 输出到 `frontend-handoff-v2/traces/ws-chat.jsonl`、`ws-cancel.jsonl`、`ws-tool-approval.jsonl`
- [ ] 验证：每行 `json.loads` 通过；事件序列符合预期

### Task 6: 写基线文档

- [ ] `docs/electron-backend-baseline.md` 按 spec §5 结构完成
- [ ] 记录三份轨迹位置与摘要

### Task 7: 验收（subagent）

- [ ] 派验收 subagent：核对 F1-F15 每项证据真实、轨迹可解析、无敏感数据、未改生产代码
- [ ] 显式验收：基线文档完成且**无悬案**（spec §6 第 3 条）；`git status` 仅新增交付物（spec §6 第 4 条）

### Task 8: 提交

- [ ] `git add docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch0-baseline.md docs/superpowers/plans/archive/2026-08/2026-08-04-electron-backend-batch0-baseline.md docs/electron-backend-baseline.md scripts/capture_ws_trace.py frontend-handoff-v2/`
- [ ] `git commit -m "feat(baseline): Electron 后端改造批次 0——契约/测试/时间戳/事件面基线 + 真实 WS 轨迹"`

## 风险

- 工具审批轨迹若无真实工具调用路径，用合成轨迹并在文档标注——不伪造为"真实抓包"
- FakeLLMWithUsage 必须实现 stream_think 才能送达 usage chunk（否则 token_usage 不触发，轨迹缺 Token 事件）
- agent 未 start() 即连 /ws 导致抓包失败——脚本必须 `await agent.start()`/`stop()`
- `frontend-handoff-v2/traces/` 目录不存在需创建（`os.makedirs(exist_ok=True)`）
- FakeLLM 扩展不得破坏现有 1883 测试（默认行为不变）

## 非目标（本批次不做）

- 任何生产代码修改；认证/版本/错误（批次 1）；v2 事件（批次 2）。
