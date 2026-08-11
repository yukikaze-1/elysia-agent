# Electron v2 后端整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `docs/review/electron-v2-first-integration-backend-review-2026-08-11.md` 全部 P0×2、P1×5、P2×3，使后端通过 D6c 冻结门禁。

**Architecture:** 设计见 `docs/superpowers/specs/2026-08-11-electron-v2-backend-hardening-design.md`（本计划唯一设计依据，含 14 项决策记录）。按 review 批次序执行：批次 A（幂等正确性）→ 批次 B（联调契约）→ 批次 C（运维）+ 门禁。任务间通过明确接口衔接。

**Tech Stack:** Python 3.12 + aiohttp + Motor + Pydantic（frontend_contract）+ pytest（`@pytest.mark.asyncio`，marker：契约→unit，其余→service）。

## Global Constraints

- 执行/测试用 `conda run -n Elysia python ...`；提交前 `make lint` + `make pyright`（Elysia env）+ `make check-types`。
- pytest strict：async 测试 `async def` + `@pytest.mark.asyncio`；每个新测试函数/类必须有 marker（unit/service）。
- 新增测试全部 unit/service（fake repo 故障注入，不依赖真实 Mongo/Milvus）。
- 只改各 Task Files 列出文件；勿碰 `frontend/` 源码。`frontend_contract` 产物（generated.ts/schema.json/raw.ts）由生成器产出，不经手编辑。
- **状态常量**收敛到 `core/storage/idempotency_repo.py`：`STATUS_CLAIMED="claimed"`、`STATUS_ACCEPTED="accepted"`、`STATUS_REJECTED="rejected"`；legacy `"in_progress"/"completed"` 在 `IdempotencyEntry.from_doc` 防御映射。
- **`claimed_at` 用墙钟**（`time.time()`，unix 秒）——跨重启可比、与 Mongo 其他时间字段同基；`_is_stale` 也须用 `time.time()`（**勿用 `time.monotonic()`**——单调钟与墙钟相减为巨大负数，stale 判定永不触发）。
- **`GRACE_SECONDS = 30.0`**（`core/input/idempotency.py`）；**`_IN_PROGRESS_RETRY_AFTER_MS = 30000`**（= int(GRACE_SECONDS*1000)，IN_PROGRESS 拒绝的 retryAfterMs 值，Task 4 与 Task 6 golden 帧共用同一常量，防帧值漂移）。
- **store 返回类型定死 `StoreResult{entry, acquired}`**（无「二选一」）；`ClaimResult`（repo 层）与 `StoreResult`（store 层）语义一致但分层独立。
- 任务顺序强依赖：**Task 2 紧跟 Task 1、Task 4 紧跟 Task 2/3 完成**。Task 1 改动 `claim()` 签名/返回类型后，`idempotency.py` 的 `store()` 消费点仍在 Task 2 才迁移；Task 2 改动 `store()` 返回类型后，ws_handler 消费点在 Task 4 才迁移。**期间不运行 `test_v2_ws.py` 与 store 层用例（`test_idempotency_durable.py` 中 TestDurable/TestDevDegradation 等）**——存在已知瞬态（Task 1→2：`store()` 调旧 `claim()` 签名；Task 2→4：`StoreResult` 无 `user_message_id`）。全量门禁只在 Task 12。

---

## 批次 A：幂等正确性

### Task 1: `_call(non_fatal)` + `is_connected` + `ClaimResult` + `claim()` 重构 + repo 写入负载扩展

**Files:**
- Modify: `core/storage/base.py:123-156`（`_call` 加 `non_fatal`）+ 基类加 `is_connected` property
- Modify: `core/storage/idempotency_repo.py`（`_is_duplicate_key` 扩展 + `ClaimResult` + `claim()` 重构 + `claimed_at` + `complete()` 加负载 + 新增 `reject()`）
- Modify: `core/storage/tests/test_idempotency_repo.py`（**既有测试适配——review 复核确认 `test_claim_new_returns_none`/`test_claim_existing_returns_existing`/`test_claim_scoped_by_conversation` 断言 `claim()` 返回 None/dict，须改断言 `ClaimResult`**）

**Interfaces:**
- Produces: `ClaimResult{entry: dict, acquired: bool}`；`Repository.is_connected`（=`_db is not None`）；`Repository._call(coro_factory, *, reason, non_fatal=None)`；`IdempotencyRepository.claim(conversation_id, client_request_id, *, content_digest, message_id, status, claimed_at, expires_at) -> ClaimResult`；`complete(conversation_id, client_request_id, *, status=STATUS_ACCEPTED, payload: dict | None = None)`（payload 存 received_at/client_message_id/conversation_id_wire/error）；`reject(conversation_id, client_request_id, *, error: dict)`（`$set` status=STATUS_REJECTED + error 负载）；doc schema 加 `claimed_at`。

- [ ] **Step 1: 写失败测试**（`core/input/tests/test_idempotency_durable.py` 追加；FakeDb 实现 `find/insert_one/update_one/ensure_index/close`，可注入 DuplicateKeyError 与可用性翻转）

