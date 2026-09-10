# Electron v2 联调：后端契约封口（Batch 9A + 9B + 9C）设计

> 审计基线：后端 `911bbe0`、Electron 前端 `4bc7f01`。本设计承接 todo.md「Electron v2 联调：后端契约封口（2026-08-09 审计）」表格中的 8 项后端待办（Batch 9A/9B/9C）。
> 审计完整清单与跨仓库契约差异见 `/home/yomu/elysia-frontend/docs/18-Cyrene-Electron-真实后端契约差异与前端修复清单-2026-08-09.md`。
> 文档导航：[项目文档](../../../../../README.md)

## 0. 决策记录（本批拍板，2026-08-09）

> 按用户 standing 指令：需要决策的点均记录于此，最终报告中复述。

### 0.1 用户拍板（4 项）

| # | 决策 | 选项 | 定案 | 影响 |
|---|------|------|------|------|
| D1 | 批次范围 | 一次全做 10 项 / 只做 9A / **9A+9B+9C 八项** | **9A+9B+9C，产品化 defer** | 鉴权 fail-closed（todo#38）+ 幂等持久化（todo#39）两项**不纳入本批**，单独批次 |
| D2 | 跨仓 replay CI | **后端侧产物 + frames 归档** / 本批就触碰 elysia-frontend | **后端侧产物** | 本批**不修改 elysia-frontend 仓库**；产出 JSON Schema + 独立 TS raw contract + golden frames 归档 + 后端漂移检测 |
| D3 | 输入音频契约策略 | **固定格式 + descriptor** / 多格式服务端解码 | **固定格式** | 16kHz/mono/pcm_s16le 为唯一支持格式；start 帧带 descriptor 供校验；后端零解码依赖 |
| D4 | 历史分页 cursor | 显式 cursor + 双兼容 / **严格区分** | **严格区分** | `before`/`after` 只接受返回的 opaque cursor；**稳定 messageId 不再作为分页参数** |

### 0.2 探索定案（本批推荐，记录备查）

- **9A-1**：`SessionMessage` 增必发 `conversationId: str`（建连=活动会话、activate=被切会话）。旧客户端忽略新增字段即可；v1 亦发该字段（v1 前端不解析）。
- **9A-3**：v2 显式 conversationId 不存在 → `user_message.ack rejected + SESSION_NOT_FOUND`（text/附件/voice.commit）；REALTIME `voice_session.start` 显式未知 → `ErrorMessage` SESSION_NOT_FOUND；缺省 ID 仍走当前 active。不得构造内存/持久化会话、不得增加附件 refs。
- **9C-2**：`dictation.start.sttProfileId` 用 `<provider>/<model>` 模型标识（与 `switch_stt` 一致）；start 时解析并**快照 backend 实例到 dictation session**；不改 `agent.stt` / `service_state`；stop/cancel 释放本会话新建实例。
- **9B-3**：握手头传输 token 与展示版本区分——`X-Protocol-Version` 支持集合 **{缺省/空 → v1, `"1"` → v1, `"2"` → v2}**，其余显式值（`"2.0"`/`"3"`/乱写）→ **升级前 HTTP 400 + ProtocolError**（复用 `server.py` 的 `_protocol_error_response` 模式，`server.py:105-119`）。显式 `"1"` **必须保留合法 v1**（现网 v1 客户端 + `test_v2_ws.py:450` 正以 `"1"` 头驱动 legacy STT）。`server_info.protocol_version` 展示值 `"2.0"` 保留，文档明确二者语义。
- **9B-2**：v2 连接入站 JSON 统一走 `ClientMessage` 判别联合 `model_validate`；拒绝 unknown type / 错误枚举 / 错误字段类型 / 缺必填 → `ErrorMessage` + ACK rejected `CONTRACT_VALIDATION`，**畸形消息不断连**。v1 保持现状（`data.get` 老路径）。顺带修复 `handle_ws` 顶层 `ValidationError("...")` 单参构造在 pydantic 2.13 抛 `TypeError` 的 bug（非 dict 帧会断连）。
  - **判别联合成员**：`type="text"` 下**只含 `V2TextMessage`**（`content: str`），**不含 `TextMessage`**——二者 discriminator 同为 `"text"`，pydantic 判别联合无法共存（实测抛 `TypeError: mapped to multiple choices`）。`TextMessage`（v1 专属，`content: str | list[ContentPart]`）**不加入判别联合**，v1 路径保持 `data.get`。收紧语义：**v2 的 `text` content 恒为 `str`**（对齐 `V2TextMessage` 契约；v2 媒体走 `attachments`/`stickerId`，不走 content list）；v2 发 list-content text → `CONTRACT_VALIDATION`（Electron 用 attachments 承载媒体）。现网 Vue v1 不设 v2 头，走 v1 路径不受影响。

### 0.3 探索发现的现状偏差（todo 未覆盖，纳入本批）

