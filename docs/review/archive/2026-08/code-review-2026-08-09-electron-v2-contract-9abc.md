# Electron v2 后端契约封口 Batch 9A/9B/9C 代码审查

> 审查日期：2026-08-09  
> 审查仓库：`/home/yomu/elysia-agent`  
> 对比基线：`a5fc6202cbfe87e5c40bb6ae49b0a2b8a0c64c2f`  
> 审查 HEAD：`0939eef05f676bb98f0c33c0572eb7607d737bbc`  
> 提交范围：`bcb267a..0939eef`（Batch 9A/9B/9C，本地 12 个提交）  
> 审查性质：只读 code review；本轮未修改后端实现。

## 1. 结论

Batch 9A/9B/9C 的主体实现已经落地：`session.conversationId`、历史 cursor、显式会话存在性检查、版本协商拒绝、ClientMessage 判别联合、机器契约产物、输入音频 descriptor 和 dictation 会话内 STT profile 均有实现与测试。相关专项 938 个测试、契约漂移检查、Ruff 与 Pyright 均通过。

但本轮审查仍发现 **4 个 P1、2 个 P2**。其中三个 P1 已用现有测试桩或真实类直接复现，另一个 P1 是生成 TypeScript 契约的确定性类型漏洞。当前不建议把 Batch 9A/9B/9C 标记为完全封口，也不建议 Electron 前端直接把 `contract.raw.ts` 当作最终可信的客户端出站约束。

| 级别 | Finding | 直接影响 |
|---|---|---|
| P1 | MongoDB 离线时，新建会话立即被严格检查判为不存在 | Electron 创建会话成功后，文本/附件/语音全部收到 `SESSION_NOT_FOUND` |
| P1 | profile dictation 仍被全局 STT 门禁取消 | 无法正常 `stop` 获取 `dictation.final`，会收到 cancelled + NOT_FOUND |
| P1 | v2 `text` 绕过 Pydantic 联合校验 | 畸形字段抛 `TypeError`，单连接被断开 |
| P1 | TS `ClientMessage` 被 `UnknownMessage` 完全放宽 | 任意 `{type: string}` 都可通过 TypeScript，和 JSON Schema/运行时不一致 |
| P2 | 会话存在性只在入队前检查一次 | 排队/在途期间删除会话后，消费时仍可复活幽灵会话 |
| P2 | Golden frames 与真实运行时语义不一致 | 跨仓 replay 会固化错误的 `turnId` 和不完整的 `conversationId` 样例 |

## 2. Findings

### [P1] MongoDB 离线时，新建会话立即被严格检查判为不存在

涉及位置：

- `core/input/ws_handler.py:500-510`：`_session_or_none()` 对显式 ID 调 `has_session()`；
- `core/memory/session_manager.py:208-218`：`create()` 返回新 `SessionSummary`，但不把新会话放入缓存；
- `core/storage/session_repo.py:166-176`：MongoDB 未连接时 `create()` 静默返回；
- `core/input/session_routes.py:431-445`：REST 创建接口仍返回成功响应。

#### 触发流程

1. MongoDB 不可用，后端仍以 JSONL fallback/降级模式运行；
2. Electron 调 `POST /api/sessions`；
3. `SessionManager.create()` 生成并返回 `conv_*`；
4. `SessionRepository.create()` 因 `_db is None` 没有落库；
5. `SessionManager.create()` 也没有建立内存实例；
6. Electron 随后发送带该 `conversationId` 的 text、附件、`voice.commit` 或 realtime start；
7. 新增的 `has_session()` 严格门禁返回 false，合法的新会话被当成幽灵会话拒绝。

直接复现结果：

```text
created=conv_...
has_session=False
cached=False
```

#### 影响

- REST 明确告诉前端“创建成功”，但紧接着所有写入都失败；
- text/附件/voice.commit 走 `user_message.ack rejected + SESSION_NOT_FOUND`；
- realtime start 走 `ErrorMessage{code=SESSION_NOT_FOUND}`；
- 破坏项目原有“MongoDB 不可用时使用本地 JSONL”的降级语义；
- Electron 本地部署未启动 MongoDB 时尤其容易触发。

#### 建议修复

优先选择一个一致语义：

1. **保留降级能力**：`SessionManager.create()` 成功后立即建立该会话的内存实例并标记为空会话已加载；必要时同时写 JSONL fallback，使 `has_session()` 立即可见；或
2. **存储 fail-closed**：让 `SessionRepository.create()` 返回明确结果/抛 `StorageUnavailableError`，REST 创建接口返回 503，禁止返回一个后续不可用的 ID。

需要新增端到端测试：Mongo 离线 → REST create → 使用返回的 ID 发送 text/附件/voice realtime，行为必须与选定语义一致。