```python
def test_claim_returns_acquired_flag_for_new():
    """新 claim → acquired=True。"""
    repo = IdempotencyRepository(db=FakeDb())
    result = run_async(repo.claim("c1", "r1", content_digest="d", message_id="m",
                                  status=STATUS_CLAIMED, claimed_at=time.time(),
                                  expires_at=...))
    assert result.acquired is True

def test_claim_loser_acquired_false():
    """并发同键 → 后写者 acquired=False 且 entry=winner。"""
    # FakeDb：第一次 insert 抛 DuplicateKeyError，find 返回既有 doc

def test_duplicate_key_does_not_invalidate_repo():
    """DuplicateKey 业务冲突不判死：health_check 仍 true、下一条可用。"""
    # 先 duplicate 后成功；断言 _db 未被置 None

def test_claim_stale_claimed_takeover():
    """过期 CLAIMED（claimed_at < now-GRACE_SECONDS）→ 接管：acquired=True 且 timestamps 已延长。"""
    # FakeDb 返回 status=claimed、claimed_at=now-60 的既有 doc；断言 acquired=True + update_one 收到 $set claimed_at/expires_at

def test_complete_reject_persist_payload():
    """complete() 存 received_at/client_message_id/conversation_id_wire；reject() 存 error。"""
    # FakeDb 记录 update_one 的 $set 内容，断言 payload/error 字段已落

def test_is_connected_reflects_db():
    """is_connected：_db 非空 True；close() 后 False。"""
```

同步更新 `core/storage/tests/test_idempotency_repo.py` 三个既有断言（`r is None` → `r.acquired is True` 等；STATUS_* import 改名）。

- [ ] **Step 2: 运行验证失败**：`conda run -n Elysia python -m pytest core/input/tests/test_idempotency_durable.py core/storage/tests/test_idempotency_repo.py -q -k "claim or is_connected or persist" -x --tb=short` → FAIL。
- [ ] **Step 3: 实现**
  - `base.py`：`_call` 加 `non_fatal: Callable[[BaseException], bool] | None = None`；`except Exception as e:` 分支先 `if non_fatal is not None and non_fatal(e): raise`（裸抛原始异常，不判死不包装）；基类加 `@property is_connected -> bool: return self._db is not None`。
  - `idempotency_repo.py`：`_is_duplicate_key` 同时查 `type(exc).__name__` 与 `type(exc.__cause__).__name__`；`@dataclass ClaimResult`；`claim()` 加 `claimed_at: float`，doc 加 `"claimed_at"`；find-hit 分支（**stale 用 `time.time()`**）：
    ```python
    if existing:
        rec = existing[0]
        # legacy "in_progress" 与 STATUS_CLAIMED 一视同仁（防御映射同源——from_doc 映射在
        # Task 2，但 claim() 读原始 doc，此处须显式归一，否则历史 CLAIMED 永不接管）
        status = "claimed" if rec.get("status") == "in_progress" else rec.get("status")
        if status == STATUS_CLAIMED and _is_stale(rec.get("claimed_at", 0)):
            await self._call(lambda: db.update_one(COLLECTION, filter_expr,
                {"$set": {"claimed_at": claimed_at, "expires_at": expires_at}}),
                reason="IdempotencyRepository.claim 接管更新失败")
            rec = {**rec, "claimed_at": claimed_at, "expires_at": expires_at}
            return ClaimResult(entry=rec, acquired=True)
        return ClaimResult(entry=rec, acquired=False)
    ```
    `_is_stale(ca) = time.time() - ca > _GRACE`（`_GRACE` 从 `core.input.idempotency` import `GRACE_SECONDS`，**只允许墙钟相减**）。insert 用 `_call(..., non_fatal=_is_duplicate_key)`，except 覆盖原始异常（`_is_duplicate_key(e)` → 读回 winner → acquired=False；否则 raise）。
  - `complete()` 签名加 `payload: dict | None = None`（`$set` status + `**payload`）；新增 `reject(conversation_id, client_request_id, *, error: dict)`（`$set` `{status: STATUS_REJECTED, "error": error}`）。
- [ ] **Step 4: 验证通过**：Step 2 命令全绿。
- [ ] **Step 5: 提交**：`git commit -m "feat(storage): claim ownership result, non-fatal DuplicateKey, is_connected, payload writes"`

---

### Task 2: `IdempotencyEntry` 三态 + `IdempotencyStore` 内存权威 overlay

**Files:**
- Modify: `core/input/idempotency.py`（状态三态 + 完整 ACK 负载字段 + lookup/store/recover/complete/reject/delete/durable/configure + `GRACE_SECONDS`）
- Modify: `core/input/tests/test_idempotency_durable.py`（追加 store 层用例）