| 偏差 | 说明 |
|------|------|
| 9A-2 前端无调用方 | 当前前端**无任何** `/api/sessions/{id}/messages` 调用方（`ArchivePanel.vue` 走 `/debug/api/archive` offset 分页，不在此端点）。「严格区分」**无现存前端破坏**。唯一从 `_id` 猜游标的是 `chatStore.ts:274-278` 的旧 v1 `/api/messages` 端点——本批不动（Electron 走新端点，后续专项）。 |
| 9A-3 第三幽灵入口 | `voice_sessions.py:526` `_resolve_conversation`（REALTIME `voice_session.start`）同样直接 `resolve_session` 懒建幽灵会话。todo 只点名 `ws_handler.py:541/704`。 |
| 9B-1 trace 陈旧 + 路径失效 | `docs/archive/frontend-handoff-v2/traces/` 8 个 jsonl 已过时（`by_model` 未随批次 8c camel 化更新为 `byModel`、features flag 过期）且已归档声明「参考，非权威」；`scripts/capture_ws_trace.py:44` TRACES_DIR 指向游离目录（非 docs/archive）。 |
| 9B-2 实测断连 bug | `ws_handler.py:304` 非 dict 帧抛单参 `ValidationError(...)` → pydantic 2.13 `TypeError` → 冲出 `async for` 断连，而非错误信封。 |
| 9B-1 无 `__main__` 测试 | `python -m frontend_contract --check` 漂移逻辑无单测，CI 门禁是唯一防线。 |

## 1. 范围

**本批做（8 项）**：

- **Batch 9A（文本/历史联调阻断）**：session 回放 conversationId、历史分页 cursor、显式未知会话拒绝。
- **Batch 9B（契约工程化）**：机器契约产物（JSON Schema + TS raw contract + golden frames 归档 + 后端漂移检测）、统一入站校验、严格版本协商。
- **Batch 9C（语音联调）**：输入音频 descriptor/校验、dictation STT profile 隔离。

**本批不做（defer，单独批次）**：非回环生产绑定 fail-closed 鉴权（todo#38）、幂等键跨进程持久化（todo#39）。

**不应登记成本批后端缺陷的前端适配项**（Electron Adapter/Accumulator 职责，后端不为兼容而伪造 delta/改契约）：`generation.segment.completed.content` 权威文本零 delta、`generation.completed.conversationUsage = {total, byModel}`、raw wire 与领域层命名差异（`type/delta/mood` vs `event/text/emotion`）、Desktop bridge 安全元数据。

## 2. Batch 9A — 文本与历史联调阻断

### 2.1 9A-1：session 回放携带明确 conversationId

**现状**：`SessionMessage`（`frontend_contract/ws_message.py:253-258`）仅 `messages`/`token_stats`，无 `conversationId`。建连先发 session（`ws_handler.py:164-166`）后发 server_info（`:176-179`）；Electron 重连无法仅凭回放确认消息归属。

**设计**：

1. `SessionMessage` 增加必发字段：
   ```python
   conversationId: str = Field(description="本回放消息所属会话 ID")
   ```
   （必填——后端总是能确定目标会话；旧客户端忽略新增字段即可，v1 前端不解析。）

2. 唯一构造点 `_send_session_replay`（`ws_handler.py:83-98`）构造时填 `str(session.session_id)`。建连回放与 `_handle_activate_session` 回放均经此函数，天然带实际目标会话 ID——无需改两个调用点。

**契约变更**：`SessionMessage` + `conversationId`。
**涉及文件**：`frontend_contract/ws_message.py`、`core/input/ws_handler.py`、`frontend/src/types/contract.generated.ts`（gen-types 重生成）。
**测试**：
- `frontend_contract/tests/test_ws_message.py`：SessionMessage 构造含 conversationId + wire 含该键。
- `core/input/tests/test_server.py` session 回放 wire 断言加 `conversationId`。
- `core/input/tests/test_session_ws.py` `test_activate_session_replays_new_active`：断言回放 conversationId = 被切会话 ID。
- golden frames 更新（见 3.1）。

### 2.2 9A-2：历史分页显式 opaque cursor（严格区分）

**现状**（`session_routes.py:544-566` + `message_repo.py`）：`before` 只接受 Mongo `_id`（24-hex，先 `ObjectId(before)` 校验），`after` 接受稳定 `messageId(msg_)`——两参格式不对称；响应 `MessagesPageResponse`（`rest.py:325-330`）仅 `messages/has_more/total`，无 cursor；前端只能从私有 `_id` 猜下一游标。现有 Electron 文档误写 `before={messageId}`，真实接入 400。

**设计（D4 严格区分）**：

1. **参数**：`before`/`after` 只接受服务端返回的 opaque cursor。cursor 值 = Mongo `_id` 字符串（24-hex），服务端**不解释内容、仅做合法性校验**（非法 → `400 CONTRACT_VALIDATION`）。**`after` 增加与 `before` 对称的 24-hex 校验**（现状只有 before 校验）。**稳定 messageId（`msg_` 前缀）不再作为分页参数**——请求 `before={messageId}`/`after={messageId}` 返回 `400 CONTRACT_VALIDATION`。
2. **响应**：`MessagesPageResponse` 增加 `cursor: Optional[str]`（有更多页时返回，指向当前页同方向的下一页边界）：
   - `before` 查询：取返回页**最旧**消息 `_id` → 客户端回传 `before=<cursor>` 翻更早页。
   - `after` 查询：取返回页**最新**消息 `_id` → 客户端回传 `after=<cursor>` 翻更新页。
   - 无更多页 / 空页 → `cursor` 省略（None）。
