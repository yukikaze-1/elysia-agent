# Electron 后端改造 — 批次 0：基线确认（Spec）

**Spec 版本：** v0.1
**文档日期：** 2026-08-04
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-001）与 `docs/15-Cyrene-后端改造执行上下文与Prompt-v0.1.md`（批次 0）
**基线 Commit：** `1e62b58`（当前 HEAD，含两份 PRD 文档）

---

## 1. 目的

在改动任何生产代码之前，把当前后端的事实基线**钉死、可复现**，产出：
1. `docs/electron-backend-baseline.md` —— 事实基线文档；
2. 至少 1 份真实、脱敏的 WebSocket JSONL 轨迹（完整聊天），另加取消、工具审批轨迹。

**本批次是验证 + 文档批次，不修改生产代码。** 若验证发现真实矛盾（代码与 PRD 描述不符且影响后续批次决策），停下来回主控确认，不得静默假设。

## 2. 待验证事实清单

每项必须给出**真实证据**（命令输出、代码引用），不得只写结论。

| # | 事实 | 验证方式 | 通过标准 |
|---|---|---|---|
| F1 | 仓库状态 | `git status --short`、`git log --oneline -1`、`git branch --show-current` | 工作树干净，HEAD=`1e62b58`，branch=`main` |
| F2 | 契约漂移 | `python -m frontend_contract --check`（Elysia conda） | 无漂移输出，exit 0 |
| F3 | TS 快照与源一致 | `make check-types` | 通过 |
| F4 | 快速测试基线 | `make test-quick`（Elysia conda） | 全绿；**当前实测 1883 passed / 2 skipped / 58 deselected**，记录为基线 |
| F5 | 时间戳现状 | 审计 `datetime.now()` / `utcnow` / `timezone.utc` 用法 | 核心数据（消息/Token/会话）走 `core.schema.utils.now_iso()` = UTC ISO；naive `datetime.now()` 用法共 4 处，均在展示/通知/工具路径，不落库：`idle_monitor.py:120`（显示）、`loop.py:1044`（_build_wake_up_message 文案）、`sensory.py:51`（系统提示上下文）、`tool/__main__.py:32` |
| F6 | 工具清单 | 读 `core/tools/__init__.py` ALL_PLUGINS | 12 个插件类，全部服务器进程内（Backend Runtime） |
| F7 | 认证范围 | 读 `core/input/server.py` | `auth_token`（SERVER_AUTH_TOKEN）仅护 `/shutdown /restart /tools /tools/reload` + `/debug/*`；`/ws`、`/api/*`、`/uploads/*` 匿名 |
| F8 | 上传匿名静态 | 读 `core/input/server.py` add_static | `/uploads/*` 公开静态，无鉴权 |
| F9 | WS 事件面 | 读 `frontend_contract/ws_message.py` | 现有 ServerMessage 判别联合成员清单 + Client→Server 消息类型；**无** protocol_version / generation / response / segment / message ACK 字段 |
| F10 | 消息落库结构 | 读 `core/schema/message.py` + `core/memory/session.py` + `core/storage/message_repo.py` | Message 字段（message_id/role/content/timestamp/metadata）；timestamp 走 `now_iso()`（UTC）；会话回放 `messages_for_client()` 结构 |
| F11 | 单活动会话 | 读 `ws_handler.py` 建连回放 + agent 组合根 | 唯一 session_id、唯一 SessionMemory，无会话选择/切换接口 |
| F12 | STT 自动提交 | 读 `core/input/ws_handler.py` `do_transcription` | 识别即 post `UserInputData`，无确认环节 |
| F13 | 审批参数截断 | 读 `ws_handler.py` tool_approval 广播 + `Module/tool/manager.py` | `str(params)[:200]` 截断串作为兼容摘要 |
| F14 | Backend/Desktop 映射 | 读 `docs/14` §5、§3.1 与 `docs/15` §三 | 沙盒 `read_file/write_file` 标注为 Backend，无"误标 Desktop"问题；交接包中的误标在本仓库文档中已被纠正 |
| F15 | 真实 WS 事件抓取 | 运行抓包脚本（见 §3） | 见 §3 验收 |

## 3. 真实 WS 事件抓取

### 3.1 目的

产出 PRD BE-001 要求的真实脱敏 JSONL：**建连 → 用户消息 → Inner Voice → 至少三句 Public Reply → Token/模型/心情 → 完成**。同时产出**取消轨迹**与**工具审批轨迹**各一份。

### 3.2 方法