**Interfaces:**
- Consumes: Task 1 `ClaimResult`/`STATUS_*`/`repo.complete(payload)`/`repo.reject(error)`/`is_connected`。
- Produces: **`StoreResult{entry: IdempotencyEntry, acquired: bool}`**；`IdempotencyEntry` 增 `status/claimed_at/expires_at/received_at/client_message_id/conversation_id_wire/error`；`store.lookup/store/recover/complete/reject/delete`；`store.durable`；`store.configure(repo, product_mode)`（初值 `_in_memory_fallback = repo is None or not repo.is_connected`）。**签名定死**：
  - `store.complete(conversation_id, client_request_id, *, received_at, client_message_id, conversation_id_wire)`；
  - `store.reject(conversation_id, client_request_id, *, code, message, retryable, received_at, client_message_id, conversation_id_wire, details=None, retry_after_ms=None)`；
  - `store.recover()`（成功 reconcile 后置 `_in_memory_fallback = False`）。

- [ ] **Step 1: 写失败测试**（`core/input/tests/test_idempotency_durable.py` 追加；Fake repo 实现 `claim/get/complete/reject/release/health_check/is_connected`，`available` 布尔可翻转）

```python
def test_lookup_memory_first_even_when_mongo_ok():
    """内存有 entry 时 Mongo 在线也命中内存（不覆盖内存事实）。"""
def test_store_returns_store_result_and_writes_shadow():
    """store 成功写穿 Mongo 也写内存；返回 StoreResult；durable=True。"""
def test_failover_up_down_up_no_dedup_loss():
    """up→down→up：同 clientRequestId 重试恒同一 entry（process 不重复）。"""
def test_recover_reconciles_memory_to_mongo_and_durable():
    """recover()：Mongo 已有→内存刷新为其状态；Mongo 无→插入内存 claim；结束后 _in_memory_fallback=False → durable=True。"""
def test_durable_false_before_connect():
    """repo 未连接（is_connected=False）→ configure 后 durable=False（初值不误报）。"""
def test_complete_reject_persist_full_ack_payload():
    """complete/reject 持久化 received_at+client_message_id+conversation_id_wire(+error)；重放可逐字重建。"""
def test_pure_memory_path_keeps_memory_first_dedup():
    """repo=None：store 同键二次返回既有 entry（acquired=False），不生成新 message_id。"""
```

- [ ] **Step 2: 验证失败**：`conda run -n Elysia python -m pytest core/input/tests/test_idempotency_durable.py -q -k "overlay or recover or durable or payload or memory_first" -x --tb=short` → FAIL。
- [ ] **Step 3: 实现**
  - `IdempotencyEntry` 字段扩展（dataclass，默认值兼容既有构造点）；`from_doc` 防御映射 legacy `in_progress→claimed`/`completed→accepted`，读取 `received_at/client_message_id/conversation_id_wire/error`。
  - `lookup()`：内存优先（`_memory_lookup` TTL 有效即返回）→ miss 才 `repo.get`；repo 报错：product 透传 / dev 视为 miss。
  - `store()` → 返回 `StoreResult`：**repo 非空** → 恒 `repo.claim`（acquired 语义来自 Task 1）；acquired=True 写内存返回 `(entry, True)`；acquired=False 把 winner 写内存返回 `(winner, False)`；repo 报错：product 透传 / dev 内存降级（内存已有 → `(mem, False)`，否则插入 → `(entry, True)`）。**repo=None 纯内存路径：保留内存优先去重**（同键二次返回既有 `(entry, False)`，不生成新 message_id——Task 2 关键，防 dev/无 Mongo 双生成）。
  - `recover()`：对每条内存 entry，`repo.get` miss → 插入（claim），命中 → 内存刷新为 Mongo 状态；结束后 `self._in_memory_fallback = False`（durable 翻回——否则 Task 5 断言不成立）。
  - `complete()/reject()`：写内存（status + 完整负载）+ best-effort `repo.complete(payload)`/`repo.reject(error)`（失败仅记日志，不抛）。`delete()` 保留（release，非 ACK 放弃路径）。
  - `configure()`：`self._in_memory_fallback = repo is None or not repo.is_connected`；`durable = repo is not None and not self._in_memory_fallback`。
  - **瞬态声明**：本 Task 后 ws_handler 消费 `store.store` 返回类型变化，`test_v2_ws.py` 暂不运行（Task 4 迁移）。
- [ ] **Step 4: 验证通过**：Step 2 + 既有 `test_idempotency_durable.py` 全量（不含 `test_v2_ws.py`）。
- [ ] **Step 5: 提交**：`git commit -m "feat(idempotency): memory-authoritative overlay, StoreResult, 3-state payload entries, recover()"`

---

### Task 3: `GenerationRegistry.find_by_user_message`

**Files:**
- Modify: `core/cognitive/generation_registry.py` + `core/storage/generation_repo.py`

**Interfaces:**
- Produces: `GenerationRegistry.find_by_user_message(user_message_id: MessageId) -> dict | None`（Task 4 证据检查用；Mongo 离线/异常 → None = 无证据）。

- [ ] **Step 1: 失败测试**（生成 registry 既有测试文件追加）：注册后按 `user_message_id` 命中；无则 None；repo 抛异常 → 返回 None。
- [ ] **Step 2: 验证失败**。
- [ ] **Step 3: 实现**：`generation_repo.py` 加 `find_by_user_message(user_message_id)`（`find_one({"user_message_id": ...})`，对齐既有 `find_by_message` :175 风格）；registry 薄封装（try/except 记日志，异常返回 None）。
- [ ] **Step 4: 验证通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat(generation): find_by_user_message for idempotency commit evidence"`