3. **repo 层**：`query_page` 签名不变（`(messages, has_more)`；`session_routes.py:309/362/461/562`、`attachment_release.py:91` 零破坏）。**`query_after` 锚点从 `message_id` 字段改为 `_id`**——严格区分后 after 只收 `_id` cursor（24-hex），原 `{session_id, message_id: <cursor>}` 锚点查不到（`message_id` 存 `msg_` 值）→ 静默空页；改为 `{session_id, "_id": {"$gt": oid}}` 正序过滤。唯一调用方是 `sessions_messages_handler` after 分支，无外部破坏。
4. **cursor 计算（handler，契约转换前）**：从 raw 查询结果取 `messages[-1].metadata.get("_mongo_id")`（`core.schema.Message` 的 Mongo id 在 `metadata`；**不是** wire 序列化别名 `_id`，那是 rest 契约视图字段）。空页守卫：`cursor = tail if messages and has_more else None`（删除锚点 → 空页 + has_more False → cursor=None，不 IndexError）。**不可从契约视图 `_contract_messages` 输出取**——单条契约化失败会丢弃消息，导致游标偏移、下一页重复返回已显示消息（M2 既有策略）。
5. **向后兼容**：`_id` 形式的旧 cursor 值恰好与新 cursor 值形式相同，旧客户端手传 `_id` 仍可用；仅 messageId 形式被拒。现有 Vue `ArchivePanel.vue` 不调此端点，**无破坏**（见 0.3）。

**契约变更**：`MessagesPageResponse` + `cursor`；`before`/`after` 语义从「messageId/_id 混合」收敛为「仅 cursor」。
**涉及文件**：`frontend_contract/rest.py`、`core/input/session_routes.py`、`core/storage/message_repo.py`（仅 `query_after` 锚点 `message_id`→`_id`）、`frontend/src/types/contract.generated.ts`、`docs/electron-backend-protocol-v2.md`（协议文档更正 `before={messageId}` 描述）。`query_page` 零改动。
**测试**（todo DoD 全部补齐，路由级）：
- 两页无重/无漏（倒序 before、正序 after 各一）。
- 非法 cursor（非 24-hex / messageId，before 与 after 两侧）→ 400 CONTRACT_VALIDATION。
- 删除锚点（cursor 指向的消息已被删）→ 空页 + cursor=None + has_more 语义正确，不 500。
- `before={messageId}` / `after={messageId}` → 400（严格区分）。
- cursor 取自契约转换前的 raw 边界（含无法契约化消息的页不产生游标偏移/重复）。

### 2.3 9A-3：显式 conversationId 验证存在（禁止懒创建幽灵会话）

**现状**：`SessionManager.resolve()`（`session_manager.py:161-170`）对显式 ID 直接 `ensure_loaded()`（内部 `get()` 构造空会话进缓存 + 后续 save 落库）。`_route_v2_text`（`ws_handler.py:541`）、`_handle_voice_commit`（`:704`）、`voice_sessions._resolve_conversation`（`:526`）三个入口均可因拼错/已删 ID 重建幽灵会话。CRUD/activate 已有 `has_session()` 存在性检查（对比基准）。

**设计**：

1. 新增 helper（`ws_handler.py` 或 `session_manager.py`）：
   ```python
   async def _session_or_none(agent, conversation_id: str | None) -> SessionMemory | None:
       """v2 显式 ID → has_session 校验；未知返回 None；缺省 → 当前 active。"""
   ```
   语义：显式 ID 且 `not await agent.session_manager.has_session(SessionId(conversation_id))` → None；缺省 → `resolve(None, 2)`（active）。
2. **文本/附件路径**（`_route_v2_text` 行 540-545）：`conversation_id` 显式未知 → `_send_ack(ws, "", client_request_id, client_message_id, conversation_id, "rejected", error={"code": "SESSION_NOT_FOUND", "message": f"会话不存在: {conversation_id}", "retryable": False})` 并 return。**不生成 messageId、不写幂等表、不建会话、不增附件 refs**。
3. **voice.commit 路径**（`_handle_voice_commit` 行 701-707）：显式未知 → `_send_voice_error`（或 ACK rejected）`SESSION_NOT_FOUND`，不提交。
4. **REALTIME `voice_session.start`**（`voice_sessions.py:526` `_resolve_conversation`）：显式未知 → `_emit_error` `SESSION_NOT_FOUND`，不建会话不建 audio buffer。
5. `has_session()` 已是只读（不构造、内存缓存命中或持久层有 doc），直接复用；异常（repo 挂）→ 按未知拒绝（fail-closed）。

**涉及文件**：`core/input/ws_handler.py`、`core/input/voice_sessions.py`、`core/memory/session_manager.py`（若加 helper）。
**测试**：
- 文本：显式未知会话 → ACK rejected `SESSION_NOT_FOUND`；不生成 messageId；`session_manager` 缓存/持久层无该会话。
- 附件：显式未知会话 + 合法附件 → rejected `SESSION_NOT_FOUND`，附件 refs 不增。
- voice.commit：显式未知 → SESSION_NOT_FOUND。
- REALTIME start：显式未知 → ErrorMessage SESSION_NOT_FOUND，不建 buffer。
- 缺省 ID → 仍走 active（既有 `TestConversationIdRouting` 正例保持绿）。

## 3. Batch 9B — 契约工程化

### 3.1 9B-1：版本化机器契约产物 + golden frames + 漂移检测（后端侧）

**现状**：`frontend_contract` Pydantic 单源 → `gen_ts` → `contract.generated.ts`（Vue 目录）+ `make check-types` 漂移门禁已闭环。但**只有 TS 一个产物**：无 JSON Schema、无独立 raw contract、无 schema hash/协议版本、golden traces 已过时且无漂移检测、`capture_ws_trace.py` TRACES_DIR 路径失效、`__main__ --check` 无单测。Electron 仓库手写领域 Zod、未持有 raw schema。

**设计（D2 后端侧产物，不碰 elysia-frontend）**：