---

### [P1] 独立 profile dictation 仍会被全局 STT 门禁取消

涉及位置：

- `core/input/ws_handler.py:468-480`：`_voice_stt_gate()` 只看全局 `agent.stt_enabled`；
- `core/input/ws_handler.py:1018-1024`：同连接 `set_service(stt=false)` 无条件通知当前 VoiceSessionManager；
- `core/input/voice_sessions.py:394-450`：profile dictation 创建独立 `_dictation_stt`；
- `core/input/voice_sessions.py:581-589`：`feed_pcm()` 已正确把 profile 快照视为独立可用，但 JSON 控制入口没有同步修正。

#### 触发流程

1. 全局 STT 关闭；
2. 客户端发送 `dictation.start{sttProfileId: "sensevoice/sense-voice"}`；
3. 独立 profile backend 创建成功，dictation 进入活动态；
4. BINARY 音频可以正常走快照 backend；
5. 客户端发送 `dictation.stop`；
6. `_voice_stt_gate()` 在执行 stop 前发现全局 `stt_enabled=false`，先调用 `notify_stt_disabled()`；
7. 会话被 teardown 并发送 `dictation.cancelled`；
8. 原来的 `stop_dictation()` 随后执行，又返回 `DICTATION_NOT_FOUND`。

实测终态帧：

```json
{"type":"dictation.cancelled","clientRequestId":"req_profile"}
{"type":"error","code":"DICTATION_NOT_FOUND","message":"无活动转写"}
```

没有 `dictation.final`。

#### 影响

- “会话内 profile 独立于全局活动 STT”的承诺只在 BINARY feed 路径成立；
- 全局 STT 从一开始关闭时，profile dictation 无法正常完成；
- 同连接在 profile dictation 期间关闭全局 STT，也会错误取消独立会话；
- 会产生两个互相矛盾的终态响应（cancelled 后又 NOT_FOUND）。

#### 建议修复

- 给 `VoiceSessionManager` 增加公开查询，例如 `depends_on_global_stt()`；
- REALTIME 恒依赖全局 STT；DICTATION 仅当 `_dictation_stt is None` 时依赖全局 STT；
- `_voice_stt_gate()` 和 `notify_stt_disabled()`/`set_service` 通知都使用同一判断；
- profile dictation 的 stop/cancel 必须忽略全局 STT 开关并正常产生唯一终态；
- 增加完整 WS handler 测试，不能只直接调用 `VoiceSessionManager.stop_dictation()`。

至少覆盖：

1. 全局 STT 初始关闭 + profile start/feed/stop → final；
2. profile dictation 途中 `set_service(stt=false)` → 会话保持；
3. 无 profile dictation 途中关闭全局 STT → cancelled；
4. 每条路径只有一个终态帧。

---

### [P1] v2 `text` 特判绕过完整入站校验，畸形帧仍会断开连接

涉及位置：

- `core/input/ws_handler.py:842-874`：非 text 走 `TypeAdapter(ClientMessage)`，text 被显式跳过；
- `core/input/ws_handler.py:880-917`：只手工检查 `content` 类型和空文本；
- `core/input/ws_handler.py:596-671`：`clientRequestId`、`attachments`、`stickerId` 等继续直接消费原始 `data`。

当前代码为保留“text 校验错误走 ACK rejected，而不是 ErrorMessage”的协议语义，完全跳过了 `V2TextMessage` 校验。但 ACK 信封类型与是否执行 Pydantic 校验并不冲突：可以先校验，再把 ValidationError 映射为 rejected ACK。

#### 已复现输入

```json
{"type":"text","content":"hi","clientRequestId":"r1","attachments":1}
```

结果：

```text
TypeError: 'int' object is not iterable
```

另一个输入：

```json
{"type":"text","content":"hi","clientRequestId":["r2"]}
```

结果：

```text
TypeError: unhashable type: 'list'
```

这些异常不是 `ValidationError`，`handle_ws()` 文本消息循环没有兜底捕获，异常会退出连接生命周期。

#### 其他契约漂移

- `AttachmentRef` 在机器契约中要求 `attachmentId/kind/mime`，运行时 text 路径实际只要求 `attachmentId`；
- 非字符串 `clientMessageId/conversationId/stickerId` 也可能导致 ACK 构造失败、错误吞掉或后续类型异常；
- 因此“所有 Client→Server WS JSON 统一验证”在 text 主入口仍未完成。

#### 建议修复

1. v2 text 先处理缺失/非法 `clientRequestId` 的 ACK 目标信息；
2. 对完整帧执行 `V2TextMessage.model_validate(data)`；
3. 捕获 ValidationError，统一发送 `user_message.ack{status=rejected,error.code=CONTRACT_VALIDATION}`；
4. `_route_v2_text()` 接收校验后的模型，不再反复使用宽松 `data.get()`；
5. 增加表驱动/fuzz 负面测试，并在每个错误后发送合法 `interrupt` 或 text，证明连接仍存活。