---

### Task 4: ws_handler 三路径 claim loser 分支 + complete 前置 + reject + voice.commit 顺序

**Files:**
- Modify: `core/input/ws_handler.py`（`_route_v2_text` / 附件路径 / `handle_voice_commit` / `_send_ack` 签名）
- Modify: `core/input/tests/test_v2_ws.py`、`core/input/tests/test_idempotency_durable.py`（并发/重放/接管用例）

**Interfaces:**
- Consumes: Task 1/2 `StoreResult`、`store.reject/complete`（签名见 Task 2）、Task 3 `find_by_user_message`、`STATUS_*`、`_IN_PROGRESS_RETRY_AFTER_MS`。
- Produces: 统一命中处理 `_handle_idempotency_hit(...) -> "REPLAYED" | "REJECT" | "TAKEOVER"`；**`_send_ack(ws, message_id, client_request_id, client_message_id, conversation_id, status, error: dict | None = None, *, received_at: str | None = None)`**（received_at 缺省用 now_iso；重放传持久化原值）。**STORAGE_UNAVAILABLE 的 rejected ACK 在 error.details 携带 `idempotency_durable`（D3，=`get_store().durable`）**。

- [ ] **Step 1: 写失败测试**（`test_v2_ws.py` 追加，fake store + fake generation registry + fake session manager）：
  - 并发同键同内容：两个请求都发 accepted ACK（同 messageId），`process_message`/EventBus 只一次；
  - 并发同键异内容：一 accepted 一 `CONTRACT_VALIDATION`，正文与 messageId 不串线；
  - 同键 CLAIMED 新鲜 → rejected `IN_PROGRESS`（retryable + `retryAfterMs == _IN_PROGRESS_RETRY_AFTER_MS`）；
  - 同键 CLAIMED 过期 + 证据（generation 存在）→ 重放 accepted + `complete`；
  - 同键 CLAIMED 过期 + 无证据 → 接管重执行（process 一次）；
  - rejected 路径（QUEUE_FULL/INTERNAL/附件失败）→ `store.reject`（status=REJECTED）；同键重试重放原 rejected（**receivedAt 用持久化原值，逐字一致**）；
  - 附件 refs 失败 → reject + 附件预留回滚；
  - **voice.commit**：claim 前 reserve_work；acquired=False/claim 失败 → release_work + 对应 ACK（不消费录音）；commit_recording 失败 → reject + release_work；**TAKEOVER 保持既有 reserve_work 从 commit_recording 续跑（不重复 reserve）**；
  - STORAGE_UNAVAILABLE rejected ACK 的 error.details 含 `idempotency_durable`。
- [ ] **Step 2: 验证失败**（现状并发双 process、reject 走 delete）。
- [ ] **Step 3: 实现**
  - `_route_v2_text` / 附件路径：`lookup` → 命中 → `_handle_idempotency_hit`；miss → 预分配 message_id → `store.store(...)` → 按 `StoreResult.acquired` 分支：acquired → 原业务流；acquired=False → `_handle_idempotency_hit(result.entry)`。
  - `_handle_idempotency_hit`：按 §1.1/§1.2 规则（digest 不符 → REJECT CONTRACT_VALIDATION；ACCEPTED → REPLAY accepted；REJECTED → REPLAY rejected；CLAIMED 新鲜 → REJECT IN_PROGRESS；CLAIMED 过期 + 证据 → REPLAY accepted + complete；CLAIMED 过期 + 无证据 → **TAKEOVER**）。
  - **TAKEOVER 续跑点**：text/附件 → 再次 `store.store(...)`（Task 2 已移除内存短路，repo 对 stale CLAIMED 接管返回 acquired=True）→ acquired 则进入业务流；**voice → 保持已获 reserve_work，从 `commit_recording` 续跑**（不得重入顶部，防双重 reserve）。
  - accepted 路径：`process_message` 返回 True 后**立即 `await store.complete(key, received_at=..., client_message_id=..., conversation_id_wire=...)`，再 `_send_ack(accepted, received_at=same)`**（media 与纯文本一致；received_at 在 handler 一次性 `now_iso()` 生成，传给 complete 与 ack 同一值）。
  - 所有 rejected 路径 `store.delete` → `store.reject(key, code=..., message=..., retryable=..., received_at=..., client_message_id=..., conversation_id_wire=...)`（before `_send_ack(rejected, received_at=same)`）。
  - STORAGE_UNAVAILABLE 分支 error.details 加 `{"idempotency_durable": get_store().durable}`（D3）。
  - **voice.commit**：`reserve_work` → `store.store`（acquired=False → release_work + `_send_ack`(对应 rejected/重放) return）→ `commit_recording`（`VoiceSessionError` 且 claim 已获 → `store.reject` + release_work + `_send_ack` return）→ `agent.bus.post(...)`（**入队失败 → `store.reject` + release_work + `_send_ack` rejected**）→ `store.complete` → `_send_ack(accepted)`——**complete-before-ack 不变量对 voice 同样成立**（complete 在 ack 前，且 bus.post 在 complete 前保证「已入队」语义）。
  - `_send_ack` 加 `received_at` 参数（缺省 now_iso）；**error 参数保持 `dict | None`**（Task 6 改判别构造时 pydantic 自动 coerce dict→AckError，本 Task 不依赖 Task 6 模型）。