1. **JSON Schema 发射器**：`frontend_contract/gen_schema.py`（新文件，零第三方依赖——用 pydantic `TypeAdapter(T).json_schema()`）。对 `ServerMessage` 判别联合 + 全部子模块模型生成 JSON Schema → 产物 `frontend_contract/contract.schema.json`。
2. **独立 TS raw contract**：`gen_ts` 复用，新增产物入口 `frontend_contract/contract.raw.ts`（Electron 可消费的纯 wire 类型，**不落 Vue 目录**）。与 `contract.generated.ts` 的关系：同一发射器的第二产物；`contract.raw.ts` 后续由 elysia-frontend 订阅（本批不建订阅）。
3. **产物版本化**：产物文件头带 `transportVersion: "2"`（**握手头可用值**——Electron 照抄即合法，避免 9B-3 对显式 `"2.0"` 的 400）+ `protocolVersion: "2.0"`（**仅展示**，文件头注释注明「不可用作握手头」）+ `schemaHash`（产物正文 sha256）。`frontend_contract/version.py`（新）定 `TRANSPORT_VERSION = "2"`、`PROTOCOL_VERSION = "2.0"`。产物头注释明确：握手头必须用 `transportVersion`。
4. **golden frames 从契约源生成**：新增 `scripts/gen_contract_frames.py`（契约层、无网络——构造 `ServerMessage` 契约模型 + `wire_dict()` 序列化），生成当前 handshake/chat/session/tool/voice golden frames 到 `docs/archive/frontend-handoff-v2/traces/`（frames 文件头标注「由 frontend_contract 契约源生成，非事实源，漂移门禁兜底」）。同一发射器即权威源——**禁止归档 trace 重新成为事实源**。**停用 `scripts/capture_ws_trace.py`**（标记 deprecated 并从文档引用移除）——它是连真实服务的捕获工具，与契约源生成双写同一 traces 目录会互相覆盖（真实消息 id/timestamp 与确定性帧冲突）导致漂移检测误报（F5）。
5. **漂移检测扩展**：`__main__.py` 增加第二产物（schema.json / raw.ts / frames）的 `--check` 漂移比对（复用现有 `git diff --exit-code` 模式）；`make check-types` 覆盖新产物；CI quick job 不变（已跑 check-types）。
6. **补单测**：`python -m frontend_contract --check` 漂移逻辑（构造产物 → 改动 → check 返回非 0）+ `gen_schema` 对判别联合产出有效 JSON Schema。

**契约变更**：无 wire 变化；新增可发布产物。
**涉及文件**：`frontend_contract/gen_schema.py`（新）、`frontend_contract/contract.schema.json`（新）、`frontend_contract/contract.raw.ts`（新）、`frontend_contract/version.py`（新）、`frontend_contract/__main__.py`、`frontend_contract/gen_ts.py`、`scripts/gen_contract_frames.py`（新）、`scripts/capture_ws_trace.py`（标记 deprecated）、`docs/archive/frontend-handoff-v2/traces/*`（重生成）、`Makefile`、`frontend_contract/tests/`。
**测试**：`test___main___check.py`（漂移返回非 0）、`test_gen_schema.py`（判别联合 schema 有效、schemaHash 存在）、frames 生成脚本跑通且输出稳定。

### 3.2 9B-2：所有 Client→Server WS JSON 统一走 Pydantic 判别联合校验

**现状**：`handle_json_message`（`ws_handler.py:757-1069`）300+ 行 if/elif + `data.get()` 手工解析；未知 msg_type 静默忽略（`:1069`）；23 个客户端消息类已建模（`ws_message.py:636-878`）但未聚合、handler 从未 `model_validate`；顶层捕获 `JSONDecodeError/ValidationError`，且 `:304` 单参 `ValidationError(...)` 在 pydantic 2.13 抛 `TypeError` → 非 dict 帧断连（0.3 记录）。

**设计**：

1. **`ClientMessage` 判别联合**（`frontend_contract/ws_message.py` 新增）：`Annotated[Union[成员], Field(discriminator="type")]`（参照现有 `ServerMessage` 联合，`ws_message.py:883-924`）。**成员不含 `TextMessage`**——其 `type="text"` 与 `V2TextMessage` 撞 discriminator，pydantic 判别联合无法共存（0.2 记录）；`type="text"` 由 `V2TextMessage` 独占。缺失建模的客户端消息类型补齐（核对分支清单：wake_toggle/interrupt/switch_model/set_tts_voice/set_service/tool_approval/activate_session/tool.status/voice.record.start|stop/voice.commit/voice.discard/voice_session.start|pause|resume|stop|cancel/dictation.start|stop|cancel/audio.cancel/text）。
2. **入口校验**（`handle_json_message` 顶部，v2-only）：`_find_channel().protocol_version >= 2` 时：
   ```python
   try:
       model = TypeAdapter(ClientMessage).validate_python(data)
   except ValidationError as e:
       # 回 ErrorMessage(CONTRACT_VALIDATION, details=...)；text 类消息再 ACK rejected
       return
   ```
   校验失败 → `ErrorMessage`（不断连）；成功 → 各分支改用模型对象字段（不再 `data.get`）。**v1 连接保持现状**（`data.get` 老路径，不破坏 v1 多模态 text 等）。