---

### [P1] 生成的 TypeScript `ClientMessage` 被 `UnknownMessage` 完全放宽

涉及位置：

- `frontend_contract/__main__.py:55-63`：新注册 `ClientMessage` 判别联合；
- `frontend_contract/gen_ts.py:214-219`：生成器对**所有**判别联合都追加 `UnknownMessage`；
- `frontend_contract/contract.raw.ts` 与 `frontend/src/types/contract.generated.ts`：生成后的 ClientMessage 均含 catch-all。

当前产物为：

```ts
export type ClientMessage =
  | V2TextMessage
  // ...具名成员...
  | ToolStatusMessage
  | UnknownMessage

export interface UnknownMessage {
  type: string
  [key: string]: unknown
}
```

因为任何具名消息的 `type` 也是 `string`，下列对象都会满足 `UnknownMessage`：

```ts
const a: ClientMessage = { type: 'dictation.start' }          // 缺 clientRequestId
const b: ClientMessage = { type: 'voice.record.start' }       // 缺 recordingId
const c: ClientMessage = { type: 'not-supported-by-backend' } // 未知 type
```

#### 影响

- TypeScript 无法约束 Electron 的任何客户端出站消息；
- `contract.raw.ts` 与 JSON Schema `ClientMessage` 不一致：JSON Schema 是严格的 22 成员 oneOf，TS 却接受任意消息；
- Electron 若以 `ClientMessage` 作为发送 API 参数类型，会得到虚假的编译期安全；
- 机器契约工程化最核心的跨仓防漂移目标因此失效。

#### 建议修复

- `UnknownMessage` 只追加到 `ServerMessage`，用于前端接收未来服务端帧的 forward compatibility；
- `ClientMessage` 必须只包含后端当前支持的具名成员；
- 或给 `generate_ts()` 的每个 union 增加显式 `allow_unknown` 配置，禁止用 alias 名称隐式猜测；
- 新增断言：两个 TS 产物的 `ClientMessage` block 不含 `UnknownMessage`，而 `ServerMessage` 仍保留；
- 重新生成三个契约产物，并让 Electron 对出站类型做一次编译型负面测试。

---

### [P2] 会话存在性只在入队前检查，删除竞态仍会复活幽灵会话

涉及位置：

- `core/input/ws_handler.py:607-623`：消息入口检查一次目标会话；
- `core/input/ws_handler.py:696-735`：经过附件 I/O 或直接把字符串 ID 交给 `agent.process_message()`；
- `core/agent/agent.py:343-357`：Agent 忙时只把 `conversation_id` 字符串放入 pending queue；
- `core/memory/session_manager.py:161-170`：最终消费时 `resolve()` 仍对显式 ID 直接 `ensure_loaded()`；
- `core/input/session_routes.py:490-527`：非活动会话即使 Agent 忙也允许删除。

#### 竞态流程

1. 会话 A 存在，入口 `has_session(A)` 通过；
2. Agent 忙，消息被排入 pending queue；或者附件路径在入口检查后进行异步存储查询；
3. 另一 REST 请求删除非活动会话 A；
4. pending 消息稍后被消费；
5. CognitiveLoop 经 `SessionManager.resolve(A, 2)` 调用 `ensure_loaded(A)`；
6. `get(A)` 再次构造空 `SessionMemory`，已删除会话被复活。

直接验证管理器生命周期：

```text
checked=conv_...
exists_after_delete=False
resurrected=conv_... exists=True
```

#### 影响

- 违反 9A-3 “已删除会话不得重建”的核心目标；
- accepted ACK 可能对应一个随后被删除、再被后台消息复活的会话；
- 新消息、generation 归档和会话列表可能重新出现不一致状态；
- realtime voice session 持有字符串 `_conv_id`，也存在相同生命周期问题。

#### 建议修复

可组合采用：

- 删除会话前检查 pending/in-flight generation/voice 是否引用该会话，有引用则返回 `AGENT_BUSY`；
- 删除时清除/终止该会话对应 pending 工作，并给客户端明确终态；
- 最终 CognitiveLoop session resolver 对显式 v2 ID 保持严格，不再调用可懒创建的通用 `resolve()`；
- 为会话引入删除 tombstone/epoch，入口校验后的旧请求不能跨删除代际继续执行。

新增测试应真实覆盖：busy queue → 删除非活动会话 → drain queue，断言不重建、不归档、不生成到已删会话。

---

### [P2] Golden frames 与真实运行时语义不一致

涉及位置：