- [ ] **Step 4: 验证通过**：`test_v2_ws.py` + `test_idempotency_durable.py` 全绿。
- [ ] **Step 5: 提交**：`git commit -m "fix(ws): claim-loser branches, complete-before-ack, reject persistence, voice order"`

---

### Task 5: idempotency 生命周期接线

**Files:**
- Modify: `core/agent/_lifecycle_persistence.py`（health probes + `_recover_dependency` + `_stop_persistence`）
- Modify: `core/agent/tests/`（lifecycle 用例）或既有 lifecycle 测试文件

**Interfaces:**
- Consumes: Task 1 `repo.health_check`/`is_connected`、Task 2 `get_store().recover()`/`durable`。
- Produces: 健康循环含 idempotency 探测与恢复；shutdown close。（`configure_store` durable 初值 Task 2 已实现，本任务**不改** idempotency.py/capabilities.py。）

- [ ] **Step 1: 写失败测试**（fake repo `available` 翻转）：
  - Mongo 离线启动 → `durable=False`；
  - 不重启，翻转可用 → 一轮 health loop 后 `_recover_dependency("idempotency")` 被调 + `store.recover()` + `durable=True`；
  - 运行中（先健康）断开再恢复同样通过；
  - stop 后 `repo.close` 被调；两轮 start/stop 无悬挂。
- [ ] **Step 2: 验证失败**（现状 probes 无 idempotency）。
- [ ] **Step 3: 实现**
  - `_db_health_loop` probes 增 `"idempotency"`（`self.idempotency_repo.health_check`）；
  - `_recover_dependency("idempotency")`：`await repo.connect()` 成功 → `await get_store().recover()` → True；失败 False（下轮重试）；
  - `_stop_persistence`/`_stop_attachment_repo`：`await self.idempotency_repo.close()`（try/except）。
- [ ] **Step 4: 验证通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat(lifecycle): idempotency health probe, recovery reconcile, shutdown close"`

---

## 批次 B：联调契约

### Task 6: ACK 判别联合 + AckError + 生成器嵌套联合 + 负面 golden

**Files:**
- Modify: `frontend_contract/ws_message.py`（`AcceptedUserAckMessage`/`RejectedUserAckMessage`/`UserAckMessage` 命名联合/`AckError`/`ErrorMessage` 加 `clientRequestId`+顶层 `retryAfterMs`/6 命令消息加可选 `clientRequestId`）
- Modify: `frontend_contract/gen_schema.py`（成员判定递归化）
- Modify: `frontend_contract/__main__.py`（`_collect` 递归展开嵌套联合）
- Modify: `scripts/gen_contract_frames.py`（重构既有 accepted 帧构造器 + 新增 6 负面帧）
- Regenerate: `contract.generated.ts` / `contract.schema.json` / `contract.raw.ts` + 归档 golden traces（`docs/archive/frontend-handoff-v2/traces/*.jsonl`）
- Modify: `core/input/ws_handler.py` `_send_ack`（按 status 构造判别成员；`error` 参数类型放宽 `dict | AckError | None`）
- Modify: `frontend_contract/tests/`

**Interfaces:**
- Consumes: 无前置任务（契约独立）；Task 4 已把 `_send_ack` 的 error 传为 dict。
- Produces: 判别联合契约 + 生成器嵌套支持 + 6 负面 golden；`_send_ack` 判别构造（`ReceivedAt` 由调用方传入原值）。

- [ ] **Step 1: 写失败测试**（`frontend_contract/tests/`，unit）：
  - `TypeAdapter(UserAckMessage).validate_python(...)` 拒绝：accepted+空 messageId / accepted+error / rejected+非空 messageId / rejected+无 error；
  - `AcceptedUserAckMessage` 强制非空 messageId/clientRequestId；`RejectedUserAckMessage.messageId` 恒 `""`；`AckError` 顶层 `retryAfterMs`；
  - `gen_schema.build_document()` 不抛 TypeError（嵌套联合回归）；`__main__._collect()` 的 ServerMessage 成员含 Accepted/Rejected 具体类型（非 "Annotated"）；
  - `contract.generated.ts` 的 `ServerMessage` 引用 Accepted/Rejected 且无 `Annotated` 字面量。