3. **未知 msg_type**：判别联合对 unknown type 抛 ValidationError → 走统一 `CONTRACT_VALIDATION` 拒绝（从静默忽略改为显式拒绝）。
4. **修断连 bug**：`:304` 非 dict 帧判断改为显式（`if not isinstance(data, dict)` 已存在但抛错构造错误）——统一入口校验后，非 dict 帧直接 `ErrorModel` 拒绝，不构造 `ValidationError`。删除单参构造。
5. handler 不再依赖宽松默认值（`enabled = data.get("enabled", False)` 等改为模型字段）。

**契约变更**：新增 `ClientMessage` 判别联合（聚合既有类，不改字段）。
**涉及文件**：`frontend_contract/ws_message.py`、`core/input/ws_handler.py`、`core/input/voice_sessions.py`（若签名收紧）、`frontend/src/types/contract.generated.ts`、`frontend_contract/tests/`。
**测试**（todo DoD）：表驱动负面——unknown type / 错误枚举 / 错误字段类型 / 缺必填 → `ErrorMessage CONTRACT_VALIDATION` 且**连接存活**（后续帧仍被处理）；畸形消息不断连（连续 5 条非法帧后正常帧成功）；v1 消息不受影响（v1 连接不走联合，text 多模态走老路径）；**v2 发 list-content text → CONTRACT_VALIDATION**（text 收紧为 str）；`V2TextMessage` 语义回归（既有 `TestV2AckIdempotency` 等全绿）。

### 3.3 9B-3：协议版本协商显式拒绝不兼容版本

**现状**：协商在 `ws.prepare` 之后（`ws_handler.py:126-131`），`"2.0"`/未来版本/乱写 → 静默降级 v1；传输 token（`"2"`）与展示版本（`server_info.protocol_version="2.0"`，`ws_message.py:292`）命名易混淆。

**设计**：

1. **版本 token 判定**：`X-Protocol-Version` 支持集合 **{缺省/空 → v1, `"1"` → v1, `"2"` → v2}**；**其余显式值**（`"2.0"`/`"3"`/乱写）→ 显式拒绝。显式 `"1"` **必须保留合法 v1**——现网 v1 客户端与 `test_v2_ws.py:450` 正以 `"1"` 头驱动 legacy STT 路径。
2. **拒绝时机与形状**：检查提前到**升级前**——`server.py` 的 `ws_handler` wrapper（`:333-352`，`handle_ws` 调用前）或 `handle_ws` 内 `ws.prepare` 之前，返回 HTTP 400 + `ProtocolError`（`_protocol_error_response`，`server.py:105-119`；复用 auth 401 / CONNECTION_LIMITED 503 的升级前拒绝先例）。
3. **token vs 展示分离**：传输 token 只接受 `"1"`/`"2"`（+缺省）；`server_info.protocol_version` 展示值 `"2.0"` 不变。协议文档（`docs/electron-backend-protocol-v2.md`）明确二者语义 + 支持集合 `{缺省→v1, "1"→v1, "2"→v2}`。
4. **platform 头**：版本拒绝优先于 platform 解析（未知版本不接受 `X-Client-Platform`）。
5. **echo `"2.0"` 客户端缓解（F7）**：`server_info.protocol_version` 展示值 `"2.0"` 是客户端重连 echo 的自然来源，收紧后会从「静默降级 v1 可用」变为 400 锁死。缓解：拒绝响应的 `ProtocolError.message` 明示支持集合（`"1"`/`"2"`/缺省）；产物头 `transportVersion: "2"` 提示（本批不达 Electron 仓库，协议文档是主要防线）；Electron 侧按协议文档用 `"2"` 握手。此收紧是 todo 明确要求（拒绝不兼容版本），风险与缓解记录见 §8。

**契约变更**：无 wire 变化（拒绝在握手层）。
**涉及文件**：`core/input/server.py`、`core/input/ws_handler.py`、`docs/electron-backend-protocol-v2.md`。
**测试**：`"2"` → v2（既有绿）；缺省头 → v1（既有绿）；**`"1"` → v1（既有 `test_v2_ws.py:450` 保持绿）**；`"2.0"` → 400 ProtocolError；`"3"` → 400；乱拼写 → 400；Electron platform + `"2"` 组合（既有 `test_desktop_ws.py` 绿）。

## 4. Batch 9C — 语音联调

### 4.1 9C-1：固定并可验证客户端输入音频契约

**现状**：三种 start 帧（`VoiceRecordStartMessage:728-731` / `VoiceSessionStartMessage:756-760` / `DictationStartMessage:783-785`）无 descriptor；BINARY 恒按 16kHz/mono/pcm_s16le 处理（`audio_buffer.py` `_FRAME_BYTES=640`、20ms）；`voice_sessions.py` 建 `AudioBuffer` 未传 sample_rate（默认 16000）；非整帧尾部被 VAD 忽略；错误格式只被 VAD 判非语音**静默吞掉**，无 `CONTRACT_VALIDATION`。唯一硬限 aiohttp 16MB 帧 + RECORDING 累计 `MAX_RECORDING_BYTES`。

**设计（D3 固定格式 + descriptor）**：