- 复用 `core/tests/fake_llm.py` 的 `FakeLLMBackend` 或子类，响应串包含协议标签以触发真实链路：
  - `<inner_voice>…</inner_voice>`（触发 `command=inner_voice`；标签解析已核：`core/text/tags.py:128` META + `loop.py:989-998`）；
  - `<mood>…|强度</mood>`（触发 `expression` 心情事件；`loop.py:984-987` `_apply_mood`）；
  - 多句标点（触发多个 `delta` / `message` 句子边界）。
- **聊天轨迹必须 TTS 禁用（或 mock）**：若 TTS active+enabled，SENTENCE 走 `_tts_mgr.stream_sentence`（loop.py:1015-1018），不会发 delta/message 句子边界事件且会真调 TTS 后端。抓包时 `config.tts.enabled=false` 或注入 mock。
- **工具审批轨迹需要带 `stream_think` 的 fake**：`FakeLLMBackend` 无 `stream_think`，经 `LLMAdapter.stream_think`（`hasattr` 为假）走 else 分支 `await chat()` + 逐字符 yield，`tool_calls` 恒为 None，**无法触发 HITL 审批**。需子类 `FakeLLMBackend`（参照 `test_phase3.py:1243-1311` 的 `async def stream_think` 模式）yield 指向需审批工具（`save_memory`/`write_file`）的 `tool_calls` delta，触发审批广播（CommandMessage tool_approval），脚本回传 `{"type":"tool_approval","request_id":...,"approved":true}`。
- **流式路径注明**：基础 `FakeLLMBackend` 无 `stream_think` → 轨迹实际走"非流式 `chat()` + 适配器逐字符重切"（标签仍可解析，但非真实流式路径）；如需更真实，给 fake 补 `stream_think`。基线可接受，文档注明。
- 用 `create_app(agent, serve_static=False)` + aiohttp 客户端连接 `/ws`，逐帧记录。
- 取消轨迹：interrupt 需紧跟用户消息发送（或拉长 fake 响应），否则生成太快无法打断。
- 输出逐行 JSONL，字段去敏（删除环境变量、路径、真实文本 → 替换为占位符）。

### 3.3 验收

1. 每行是合法 JSON（`json.loads` 通过）；
2. 事件序列与事件流测绘的预期一致；
3. 轨迹可重放（同一响应串 + 同一脚本 → 稳定事件序列）；
4. 无敏感数据（API key、本地路径、真实姓名）。

### 3.4 轨迹存放位置

- 完整聊天：`frontend-handoff-v2/traces/ws-chat.jsonl`（`frontend-handoff-v2/` 未被 gitignore）
- 取消：`frontend-handoff-v2/traces/ws-cancel.jsonl`
- 工具审批：`frontend-handoff-v2/traces/ws-tool-approval.jsonl`

## 4. 决策确认

| 决策 | 状态 | 结论 |
|---|---|---|
| D5 时间戳 | **确认** | 核心数据已统一 UTC ISO 8601（`now_iso()`）；v2 契约沿用 UTC。展示路径的 naive `datetime.now()` 不落库，批次 0 不修改。Z 后缀 vs `+00:00` 收口留到批次 2 契约定义 |

## 5. 产出交付物

1. `docs/electron-backend-baseline.md` —— 结构：
   - 仓库/HEAD 状态；
   - 契约与测试基线（F2-F4 结果）；
   - 时间戳事实（F5）与结论；
   - 事件面现状（F9：ServerMessage 成员清单、Client→Server 类型、缺什么）；
   - 消息落库与会话结构（F10-F11）；
   - 语音/工具/认证现状（F7-F8、F12-F13）；
   - 工具清单与 Runtime 标注（F6、F14）；
   - 真实轨迹摘要与存放位置（F15）；
   - 与 PRD 描述的差异清单（无差异或已消除的明确列出）。
2. 三份 WS JSONL 轨迹（§3.4）。

## 6. 退出标准

- [ ] F1-F14 全部有真实证据；
- [ ] F15 三份轨迹可解析、可重放、无敏感数据；
- [ ] `docs/electron-backend-baseline.md` 完成且无悬案；
- [ ] 未修改生产代码（`git status` 仅新增交付物）；
- [ ] 本 spec 与对应 plan 已 review。

## 7. 非目标

- 不实现认证（批次 1）；
- 不实现协议版本协商（批次 1）；
- 不实现统一错误目录（批次 1）；
- 不实现 v2 事件（批次 2）；
- 不修改 `now_iso()` 或任何时间戳代码。
