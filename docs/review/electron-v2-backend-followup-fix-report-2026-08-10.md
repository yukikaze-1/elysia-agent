# Electron v2 后端后续复验修复报告

> 日期：2026-08-10  
> 仓库：`/home/yomu/elysia-agent`  
> 修复前 HEAD：`c599811`  
> 状态：实现与回归测试已完成，尚未代用户创建提交

## 1. 结论

本轮直接修复了后端更新后复验出的四组问题：

1. 会话在 ACK accepted 后、最终消费或异步保存前仍可被删除；
2. realtime/dictation 的小于 640B BINARY 网络分包会被 VAD 逐块判静音并丢弃；
3. MongoDB 离线时 create 虽成功，但 list/detail/rename 看不到或改不了同一会话；
4. golden `.jsonl` 含非 JSON 注释，且 tool_calls 终态缺 `conversationId`。

修复没有新增对外 wire 字段，也没有数据库迁移。前端需要感知的行为变化主要是：删除有在途工作的会话会稳定返回可重试的 `409 AGENT_BUSY`；BINARY chunk 不再要求客户端自行凑成 640B；五个 golden 文件现在是可逐行直接解析的标准 JSONL。

## 2. 会话删除竞态：从 REST 预检查改为生命周期最终门禁

### 原问题

原删除接口只检查活动会话 FSM 状态和 Agent FIFO pending：

- 已从 `process_message()` 接受、但仍停留在 EventBus 的消息不在两个检查范围内；
- 缺省 `conversationId` 的 ACK 已解析到会话 A，最终消费却可能在 A 删除后重新读取 active，改投 default；
- 非活动会话生成中仍可被删除；旧 `SessionMemory` 的晚到 `save(upsert=True)` 会把已删文档重新创建；
- `_after_response()` 是独立后台任务，主生成结束后仍可能晚到保存并复活会话；
- realtime voice 只保存字符串 `_conv_id`，会话生命周期没有绑定。

### 实现

`SessionManager` 现在是会话生命周期的最终裁决点：

- 增加 lifecycle lock、每会话 accepted-work 引用计数，以及 `deleting/deleted` tombstone；
- v2 消息在 accepted 前调用 `reserve_work()`，同时把缺省会话固化成当时的实际 active ID；
- EventBus、FIFO pending、CognitiveLoop、取消/中断/队列满/关机都成对释放引用；
- `_after_response/session.save` 单独续持一份引用，晚到保存结束前 DELETE 仍被拒绝；
- `voice.commit` 在 ACK 前预占实际会话；realtime 会话生命周期和每个自动提交 turn 分别持有引用；
- DELETE 先在 manager 内检查引用并写 tombstone，再执行附件引用扫描与存储删除；扫描期间到达的新消息也不能穿透；
- tombstone 后 `get/resolve/has_session/reserve_work/switch` 均不能懒创建已删会话。

### 对外行为

- 会话有 EventBus/pending/生成/异步保存/realtime voice 工作时：
  `DELETE /api/sessions/{id}` → `409 AGENT_BUSY`，`retryable=true`；
- 删除已开始后到达的显式会话消息：ACK rejected `SESSION_NOT_FOUND`；
- 客户端省略 `conversationId` 时，消息归属以 accepted 前解析出的实际 ID 为准，不随之后 active 切换；
- 默认会话仍受 `SESSION_PROTECTED` 保护；无关联工作的非活动会话仍可正常删除。

## 3. 音频网络分包：跨 BINARY chunk 重组 VAD 帧

### 原问题

VAD 只接受 640B/20ms 完整帧，但 `feed()` 把每个 WebSocket BINARY chunk 独立交给 `_is_speech()`。Electron 若连续发送 256B chunk，即使累计 1.6 秒响亮 PCM，每块仍因 `<640B` 被判为非语音，最终零 STT 调用。

### 实现