1. **契约**：新增 `AudioInputDescriptor`（`codec: str`、`sampleRate: int`、`channels: int`）。三种 start 帧各增可选字段 `audio: Optional[AudioInputDescriptor]`。**不含 `chunkDurationMs`**——服务端帧长固定 20ms，客户端 chunk 时长无服务端用途，声明反而引入无校验无消费的字段（F8）。
2. **固定格式**：唯一支持 = `codec="pcm_s16le"` + `sampleRate=16000` + `channels=1`。start 带 descriptor 且不符 → `CONTRACT_VALIDATION`（`_send_voice_error`），不建会话。descriptor 缺省 → 按默认 16k/mono/pcm_s16le 处理（**向后兼容**旧客户端/旧 Vue）。
3. **BINARY chunk 校验**（`ws_handler.py:321-329` 喂 voice_mgr 前 + `voice_sessions.feed_pcm:450` 入口）：
   - **feed 不强制整帧倍数**：chunk 可为任意长度（REALTIME 网络分包任意），VAD 只扫整帧（`_FRAME_BYTES=640`）、非整帧尾部不进语音判定——既有行为，保留。
   - **段/WAV 边界整帧对齐（所有模式）**：`_process_segment`（audio_buffer.py:265-266 join 任意长度 `_chunks`）与 RECORDING stop 的 `pcm_to_wav(b"".join(self._rec_pcm))` 均在构造 WAV 前显式丢弃非整帧尾部（`audio[: len // 640 * 640]`）。REALTIME/DICTATION 段以网络帧边界结束（含部分尾部）——**VAD 检测只扫整帧 ≠ 段数据整帧对齐**，须截断。文档明确「固定 16k/mono/pcm_s16le + 段边界整帧对齐」。
   - per-chunk 上限：新增 `MAX_CHUNK_BYTES`（定 1 MiB），超限 → `CONTRACT_VALIDATION` + 会话终止（防背压/内存）。
   - 校验失败返回稳定错误，**不静默吞**（格式错误由 descriptor 校验 + chunk 上限兜底，不依赖 VAD 判噪）。
4. **descriptor 与 VAD 一致性**：descriptor 只在 start 时校验一次（16k/mono 与硬编码 VAD 一致）；非法 descriptor 不落 buffer。后端 STT（SenseVoice）不关心采样率，校验放 BINARY/feed_pcm 入口（0.3 已注明）。

**契约变更**：`AudioInputDescriptor` 新类型 + 三种 start 帧 `audio?` 字段。
**涉及文件**：`frontend_contract/ws_message.py`、`core/input/voice_sessions.py`、`core/input/ws_handler.py`、`core/input/audio_buffer.py`、`frontend/src/types/contract.generated.ts`、`docs/electron-backend-protocol-v2.md`（固定输入格式 + 尾帧策略 + chunk 上限）。
**测试**（todo DoD）：
- 真实 PCM fixture：新增 `core/input/tests/fixtures/` 16k/mono/pcm_s16le fixture，覆盖 recording/realtime/dictation 全流程。
- 错误格式：descriptor 不符（48k / stereo / opus）→ CONTRACT_VALIDATION。
- 非整帧尾部：stop 残余 < 640B → 丢弃不报错、STT 文本不异常；**任意长度 chunk 喂入（含非 640 倍数）不报错**（feed 不强制整帧，VAD 只扫整帧）；**REALTIME/DICTATION 段 WAV 同样整帧对齐**（网络帧尾部被截断，STT 不收到残余字节）。
- 超大 chunk（> MAX_CHUNK_BYTES）→ CONTRACT_VALIDATION + 会话终止。
- 缺省 descriptor → 默认格式正常（向后兼容）。

### 4.2 9C-2：Dictation 使用会话内 STT profile（不修改全局活动 STT）

**现状**：`DictationStartMessage`（`:783-785`）仅 `clientRequestId`；dictation 转写直接读全局 `agent.stt`（`voice_sessions._transcribe_no_emotion:126-135`）；Electron 的 `sttProfileId` 只能先 `switch_stt` 全局切换（`server.py:196-252`，改 `agent.stt` + `service_state.stt_model`，影响所有连接）。已实现隔离仅限情绪副作用（D6 `_transcribe_no_emotion` 不写共享 `stt_emotion_state`）。

**设计**：

1. **契约**：`DictationStartMessage` 增加可选 `sttProfileId: Optional[str]`（`<provider>/<model>`，与 `switch_stt` 一致）。
2. **解析与快照**（`voice_sessions.start_dictation`）：
   - `sttProfileId` 指定 → 解析 profile：custom provider 经 `resolve_model_params`（`custom_providers.py:151`）补 base_url/api_key → `STTFactory.create(profile_id, **params)` 建**独立 backend 实例**，快照到 dictation session（`self._dictation_stt`）。`runtime != "backend"`（desktop/local，Electron 执行）→ 拒绝 `STT_PROFILE_NOT_FOUND`（后端无可执行实例）。
   - 缺省 → 用全局 `agent.stt`（现状）。
   - **不修改** `agent.stt` / `agent.stt_enabled` / `service_state`。
3. **转写**：`_transcribe_no_emotion` 优先用 `self._dictation_stt`（无则 `agent.stt`），保持不写共享情绪状态。
4. **profile 不可用错误**：profile 不存在 / provider 非法 / 建后端失败 → `_emit_error`（code `STT_PROFILE_NOT_FOUND`）+ 不进入 DICTATION 态。
5. **资源释放**：本会话新建实例在 `_teardown_session()`（`voice_sessions.py:160`）释放——`STTBackend.close()` 是 `async`（`Module/stt/protocol.py:62`），而 `_teardown_session` 是**同步方法**且被 ~8 个调用点调用（stop_session/cancel_session/stop_dictation/cancel_dictation/notify_stt_disabled/idle 超时/连接断开 close），**不可直接 await**；用 `asyncio.ensure_future(self._dictation_stt.close())` fire-and-forget 异步关闭，**照抄 `llm_adapter.py:194-198` 的 try/except RuntimeError + warning 守卫**（无 running loop 时降级告警不裸抛；close 异常不外泄为 unretrieved task exception），不阻塞状态机。全局 `agent.stt` 不 close。
6. **并发与中途切换**：每个 dictation session 持有自己的 backend 快照，全局 `switch_stt` 不影响已开始会话；`_stt_available` 门控在 profile 路径独立判定（不依赖 `agent.stt`）。