- [ ] **Step 2: 验证失败**（生成器崩 / 模型不存在）。
- [ ] **Step 3: 实现**
  - 契约模型（§2.1 字段表）；`UserAckMessage = Annotated[Union[AcceptedUserAckMessage, RejectedUserAckMessage], Discriminator("status")]`；`ServerMessage` 的 ack 分支引用它；`AckError` 取代 `error: dict`；`ErrorMessage` 加 `clientRequestId`+`retryAfterMs`（均可选）；6 命令消息（switch_model/tool_approval/set_service/set_tts_voice/activate_session/wake_toggle）加可选 `clientRequestId`。
  - `gen_schema.build_models`：成员判定递归化（`_collectible(c)` = BaseModel 子类 **或** Annotated[Union]）；删 `all(issubclass(c, BaseModel))` 硬约束（pydantic `json_schema()` 原生产出嵌套 oneOf）。
  - `__main__._collect`：展开 members 时对 `get_origin(m) is Annotated` 的成员递归取内层 Union 成员 flat 放入 `discriminated[alias]`。**gen_ts 无需额外改动**——扁平化后成员为具体 BaseModel，gen_ts 从 `discriminated` dict 发射即可；验证产物无 "Annotated"。
  - `_send_ack`：按 status 构造 `AcceptedUserAckMessage`/`RejectedUserAckMessage`（`error` 经 `AckError.model_validate` 归一，兼容 dict 与 AckError 两种入参；receivedAt 用传入原值）。
  - `gen_contract_frames.py`：重构 4 个既有 accepted 帧构造器为判别成员（`AcceptedUserAckMessage`）；新增 6 负面帧（CONTRACT_VALIDATION/SESSION_NOT_FOUND/QUEUE_FULL/STORAGE_UNAVAILABLE/RATE_LIMITED/**IN_PROGRESS**，均 rejected + `messageId:""` + AckError，IN_PROGRESS 带 `retryAfterMs=_IN_PROGRESS_RETRY_AFTER_MS`——**与 Task 4 常量一致**）；`--check` 与归档 traces 对齐。
- [ ] **Step 4: 重新生成 + 验证**：`conda run -n Elysia python -m frontend_contract`；`python scripts/gen_contract_frames.py`；`conda run -n Elysia make check-types`；frontend_contract tests + golden `--check` 全绿。
- [ ] **Step 5: 提交**：`git commit -m "feat(contract): status-discriminated ack union, AckError, nested-union generators, 6 negative frames"`

---

### Task 7: WS 限流关联

**Files:**
- Modify: `core/input/rate_limiter.py`（`retry_after_ms`）
- Modify: `core/input/ws_handler.py`（限流块分通道 + `_extract_client_request_id`）
- Modify: `core/input/server.py`（WS 建连 429 加 `Retry-After` 头，`server.py:476-478`）
- Modify: `core/input/tests/test_rate_limit.py`、`core/input/tests/test_v2_ws.py`

**Interfaces:**
- Consumes: Task 6（`_send_ack` 判别 + `AckError`）。
- Produces: `RateLimiter.retry_after_ms(category, key) -> int`；限流块 text/voice.commit → rejected ACK（合法 clientRequestId 时）。

- [ ] **Step 1: 写失败测试**（service）：
  - `retry_after_ms` 对满桶返回 ≥1000 且有限值；
  - text 超限 → 收到 `user_message.ack` rejected RATE_LIMITED（原 clientRequestId + retryAfterMs），promise 立即 settle；
  - voice.commit 超限同样；两条并发只拒被限那条；
  - 其余命令（switch_model）超限 → `error` 带 clientRequestId + 顶层 retryAfterMs；
  - clientRequestId 缺失 → 退化 generic `error`；
  - WS 建连 429 带 `Retry-After` 头。
- [ ] **Step 2: 验证失败**。
- [ ] **Step 3: 实现**
  - `TokenBucket.retry_after_ms()`：`int(max(1.0, capacity - tokens) / refill_per_sec * 1000)` 下限 1000；`RateLimiter.retry_after_ms(category, key)` 无桶/未限流 → 0。
  - 限流块：`msg_type in ("text", "voice.commit")` 且 `_extract_client_request_id(data)` 有效 → `_send_ack(rejected, error=AckError{code=RATE_LIMITED, retryable=True, retryAfterMs})`；否则 generic `ErrorMessage`（+可选 clientRequestId + 顶层 retryAfterMs）。`_extract_client_request_id`：`data.get("clientRequestId")` 为 str 且 `0 < len ≤ 128` 才返回，否则 None。
  - WS 建连 429（`server.py:476-478`）加 `Retry-After: <seconds>`。
- [ ] **Step 4: 验证通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat(rate-limit): correlated rejected ack for text/voice, retryAfterMs, Retry-After on connect 429"`

---

### Task 8: clean-start 会话发现

**Files:**
- Modify: `core/storage/session_repo.py`（新增 `ensure_exists(session_id)`，`$setOnInsert`——**不可**用既有 `create()` 的 `$set messages:[]`，会清空既有历史）
- Modify: `core/memory/session_manager.py`（`__init__` eager default 建 summary + 触发 `ensure_default_persisted()`）
- Modify: `core/agent/_lifecycle_persistence.py`（`_recover_dependency("session")` 恢复边界调 `ensure_default_persisted()`——解决 repo 在 `__init__` 时未连接的问题）
- Modify: `core/input/session_routes.py`（确认 detail 兜底 updatedAt 非空）
- Modify: `core/input/tests/test_session_routes.py`（`test_list_empty` 更新 + 一致性用例）

**Interfaces:**
- Produces: `SessionRepository.ensure_exists(session_id) -> bool`；`SessionManager.ensure_default_persisted()`。

- [ ] **Step 1: 写失败测试**：
  - clean-start（空 Mongo + 空 local）→ `GET /api/sessions` 返回 `[default(active=true)]`；
  - `GET /api/sessions/default` detail `updatedAt` 非空可解析；
  - Mongo 离线/恢复/重启后三条接口（list/detail/WS 首帧 conversationId）仍一致；
  - 列表会话非空时恒恰一个 active；
  - **ensure_exists 不覆盖既有 `messages`**（预置 default 文档含 messages 后调 ensure_exists，断言 messages 未清空）；
  - `test_list_empty` 从 `sessions == []` 改为断言 default 存在且 active。
- [ ] **Step 2: 验证失败**（现状 list 空）。
- [ ] **Step 3: 实现**
  - `session_repo.py`：`ensure_exists(session_id)` = `update_one({session_id}, {"$setOnInsert": {session_id, messages: [], updated_at: now_iso()}}, upsert=True)`（返回是否新插入）。
  - `session_manager.py`：`__init__` eager default 处 `self._local_summaries[DEFAULT_SESSION] = SessionSummary(session_id=DEFAULT_SESSION, message_count=0, updated_at=now_iso(), title="")`；`ensure_default_persisted()` 调 `repo.ensure_exists(DEFAULT_SESSION)`（repo 可用时，失败仅记日志）。
  - `_lifecycle_persistence._recover_dependency("session")`：在既有恢复逻辑后追加 `ensure_default_persisted()`。
  - `_merged_summaries` 已按 `max(updated_at)` 合并（`:420`）——确认 default 新鲜本地 summary 胜出，补注释。
- [ ] **Step 4: 验证通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat(session): clean-start default discoverable, setOnInsert ensure"`

---

## 批次 C：运维与交接

### Task 9: 探针分层 /health /ready

**Files:**
- Modify: `core/input/server.py`（拆 `_liveness_payload`/`_readiness_payload` + 并行探测 + chatReady/fullReady）
- Modify: `core/input/tests/test_server.py`、**`core/input/tests/test_health.py`**（review 复核确认 `test_dep_false_health_degraded_ready_503` 断言 Milvus down→/ready 503、`test_health_response_fields` 断言 dependencies 精确集——均须按新语义更新）

**Interfaces:**
- Consumes: Task 5（/ready 含 idempotency ping）。
- Produces: `/health` 零网络探针；`/ready` HTTP 门禁=核心 chat（mongo 并行 ping + product idempotency ping）；payload 带 chatReady/fullReady/dependencies。

- [ ] **Step 1: 写失败测试**（service，fake agent 暴露 health_check 可翻转）：
  - 依赖全挂起 → `/health` 在很小预算（<0.5s）内返回 200；
  - Milvus down → `/ready` 200（chatReady=true, fullReady=false）+ payload 解释；
  - product + idempotency down → `/ready` 503；dev + idempotency down → `/ready` 200；
  - payload 含 `chatReady`/`fullReady`/`dependencies`（dependencies 现含 mongo/milvus/idempotency）。
  - 更新 `test_health.py`：`test_dep_false_health_degraded_ready_503`（Milvus down → 200）、`test_health_response_fields`（依赖集 + 新字段）。
- [ ] **Step 2: 验证失败**。
- [ ] **Step 3: 实现**：`_liveness_payload`（fsm/uptime/build，无网络）；`_readiness_payload`（`asyncio.gather` 并行 ping session/message/attachment + idempotency（仅 product）+ milvus 仅上报）；HTTP 状态按 `chatReady`；`status`/`chatReady`/`fullReady`/`dependencies` 组装。
- [ ] **Step 4: 验证通过**。
- [ ] **Step 5: 提交**：`git commit -m "feat(server): liveness without network probes, core-chat readiness gate"`

---

### Task 10: 限流认证主体分类

**Files:**
- Modify: `core/input/rate_limiter.py`（`rate_key_for_request` 加 `valid_tokens` 参数）
- Modify: `core/input/server.py`（中间件传合法 token 集 + `request.app["rate_valid_tokens"]` 供 WS 建连读取）
- Modify: `core/input/ws_handler.py`（`handle_ws:362` 建连限流 key 改用 `request.app` 内合法集）
- Modify: `core/input/tests/test_rate_limit.py`（**`TestRateKey` 既有断言适配**——`test_authenticated_key_stable_across_ips` 等传 `valid_tokens`）、`core/input/tests/test_auth.py`

**Interfaces:**
- Consumes: `create_app` 作用域 `auth_token`/`access_token`。
- Produces: `rate_key_for_request(request, valid_tokens: frozenset[str])`；认证成功→auth 桶，无效/缺失→ip 桶。

- [ ] **Step 1: 写失败测试**：
  - 合法 Token 两主体并发 → 独立配额（互不挤占）；
  - 同 IP 轮换 10001 个假 Token → 不获 10001 份配额（仍受 ip 桶限制）、不挤掉合法主体桶；
  - 缺失 Authorization → ip 桶；
  - **更新 `TestRateKey`**：`test_authenticated_key_stable_across_ips` 传含该 token 的 `valid_tokens` 后仍 `auth:` 前缀；无效 token → `ip:` 前缀。
- [ ] **Step 2: 验证失败**（现状任意 Authorization 独立 key）。
- [ ] **Step 3: 实现**
  - `rate_key_for_request(request, valid_tokens: frozenset[str])`：解析 `Bearer <token>`；token ∈ valid_tokens（`hmac.compare_digest` 遍历，集小）→ `"auth:" + sha256(token)[:16]`；否则/缺失 → `"ip:" + (request.remote or "unknown")`。
  - 中间件：`_make_rate_limit_middleware(limiter, valid_tokens)`；`create_app` 在 `request.app["rate_valid_tokens"] = frozenset({auth_token, access_token} - {""})`。
  - `handle_ws` 建连限流 key：`rate_key_for_request(request, request.app.get("rate_valid_tokens", frozenset()))`。
- [ ] **Step 4: 验证通过**。
- [ ] **Step 5: 提交**：`git commit -m "fix(rate-limit): bucket by validated principal, invalid/missing → IP"`

---

### Task 11: 文档同步 + runbook

**Files:**
- Modify: `README.md`、`docs/electron-backend-security.md`、`docs/progress/2026-08-10-backend-frontend-progress.md`、`docs/contract-release-2026-08-11.md`、`docs/review/electron-v2-backend-change-list-2026-08-11.md`、`todo.md`
- Create: `docs/electron-v2-first-integration-runbook.md`

**Interfaces:**
- Consumes: 全部任务语义。
- Produces: 文档互不冲突 + runbook（含 §8.5 纪律 + §8.8 六事件通过标准）。

- [ ] **Step 1: 按 spec §3.3 逐项同步**（security fail-closed/host 默认、README server.host、progress 历史快照标注、contract-release contractCommit/runtimeCommit 区分、change-list 同步、todo 关闭项+决策）。
- [ ] **Step 2: runbook**：按 spec 内容清单 + review §8.5/§8.8（runtime SHA/schemaHash 留占位待联调回填——spec DoD 已声明，非占位符）。
- [ ] **Step 3: 验证**：`grep` 确认无残留旧表述。
- [ ] **Step 4: 提交**：`git commit -m "docs: sync security/progress/contract-release/change-list + integration runbook"`

---

### Task 12: 全量门禁

- [ ] **Step 1**: `conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short`（记录计数）
- [ ] **Step 2**: `conda run -n Elysia pyright`（0 errors）；`conda run -n Elysia ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__`；`conda run -n Elysia make check-types`；`python scripts/gen_contract_frames.py --check`
- [ ] **Step 3**: 若失败——修复（注释中文、类型注解、语义保持），重跑直至绿。
- [ ] **Step 4**: `conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short`（test-normal，~5min，**对齐当前 Makefile**——review 复核确认 Makefile 已用 `not slow and not external`，非 CLAUDE.md 的旧 `not slow`）记录计数。

---

## Self-Review

- **Spec 覆盖**：批次 A（Task 1-5 = BE-P0-01/P0-02/P1-01/P1-02）、批次 B（Task 6-8 = BE-P1-04/P1-03/P1-05）、批次 C（Task 9-11 = BE-P2-01/P2-02/P2-03）+ Task 12 门禁。**D3（STORAGE_UNAVAILABLE 携带 idempotency_durable）在 Task 4 实现**——review 复核确认无遗漏。全部 review 条目有 Task。
- **类型一致**：`ClaimResult`（repo，Task 1）/`StoreResult`（store，Task 2）分层定义、Task 4 消费；`store.complete/reject` 签名 Task 2 定死、Task 4 按此调用；`_send_ack` 签名（Task 4 加 received_at + error dict；Task 6 改判别构造 + AckError.model_validate 归一 dict/AckError）；`find_by_user_message`（Task 3→4）；`rate_key_for_request` 新签名（Task 10）；`is_connected`（Task 1 base 基类）。
- **无占位符**：`claimed_at` 时钟（墙钟 time.time()）、`GRACE_SECONDS`、`_IN_PROGRESS_RETRY_AFTER_MS`、`StoreResult` 均已定死；Task 6 gen_ts 判定已收敛（无需额外改动）；runbook runtime SHA/schemaHash 为显式联调回填（spec DoD 声明）。
- **既有测试适配**：`test_idempotency_repo.py`（Task 1）、`test_health.py`（Task 9）、`TestRateKey`（Task 10）、`test_list_empty`（Task 8）——全部列入对应 Task Files。
- **批次依赖**：Task 4 依赖 1/2/3；Task 6 独立（契约）；Task 7 依赖 6；Task 8 依赖 Task 5 的恢复 hook；Task 9 依赖 5；Task 10 独立。Task 2 后、Task 4 前不跑 `test_v2_ws.py`（瞬态已声明）。