- `AudioBuffer` 增加跨调用 VAD remainder；不足 640B 的检测尾部与下一 BINARY chunk 拼接；
- VAD 仍只处理连续的 640B/20ms 帧，但不再依赖网络 chunk 边界；
- VAD 每帧使用独立实例，避免同一检测块的 hangover 让残余语音污染后续静音多数决；
- VAD 检测缓冲与原始 STT 段分离，原始网络 PCM 只写一次，最终非整帧尾部不丢失、不重复；
- `reset/deafen/wake/segment` 均会正确清理检测 remainder。

### 对前端的明确契约

- 输入仍必须是 `pcm_s16le / 16000Hz / mono`；单条 BINARY 仍不得超过 1 MiB；
- BINARY 网络 chunk **无需**是 640B 的整数倍，256B、512B 等切法均可；
- `<640B` 的含义只剩“当前流末不足一帧时暂不单独做 VAD”，不再表示该网络 chunk 被丢弃；
- 普通录音模式的**整段总长度** `<640B` 仍按既有协议视为空转写。

## 4. Mongo 离线会话摘要一致性

`SessionManager` 增加进程内 local summary 层，并和 `repo.list_all()` 合并去重：

- offline create 后立即出现在 list/detail；
- offline rename 对已知缓存会话返回成功，并立即反映到 detail/list/search；
- 本地显式标题覆盖持久层旧标题，消息计数与持久层摘要按现有规则合并；
- delete 同步清理 local summary 与标题 override；
- REST 单会话摘要改从 `SessionManager.get_summary()` 读取，不再绕过降级层直查 repo。

这部分保持项目现有的“Mongo 离线时进程内/JSON fallback 降级”边界，没有新增独立数据库或 wire 字段。

## 5. Golden frames 与跨仓 replay

- 五个 `ws-v2-*.jsonl` 移除 `#` 文件头注释；每个非空行现在都是 JSON object；
- 非事实源、生成器、漂移门禁等元数据移到 `traces/README.md`；
- tool fixture 的 `generation.completed{finishReason:"tool_calls"}` 补
  `conversationId:"default"`；
- 语义测试扩为：所有 golden 中的 `generation.*` 必须携带非空 `conversationId`；
- Electron replay/parser 不再需要实现“跳过 `#` 行”的私有规则。

## 6. 回归覆盖

新增或强化的测试覆盖：

- accepted work 阻止删除，终态释放后允许删除；
- 缺省会话在 accepted 时固化；
- `before_delete` 附件扫描期间 tombstone 拒绝新工作；
- EventBus 窗口与真实 Agent CognitiveLoop 终态引用释放；
- `_after_response/save` 晚到窗口持续阻止删除；
- realtime voice 生命周期/turn 与 voice.commit 的预占、释放；
- 200 个 256B loud chunk 跨帧重组，WAV PCM 长度与输入逐字节一致；
- Mongo 离线 REST create → list → rename → detail 完整链；
- 所有 golden 逐行 `json.loads`，以及全量 generation 会话归属。

验证结果：

| 门禁 | 结果 |
|---|---|
| 会话/语音/WS/音频/契约专项 | `308 passed` |
| `make test-quick` | `2894 passed, 2 skipped, 95 deselected` |
| Ruff | `All checks passed` |
| Pyright | `0 errors`（15 条为仓库既有缺包/`__all__` warning） |
| contract + golden 漂移 | `frontend_contract --check` 与 `gen_contract_frames.py --check` 均通过 |
| diff 格式 | `git diff --check` 通过 |

quick suite 的 warning 为仓库既有 `webrtcvad/pkg_resources` 弃用提示及部分旧测试
`AsyncMock` 未 await 提示，本轮无新增失败。

## 7. 给 Electron 开发侧的同步摘要

1. 不需要更新 raw schema/领域类型；本轮没有新增 wire 字段。
2. DELETE 收到 `409 AGENT_BUSY + retryable=true` 时保留会话并稍后重试，不要本地先永久移除。
3. 发送 PCM 时可以继续用 640B/20ms，也可以按任意更小网络 chunk 发送；格式仍固定为 16k mono s16le。
4. golden replay 直接逐行 JSON.parse；不要再依赖或保留 `#` 注释过滤逻辑。
5. `generation.*.conversationId` 仍应视为必需归属字段，tool_calls 完成帧也一样。