**契约变更**：`DictationStartMessage` + `sttProfileId?`。
**涉及文件**：`frontend_contract/ws_message.py`、`core/input/voice_sessions.py`、`core/input/ws_handler.py`、`core/input/custom_providers.py`（复用 `resolve_model_params`）、`Module/stt/registry.py`（复用 `STTFactory.create`）、`frontend/src/types/contract.generated.ts`、`docs/electron-backend-protocol-v2.md`。
**测试**（todo DoD）：
- `sttProfileId` 指定 → 用独立 backend 转写；全局 `agent.stt` 未被替换、`service_state.stt_model` 不变。
- profile 不存在 / runtime=desktop → `STT_PROFILE_NOT_FOUND`，不进入 DICTATION。
- 中途全局 `switch_stt` → 已开始 dictation 继续用快照 backend。
- 并发两个 dictation（不同 profile）互不影响。
- stop/cancel → 新建 backend 被 `close()`（mock 断言）。

## 5. 契约变更清单（wire 汇总）

| 位置 | 变更 | 影响 |
|------|------|------|
| `SessionMessage` | + `conversationId: str`（必填） | 旧客户端忽略；v1 亦发 |
| `MessagesPageResponse` | + `cursor: Optional[str]` | 有更多页时返回 |
| `before`/`after` 参数 | 语义收敛为仅 opaque cursor | `messageId` 形式 → 400 |
| `AudioInputDescriptor` | 新增类型（codec/sampleRate/channels） | — |
| `VoiceRecordStartMessage`/`VoiceSessionStartMessage`/`DictationStartMessage` | + `audio: Optional[AudioInputDescriptor]` | 缺省向后兼容 |
| `DictationStartMessage` | + `sttProfileId: Optional[str]` | 缺省用全局 STT |
| `ClientMessage` 判别联合 | 新增（**不含 `TextMessage`**，`type="text"` 由 `V2TextMessage` 独占） | 不改字段；v2 入口校验；v2 text content 恒 `str` |
| 握手层 | 支持集合 {缺省/`"1"`→v1, `"2"`→v2}；`"2.0"`/未知版本 → 400 ProtocolError | 无 wire 变化 |
| 新产物 | `contract.schema.json` / `contract.raw.ts` / golden frames | 漂移门禁覆盖 |

改契约必须跑 `make gen-types` + `make check-types`（CI 强制）。

## 6. 测试计划

- **契约层**：`frontend_contract/tests/`——新增会话 conversationId、分页 cursor、音频 descriptor、dictation sttProfileId、ClientMessage 判别联合、`--check` 漂移、JSON Schema 有效。
- **路由/仓库层**：`core/input/tests/`——分页 cursor 严格区分（非法/两页/删锚点）、未知会话拒绝三入口、版本协商 400、BINARY chunk 校验、dictation profile。
- **集成**：`make test-quick`（unit+service）全绿；`make test-normal`（排除 slow）全绿。
- **现有回归**：`TestConversationIdRouting`、`TestV2AckIdempotency`、`TestV2Reconnect`、`test_version.py`、`test_desktop_ws.py`、`test_voice_ws.py`、`test_voice_sessions.py` 全部保持绿。
- **门禁**：`make lint`、`make pyright`（Elysia env 全仓）、`make check-types` 全绿。

## 7. 文档更新

- `docs/electron-backend-protocol-v2.md`：分页 `before`/`after` 只认 cursor（更正 `before={messageId}` 误写）；输入音频固定格式 + descriptor + 尾帧 + chunk 上限；`dictation.start.sttProfileId`；版本协商支持集合 + token/展示版本语义。
- `docs/archive/frontend-handoff-v2/traces/`：由契约源重新生成 golden frames，标注非事实源。
- `frontend_contract/README.md`：新产物（schema.json/raw.ts/frames）+ 漂移门禁说明。
- `README.md`：契约机制一节补充机器契约产物。
- `todo.md`：本批 8 项核对勾选 + 产品化 defer 两项保留；flaky 集成测试说明不变。
- `CLAUDE.md`：`core.input` 目录说明同步（session_routes/voice_sessions 能力）；`frontend_contract` 层说明同步新产物。

## 8. 风险与取舍

- **9B-2 重构面大**：`handle_json_message` 300+ 行分支改模型分发，回归风险最高——用既有 `TestV2AckIdempotency`/`TestVoiceWs`/`TestQueueFullAck` 作护栏；v1 路径零改动。
- **9A-2 严格区分**：Electron 文档曾误写 `before={messageId}`，接入方须改用返回 cursor；协议文档更新 + 400 显式报错兜底（接口级反馈，不静默）。
- **9C-2 profile**：`STTFactory.create` 对 custom 无 base_url 抛 `ValueError`（`registry.py:58-59`）——profile 解析必须先 `resolve_model_params` 补齐再建后端；`runtime=desktop/local` 的 custom profile 拒绝（后端无执行实例）。
- **9B-1 frames 从契约源生成**：放弃「连真实服务 capture」方案，改为契约模型直接序列化——可测试、可漂移检测、不依赖外部；副作用是 frames 不含真实运行时字段（如动态 build_sha），文档标注即可。
- **descriptor 缺省兼容**：旧客户端不传 `audio` 仍按默认格式工作；传了但格式不符才拒绝——不破坏存量，只对新接入的 Electron 提供确定性。
- **9B-3 收紧是硬行为变化（F7）**：echo `server_info.protocol_version="2.0"` 作握手头的存量客户端会从「静默降级 v1」变为「400 锁死」。缓解已定（3.3-5：拒绝 message 明示支持集合 + 产物 transportVersion 提示 + 协议文档），但产物本批不达 Electron 仓库——协议文档是上线前主要防线；任何部署中已 echo "2.0" 的 Electron 构建需在升级后改握手头。此属 todo 明确要求的协议收紧，非缺陷。