- `scripts/gen_contract_frames.py:76-117`：chat golden；
- `scripts/gen_contract_frames.py:175-189`：voice golden；
- `core/input/voice_sessions.py:600-626`：真实 realtime turn 发送；
- `core/cognitive/tests/test_v2_generation.py:209-215`：运行时 generation.* 的 conversationId 断言。

#### 已确认差异一：首轮 `turnId`

Golden frame：

```json
{"type":"voice_session.partial","turnId":0,"segmentIndex":0}
{"type":"voice_session.turn","turnId":0,"segmentIndex":0}
```

真实运行时在 `_on_segment()` 中先执行 `self._turn_id += 1`，再发送 partial/turn，因此首轮 `turnId=1`。公开协议文档同样写 `turnId=1`。

#### 已确认差异二：默认会话 `conversationId`

`_chat()` 构造的 ACK 和 generation.* 大量省略 `conversationId`；真实 v2 text 路径会把缺省会话解析为当前活动会话，ACK 填实际 ID，CognitiveLoop 的六类 generation.* 也携带非空实际 conversationId。

#### 影响

- Electron 跨仓 replay 会把错误的首轮 ID 固化进 mapper/store 测试；
- 默认会话 fixture 不会验证本批刚封口的 `conversationId` 贯通；
- 当前 frames 只证明“这些 Pydantic 模型能够序列化”，不能证明“运行时会发送同样的字段和值”；
- `make check-types` 会稳定地守住错误样例，因为生成器与归档来自同一份手写构造。

#### 建议修复

1. 立即把 voice golden 首轮 `turnId` 改为 1；
2. chat ACK 与全部 generation.* 填 `conversationId="default"`；
3. 尽可能复用运行时 builder，或增加 golden 与 `WebSocketChannel`/VoiceSessionManager 实际发送结果的语义对比测试；
4. 为每个 golden 文件增加必需语义断言，而不只做字节漂移；
5. 若继续使用 `.jsonl` 扩展名，Electron parser 需要明确跳过当前 `#` 注释行，或把元数据移到独立 manifest，保持标准 JSONL 每行均可解析。

## 3. 验证记录

### 3.1 通过的门禁

```text
make check-types
  python -m frontend_contract --check
  python scripts/gen_contract_frames.py --check
  PASS

conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__
  All checks passed!

make pyright
  0 errors, 1 warning, 0 informations

conda run -n Elysia python -m pytest \
  frontend_contract/tests Module/output/tests core/input/tests \
  core/desktop/tests core/storage/tests -q --tb=short
  938 passed, 1 warning in 102.98s
```

Pyright 的唯一 warning：

```text
core/monitoring/__init__.py: IdleMonitor is specified in __all__ but is not present in module
```

pytest 的唯一 warning 来自第三方 `webrtcvad` 使用即将移除的 `pkg_resources`。

### 3.2 本地命令环境说明

直接运行 `make lint` 时，当前未激活 Conda 的 shell 找不到裸 `ruff`；使用项目要求的 Elysia 环境执行相同 Ruff 命令后全绿。这不是本批代码缺陷，但 Makefile 的 lint 命令与 pyright/pytest 的 Conda 环境策略不一致，后续可单独统一。

### 3.3 为什么现有测试全绿仍有问题

现有测试主要按单模块验证：

- profile 独立测试直接调用 `VoiceSessionManager.stop_dictation()`，绕过了 WS `_voice_stt_gate()`；
- ClientMessage 负面测试覆盖了非 text 联合和 text 的 `content`，没有覆盖 text 的其他错误字段类型；
- 未覆盖 Mongo 离线状态下 REST create 与新增 `has_session()` 的组合；
- 未覆盖“入口验证后排队/异步等待，再删除会话”的生命周期竞态；
- golden frames 只做生成字节漂移，没有与运行时语义互证；
- TS 产物测试只断言 `ClientMessage` 存在具名成员，没有断言它不包含 `UnknownMessage`。

因此 938 tests 通过只能说明已有断言没有回归，不能推翻本报告中的跨组件复现结果。

## 4. 建议修复顺序

1. 修复 profile dictation 的全局 STT 门禁，保证唯一 final/cancelled 终态；
2. 让 v2 text 完整走 `V2TextMessage` 校验，消除可断连的 TypeError；
3. 修复离线 create 与严格存在性检查的矛盾；
4. 从 TS `ClientMessage` 移除 `UnknownMessage` 并重新生成契约；
5. 收口会话删除与 pending/in-flight 生命周期，彻底禁止幽灵复活；
6. 校正 golden frames，并增加与真实发送路径的语义互证。

完成以上修复后，应重新运行本报告 §3.1 的全部门禁，并新增覆盖六项 finding 的回归测试，再进行 Electron 跨仓 mapper replay。