## 9. code-review 修复记录（2026-08-09，spec 初稿 + 复验两轮 review 后）

> 初稿经 code-review（8 angles，effort high）审出 7 项，均已在正文修复。此处留档供最终报告引用。

| # | finding（review 原文要点） | 定案 | 正文落点 |
|---|---------------------------|------|----------|
| R1 | `ClientMessage` 判别联合无法构造：`TextMessage` 与 `V2TextMessage` 的 discriminator 同为 `"text"`，pydantic 2.13 定义即抛 `TypeError`（实测） | 判别联合**不含 `TextMessage`**（v1 专属不走联合）；`type="text"` 由 `V2TextMessage` 独占；v2 text content 收紧为 `str`（媒体走 attachments） | 0.2 / 3.2 / 5 |
| R2 | 9C-2 把 async `STTBackend.close()` 挂到同步 `_teardown_session()`，无法 await（~8 调用点） | `asyncio.ensure_future(close())` fire-and-forget（对齐 TTS/LLM adapter 既有模式） | 4.2-5 |
| R3 | 9B-1 产物 `protocolVersion: "2.0"` 与 9B-3 拒绝显式 `"2.0"` 握手自相矛盾（Electron 照抄即 400） | 产物头带 `transportVersion: "2"`（握手可用值）+ `protocolVersion: "2.0"`（仅展示，注明不可用于握手头） | 3.1-3 |
| R4 | `query_page`/`query_after` 改 3 元组破 5 处既有调用点（`session_routes.py:309/362/461/562`、`attachment_release.py:91`） | **repo 签名不变**；cursor 由 handler 从 raw Message 边界算 | 2.2-3 |
| R5 | 版本协商「其余显式值一律拒绝」会把显式 `"1"` 从合法 v1 打成 400（`test_v2_ws.py:450` 回归） | 支持集合 **{缺省→v1, `"1"`→v1, `"2"`→v2}**；只拒 `"2.0"`/`"3"`/乱写 | 0.2 / 3.3 |
| R6 | 9C-1「chunk 须 640 整数倍」与「stop 丢弃残余尾帧」互斥 | feed 不强制整帧对齐（任意长度），**段/WAV 边界丢弃非整帧尾部**；chunk 只校验 ≤ `MAX_CHUNK_BYTES` + descriptor 格式 | 4.1-3 |
| R7 | cursor 若从契约转换后页面取边界 `_id`，单条契约化失败丢弃 → 游标偏移、下一页重复返回 | cursor 从**契约转换前** raw 查询结果取 `messages[-1].metadata.get("_mongo_id")`（`core.schema.Message` 的 Mongo id 在 metadata） | 2.2-4 |
| R8 | `core.schema.Message` 无 `_id` 属性（Mongo id 在 `metadata['_mongo_id']`，`_id` 是 rest 契约视图的 wire 别名） | cursor 取 `messages[-1].metadata.get("_mongo_id")`；涉及文件说明去 `Message._id` 混淆 | 2.2-4 |
| R9 | `query_after` 按 `message_id` 锚点，24-hex cursor 永不匹配 → after 方向静默空页 | **`query_after` 锚点改为 `_id`**（`{session_id, "_id": {"$gt": oid}}`）；`message_repo.py` 非零改动（仅 query_after） | 2.2-3 |
| R10 | 空页 `messages[-1]` → IndexError（删除锚点场景） | cursor 空页守卫：`messages and has_more` 才取，否则 None | 2.2-4 |
| R11 | 「REALTIME VAD 分段只产出整帧」为假（`_process_segment` join 任意长度 `_chunks`） | 段/WAV 边界整帧对齐**扩到所有模式**（RECORDING stop + `_process_segment` 均截断至 640 整帧） | 4.1-3 |
| R12 | `capture_ws_trace.py` 与 `gen_contract_frames.py` 双写同一 traces 目录互相覆盖 → 漂移误报 | **停用 capture_ws_trace.py**（标记 deprecated）；frames 唯一权威 = 契约源生成 | 3.1-4 |
| R13 | 裸 `ensure_future(close())` 缺 llm_adapter 的 try/except RuntimeError 守卫 | 照抄 `llm_adapter.py:194-198` 守卫（无 loop 降级告警、任务异常不外泄） | 4.2-5 |
| R14 | echo `server_info.protocol_version="2.0"` 的客户端收紧后 400 锁死；产物缓解本批不达 Electron | 拒绝 message 明示支持集合 + 产物 `transportVersion` 提示 + 协议文档；风险记录 §8 | 3.3-5 |
| R15 | `chunkDurationMs` 无校验无消费 | **去掉该字段**（descriptor 只留 codec/sampleRate/channels，固定 20ms 帧客户端无需声明） | 4.1-1 |
