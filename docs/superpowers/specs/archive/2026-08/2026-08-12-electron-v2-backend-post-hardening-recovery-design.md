# Electron v2 后端加固后复核整改 — 恢复正确性与文档收口设计

> **日期：** 2026-08-12
> **审查对象：** `main@96623b5`
> **对照基线：** `main@b5b7e6e`（上一份首次联调后端复核基线）
> **关联 review：** `docs/review/electron-v2-backend-post-hardening-review-2026-08-12.md`
> **本 spec 范围：** BE-P1-06（完整记录恢复 API）、BE-P1-07（恢复触发条件）、BE-P2-04（runbook 修正）、BE-RISK-01（接受风险诚实记录）、D4（/ready 门禁含补偿状态）。
> **不在本 spec 范围：** Electron 前端阶段 0/1、前端运行时 Schema、跨仓契约字段扩展（schemaHash 上握手等，另起跨仓契约变更）。

## 1. 背景与已核实证据

上一份加固批次的验收为「PASS WITH NOTES」，但复核报告通过代码审查 + 故障注入确认两个恢复路径 P1 缺陷未闭合。本批所有事实主张已在代码层核实：

1. **BE-P1-06 属实**：`core/input/idempotency.py` 的 `recover()` 只对 Mongo 无记录的条目调用 `repo.claim(...)`，仅写最小 claim 字段，不写终态 ACK 重放负载（`received_at`/`client_message_id`/`conversation_id_wire`/`error`）。重启后 `from_doc()` 得到残缺终态，rejected 重放被 `ws_handler.py` 构造为 `AckError(code="INTERNAL", message="未知错误", retryable=False)`。
2. **BE-P1-07 属实**：`core/agent/_lifecycle_persistence.py` 的 `_db_health_loop` 以 `prev: dict[str, bool] = {}` 起始，首轮健康结果只建基线（`name not in prev` → 跳过 recover）；而 `Repository._health_check`（base.py）在 ping 失败后会内部重连——「离线启动 + 首轮探测前 Mongo 已恢复」时首轮直接 true，recover 永不触发，降级期内存记录不补落。
3. **BE-P2-04 属实**：`docs/archive/electron-v2-first-integration-runbook.md:60/:125` 断言 `server_info` 携带 `schemaHash`，但 `ServerInfoMessage`（frontend_contract/ws_message.py）实际字段无 `schemaHash`。
4. **BE-RISK-01 属实（已记录）**：`2026-08-11-electron-v2-backend-hardening-design.md:71` 决策记录 D7-b 已明确接受「ACCEPTED 后、loop `register()` 前 kill」窗口。

**本批 spec review 追加确认的设计缺陷（代码层可构造）：**

- **S1（recover 覆盖终态）**：`recover()` 的「Mongo 已有 → 刷新内存」分支会无条件用 Mongo 的陈旧 `claimed` 覆盖内存中更前进的 `accepted` 终态（complete/reject 写穿失败后 Mongo 停留 claimed、内存为 accepted 含完整负载）——accepted 完整 ACK 负载从内存与 Mongo 同时消失，无重试路径再持久化。
- **S2（durable 信号不可靠）**：`durable` 的唯一负因子 `_in_memory_fallback` 会被任意成功 Mongo 操作（lookup/store/complete/reject/delete/configure 六处）无条件清零——降级期记录可在未 reconcile 时被无关操作静默标记 durable，P1-07 想修的首轮漏补偿在请求介入时依旧复现；且降级内存 entry 在 fallback 翻转后会被 repo.claim 重新 insert（重复处理窗口）。
- **S3（/ready 误伤）**：`/ready` 门禁若直接门 `durable`，启动瞬时建连失败 + 零降级数据场景下 Mongo 恢复后 `/ready` 最长 60s 误报 503（编排器被无谓阻塞）。
- **S4（store 未配置死循环）**：触发条件若为 `not durable`，repo 为 None（纯内存/未配置）时 durable 恒 False → 健康循环每 60s 重复 connect+空 recover 永不收敛。

## 2. BE-P1-06：完整记录恢复 API

### 2.1 根因

`store.recover()` 只补最小 claim，进程存活期间被内存 overlay 掩盖；进程重启后 `from_doc()` 只能重建残缺终态——accepted 重放用本次重试时间代替原 `receivedAt`，rejected 重放得到 `error=None`。与「持久化字段逐字一致重放」（spec §1.1）直接冲突。叠加 S1：即使 Mongo 已有记录，恢复路径也可能把更前进的内存终态覆盖掉。

### 2.2 设计：`IdempotencyRepository.restore(doc)`（条件 upsert）

`DocumentDBBackend.update_one` 支持 `upsert=True`（返回 modified_count，不暴露 upserted_id——D6-d 既有结论）。restore 用条件 upsert 一次表达「缺失插入 / claimed 升级 / 终态 no-op」：

```python
async def restore(self, doc: dict) -> None:
    """完整记录恢复（BE-P1-06）：缺失插入 / claimed 升级 / 终态防御读回（原子 upsert）。

    区别于 claim()（只写最小 claim 字段），restore() 写入完整 ACK 重放负载
    （received_at/client_message_id/conversation_id_wire/error）——降级期终态补落
    Mongo 后跨重启重放逐字一致。

    过滤器 status∈{claimed,in_progress} + upsert：
    - Mongo 缺失 → 插入完整 doc；
    - Mongo 为 claimed（本进程写穿失败的前进状态）→ $set 升级为完整终态；
    - Mongo 已终态（另一进程 winner / 已落库）→ 过滤器失配，upsert 尝试插入同键 →
      唯一索引 E11000 → non_fatal 防御层读回 winner（**不覆盖**）。注：recover() 的
      get-first 已保证终态 doc 不进入本方法（§2.4 #4 在 recover 层处理），E11000 层
      仅兜底并发/直调。

    原子性：单条 update_one(upsert) 无部分态；并发恢复者 last-wins（同内容）；
    唯一索引并发 upsert 竞态的罕见 E11000 经 non_fatal 读回 winner（防御层）。

    Args:
        doc: 完整记录（conversation_id/client_request_id 必含——身份键守卫，缺失
            抛 ValueError，对齐 AttachmentRepository.create 先例；其余为
            content_digest/message_id/status/claimed_at/expires_at + 终态负载）。

    Raises:
        ValueError: 缺 conversation_id/client_request_id（编程错误，非存储故障）。
        StorageUnavailableError: 存储不可用。
    """
    db = self._require_db()
    if doc.get("conversation_id") is None or doc.get("client_request_id") is None:
        raise ValueError("IdempotencyRepository.restore 缺少身份键")
    set_expr = {k: v for k, v in doc.items()
                if k not in ("conversation_id", "client_request_id")}
    try:
        await self._call(
            lambda: db.update_one(
                COLLECTION,
                {"conversation_id": doc["conversation_id"],
                 "client_request_id": doc["client_request_id"],
                 "status": {"$in": [STATUS_CLAIMED, "in_progress"]}},
                {"$set": set_expr},
                upsert=True,
            ),
            reason="IdempotencyRepository.restore 写入失败",
            non_fatal=_is_duplicate_key,
        )
    except Exception as e:
        if not _is_duplicate_key(e):
            raise
        # 并发 upsert 竞态（罕见 E11000）：读回 winner，不判死 repo（防御层）
        existing = await self._call(
            lambda: db.find(COLLECTION,
                            {"conversation_id": doc["conversation_id"],
                             "client_request_id": doc["client_request_id"]},
                            limit=1),
            reason="IdempotencyRepository.restore 冲突读回失败",
        )
        # winner 已在 Mongo；调用方（recover）内存已含完整终态或按 #4 刷新，无需覆盖
```

注：`status: {$in: [...]}` 非等值条件、不进入 upsert 插入文档——插入文档的 status 来自 `$set`（即 doc["status"]）。Mongo 等值条件（conversation_id/client_request_id）自动并入插入文档。

### 2.3 `store.recover()` 重写

get-first → 终态优先 / restore upsert 补齐，逐条 reconcile：

```python
async def recover(self) -> None:
    """Mongo 恢复 reconcile（D5 + BE-P1-06/07）：对每条内存 entry 与 Mongo 对齐。

    内存权威 overlay 逐条对齐，四分支：
    - Mongo 缺失 → restore() 插入完整文档（含终态 ACK 负载）；
    - Mongo claimed + 内存终态（本进程写穿失败的前进状态）→ restore() 升级为终态；
    - Mongo claimed + 内存 claimed → 刷新内存为 Mongo 状态（Mongo 的 message_id 权威，
      防多 worker 翻转）；
    - Mongo 已终态 → 刷新内存为 Mongo 状态（Mongo 优先、读时一致）。
    跨重启重放逐字一致（修复 S1：不再用陈旧 claimed 覆盖前进终态）。结束后
    ``_in_memory_fallback = False`` 且 ``_needs_recovery = False``（durable 翻回）。
    任一 get/restore 失败 → 异常冒泡，不翻 durable（下轮健康循环重试，§4.3 #6）。
    """
    if self._repo is None:
        return  # 纯内存路径无可 reconcile
    for (conversation_id, client_request_id), entry in list(self._entries.items()):
        if self._is_stale(entry):
            # 过期内存记录（Mongo 已 TTL 清除）→ 不复活（review Minor#3）
            self._entries.pop((conversation_id, client_request_id), None)
            continue
        doc = await self._repo.get(conversation_id, client_request_id)
        mongo_terminal = doc is not None and _is_terminal_status(doc.get("status"))
        memory_terminal = entry.status in (STATUS_ACCEPTED, STATUS_REJECTED)
        if doc is not None and (mongo_terminal or not memory_terminal):
            # Mongo 已终态（另一进程 winner / 已落库）→ 刷新内存为 Mongo 状态（#4）；
            # 或双方皆 claimed → Mongo 的 message_id 权威（防多 worker 降级同键时
            # message_id 在 A/B 间翻转——各自 restore 会交替覆盖 Mongo claimed）
            self._entries[(conversation_id, client_request_id)] = IdempotencyEntry.from_doc(doc)
            continue
        # Mongo 缺失，或（Mongo claimed + 内存终态 = 本进程写穿失败的前进状态）→
        # restore 原子 upsert 完整文档（缺失→插入 / claimed→升级终态，含重放负载）
        await self._repo.restore(_build_full_doc(conversation_id, client_request_id,
                                                 entry, self._ttl))
    self._in_memory_fallback = False
    self._needs_recovery = False
```

`_build_full_doc`（**模块级函数，无 self**，与 `_mongo_expiry`/`_is_terminal_status` 同层）从内存 entry 构建完整文档（`client_request_id` 是 `_entries` 的 dict key 部分、不在 entry 内，由调用方传入；`ttl_seconds` 传 store 的 `self._ttl`，与 claim() 写穿同基——防自定义 TTL 下两种过期语义分裂）：

```python
def _build_full_doc(conversation_id: str, client_request_id: str,
                    entry: IdempotencyEntry, ttl_seconds: float) -> dict:
    """从内存 entry 构建 Mongo 完整文档（claim 字段 + 终态 ACK 负载）。"""
    full = {
        "conversation_id": conversation_id,
        "client_request_id": client_request_id,
        "content_digest": entry.content_digest,
        "message_id": entry.user_message_id,
        "status": entry.status,
        "claimed_at": entry.claimed_at if entry.claimed_at is not None else time.time(),
        "expires_at": _mongo_expiry(ttl_seconds),
    }
    if entry.received_at is not None:
        full["received_at"] = entry.received_at
    if entry.client_message_id is not None:
        full["client_message_id"] = entry.client_message_id
    if entry.conversation_id_wire is not None:
        full["conversation_id_wire"] = entry.conversation_id_wire
    if entry.error is not None:
        full["error"] = entry.error
    return full
```

辅助：

```python
def _is_terminal_status(status: str | None) -> bool:
    """Mongo status 是否终态（accepted/rejected + legacy completed）。"""
    return status in (STATUS_ACCEPTED, STATUS_REJECTED, "completed")
```

### 2.4 语义保证（报告 §4.3 逐条映射）

| # | 报告要求 | 设计满足 |
|---|---|---|
| 1 | Mongo 无 + 内存 CLAIMED → 补落完整 claim | restore upsert 缺失→插入完整 doc |
| 2 | Mongo 无 + 内存 ACCEPTED → 补落完整 accepted ACK 字段 | `_build_full_doc` 含 `received_at/client_message_id/conversation_id_wire` |
| 3 | Mongo 无 + 内存 REJECTED → 补落完整 error（含 details/retryAfterMs） | `full["error"]` 为内存完整 error dict |
| 4 | Mongo 已有 → Mongo 优先，不覆盖另一进程 winner | get-first 分支：Mongo 终态或双方皆 claimed → 刷新内存为 Mongo 状态（含 message_id，防多 worker 翻转）；仅 Mongo claimed + 内存终态才 restore 升级 |
| 5 | 并发恢复 DuplicateKey → 读回 winner + 刷新内存，不判死 repo | upsert 天然 last-wins；终态同键 → E11000 → 防御层 non_fatal 读回（recover 的 get-first 已刷新内存） |
| 6 | 任一步失败 → recover 失败、durable 不提前翻，下轮重试 | 异常冒泡，`_in_memory_fallback=False`/`_needs_recovery=False` 在循环后唯一位置；健康循环 `_recover_dependency` 捕获返回 False |
| 7 | 过期内存记录不复活 | `_is_stale(entry)` 先行弹出（既有规则不变） |

## 3. BE-P1-07：恢复触发条件

### 3.1 根因

`_db_health_loop` 用进程内 `prev` 推断「是否需要补偿」，但首轮 `prev={}` 只建基线不触发；而 `health_check()` 内部重连（base.py `_health_check`），离线启动 + 首轮探测前恢复 ⇒ 首轮直接 true，recover 永不触发。叠加 S2：`durable` 会被无关成功操作清零，即使有了边界触发也未必可靠。

### 3.2 设计：strengthen `durable` + `_dependency_needs_recovery(name)` 钩子

**store 状态扩展（idempotency.py）：**

```python
class IdempotencyStore:
    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], IdempotencyEntry] = {}
        self._repo: IdempotencyRepository | None = None
        self._product_mode = False
        self._in_memory_fallback = True
        self._needs_recovery = False   # NEW：存在未 reconcile 到 Mongo 的内存记录

    @property
    def durable(self) -> bool:
        """当前是否完整持久化（BE-P1-07 强化）：repo 已配置 且 非操作级降级
        且 无未 reconcile 记录。server_info 能力暴露用——无关成功操作不再能静默清零。
        """
        return (self._repo is not None and not self._in_memory_fallback
                and not self._needs_recovery)

    @property
    def configured(self) -> bool:
        """是否配置了持久化后端（repo 非 None；恢复触发 guard，防 repo-None 死循环）。"""
        return self._repo is not None

    @property
    def needs_recovery(self) -> bool:
        """是否有未 reconcile 到 Mongo 的内存记录（/ready 门禁信号）。"""
        return self._repo is not None and self._needs_recovery
```

**`_needs_recovery` 置位/清零点：**

| 时机 | 动作 |
|---|---|
| store() 降级分支（dev，Mongo 报错写内存） | `_needs_recovery = True`（降级写入未 reconcile） |
| complete()/reject() 写穿失败（best-effort 吞错后） | `_needs_recovery = True`（内存终态超前 Mongo） |
| recover() 全部成功后 | `_needs_recovery = False` |
| 其他成功操作 | **不清零**（全局标志，其他 key 可能仍 dirty——须等 recover） |
| `reset()`（测试隔离，reset_store 调用） | `_needs_recovery = False`（清空全部状态，防测试顺序污染——否则 TestDevDegradation 先置位、后续 durable-True 断言全挂） |
| `configure()`（create_app / in-process 重启） | **不清零**（重启后降级 entry 仍在内存，须保持待恢复标志、recover 才能触发——S2 不复开） |

**store() 去重短路扩展（修复 S2 的重复处理窗口，不破坏 TAKEOVER）：**

```python
if self._repo is not None and (self._in_memory_fallback or self._needs_recovery):
    # 降级/未 reconcile 窗口：同键有效内存 entry 恒去重短路（不触碰 Mongo）——
    # 关闭「down→up 且 recover() 未跑时 repo.claim 对内存 claim 重新 insert 导致
    # 重复处理」窗口。正常路径（已 reconcile）不走短路，保留 repo.claim 的 stale
    # 接管（TAKEOVER）语义。
    prev = self._memory_lookup(conversation_id, client_request_id)
    if prev is not None:
        return StoreResult(entry=prev, acquired=False)
```

**协调器触发（_lifecycle_persistence.py）：**

```python
for name, ok in results.items():
    if not ok:
        continue
    # 恢复触发：False→True 边界（既有）∪ 依赖自报需补偿（BE-P1-07：首轮 prev={}
    # 无边界可依，按依赖补偿状态补触发；durable 已强化——无关操作不能静默清零）
    edge_recovery = name in prev and not prev[name]
    if not edge_recovery and not self._dependency_needs_recovery(name):
        continue
    if not await self._recover_dependency(name):
        results[name] = False  # 补偿未完成 → 下轮重试
prev = results
```

```python
def _dependency_needs_recovery(self, name: str) -> bool:
    """依赖自报需补偿（BE-P1-07）：probe 为 true 不足以判定「已对齐」。

    首轮 prev={} 时 False→True 边界不可得（离线启动、首轮探测前 Mongo 已恢复——
    health_check 内部重连直接返回 true）。本轮仅 idempotency 接线：configured 且
    durable=False（= 操作级降级 ∨ 未 reconcile 记录）→ 执行 recover；configured
    guard 防 repo-None（纯内存/未配置）时 durable 恒 False 的每轮空转死循环（S4）。
    其余依赖保持边界触发（已知潜在同类项，YAGNI 不推广）。
    """
    if name != "idempotency":
        return False
    from core.input.idempotency import get_store
    return get_store().configured and not get_store().durable
```

### 3.3 语义保证（报告 §5.2 逐条映射）

| # | 报告要求 | 设计满足 |
|---|---|---|
| 1 | probe true 且 `durable=False` → recover；成功后 durable 翻 true | `_dependency_needs_recovery` 判定（`configured and not durable`）+ `_recover_dependency("idempotency")`（recover() 末尾清 fallback+needs_recovery → durable True） |
| 2 | （备选：prev 从 startup 初始化） | **不采用**（见 D2；需每依赖启动态追踪，改动面大且不覆盖静默恢复） |
| 3 | 通用 needs_recovery 骨架，健康与补偿分开建模 | 采纳骨架——`_needs_recovery` 显式标志 + `_dependency_needs_recovery` 钩子即该协议第一实现 |
| 4 | 补偿失败保持待恢复，下轮重试（即使 probe 恒 true） | recover 失败 → `_recover_dependency` 返回 False → `results[name]=False`；durable 仍 False → 下轮触发 |
| 5 | 恢复 hook 幂等，允许不确定边界重复调用 | recover() get-first + restore upsert 幂等；无降级记录时 no-op 自愈 |

### 3.4 S1/S2/S3/S4 修复映射

| 评审缺陷 | 修复 |
|---|---|
| S1 recover 覆盖终态 | §2.3 终态优先 + restore upsert 升级（claimed→终态） |
| S2 durable 被无关操作清零 | §3.2 durable 强化含 `_needs_recovery`；store() 去重短路扩展到未 reconcile 窗口 |
| S3 /ready 误伤 | §4 门禁改 `ping AND not needs_recovery`（非 durable） |
| S4 repo-None 死循环 | `configured` guard（§3.2 `_dependency_needs_recovery`） |

## 4. D4：/ready 幂等门禁含补偿状态

现状 `_readiness_payload`：`chat_ready = mongo_ok and (not product_mode or idempotency_ok)`，其中 `idempotency_ok` 仅真实 ping——只测可达、不测补偿状态。

修改（product 模式）：

```python
idempotency_ok = True
if idempotency_repo is not None:
    idempotency_ok = await _probe(idempotency_repo.health_check)
    # D4（BE-P1-07 §5.3.6）：product 模式幂等门禁含补偿状态——ping 可达不足以判定
    # 对齐；存在未 reconcile 记录（needs_recovery）时 chatReady 保持 false。
    # 用 needs_recovery 而非 durable：启动瞬时建连失败 + 零降级数据（needs_recovery
    # False）时 Mongo 恢复即可 ready，不受 durable 标志 60s 自愈窗口误伤（S3）。
    if product_mode:
        from core.input.idempotency import get_store
        idempotency_ok = idempotency_ok and not get_store().needs_recovery
```

`_readiness_payload` docstring 需同步（现仍声明「idempotency_ok 仅真实 ping」）。`get_store()` 进程全局单例与注入 repo 同实例的耦合由 create_app 单点维持（`configure_store` + `app["idempotency_repo"]` 同源）——与 capabilities.py:104 既有先例一致，docstring 注明。

启动顺序已核实：`run_server` 先 `await self.start()`（`_start_persistence` 建连）再 `create_app`（`configure_store`）——健康启动时 repo 已连接，`durable` 初值 True，`/ready` 门禁不误伤正常启动。dev 模式不变（内存降级允许、仅上报）。`server_info.features.idempotency_durable` 已暴露 `get_store().durable`（握手快照），补偿完成后新连接自然反映 true。

**已知残余（accepted trade-off，D4 修订记录）**：零降级瞬时建连失败窗口（启动时 connect 失败、Mongo 恢复、无任何降级记录）内，`needs_recovery=False` → `/ready` 200，但 `durable` 仍 False（fallback 未清）→ 新连接 `server_info.idempotency_durable=False`，两信号短暂分化。二者各自诚实（/ready 测「可服务」，durable 测「全部记录已持久化」），≤60s 自愈（首个请求成功操作或健康循环首轮 recover 清 fallback）。不升级处理（YAGNI）——报告 §5.3.6 验收只覆盖降级记录场景，本残余不影响。

## 5. BE-P2-04：Runbook 修正

`docs/archive/electron-v2-first-integration-runbook.md`：

1. `:60` 与 `:125`：删除 `server_info` 运行时携带 `schemaHash` 的说法（`server_info` 字段以 `ServerInfoMessage` 为准：features/version/services/connectionId?/build_sha?）。
2. `schemaHash` 验证明确写成**联调前离线门禁**：`python -m frontend_contract --check` + `python scripts/gen_contract_frames.py --check`，并记录冻结产物 hash（`:15` 行保留、措辞确认）。
3. 保留 runtime backend SHA / frontend SHA 的联调前回填项。
4. 记录：「未来若需握手协商 schemaHash，另起跨仓契约变更」（不擅自加字段）。

## 6. BE-RISK-01：接受风险诚实记录

遵守报告 §6.2（不改代码）：

1. `todo.md`、加固报告与联调结论不得声称 production exactly-once 或「accepted 后任务绝不丢失」。
2. 首轮联调通过只能标记 happy path，不关闭该风险。
3. 生产封口需独立设计持久化 ingress/outbox 或 accepted 前建立可恢复 generation 证据，**并提供启动恢复重新入队机制**（报告 §6.2.3 的并行必需项——accepted 后未消费的工作需在进程重启后重新入队；缺它则封口路径不完整）。记录为后续工作项，不在本批。
4. 未来实现必须同时证明「不丢工作」与「不重复 generation」。

## 7. 决策记录

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| **D1** | 恢复 API 形态 | `IdempotencyRepository.restore(doc)` 条件 upsert（filter status∈{claimed,in_progress} + `$set` 完整 doc + upsert=True） | `update_one` 支持 upsert（已核实）；单条原子无部分态；缺失→插入 / claimed→升级 / 终态 no-op 三态一次表达，天然处理 S1（写穿失败的前进终态不被陈旧 claimed 覆盖）；并发恢复 last-wins 免 DuplicateKey。备选「claim 后补 complete/reject」需改 best-effort 吞错为传播且两写非原子——更弱。E11000 竞态仅防御层保留 non_fatal 读回 |
| **D2** | 恢复触发条件 | 报告方案 1 强化：durable 含 `_needs_recovery`（无关操作不再静默清零）+ `_dependency_needs_recovery = configured and not durable` | 方案 1 字面满足报告 §5.2 #1，但原始 durable 会被无关操作清零（S2）——强化后可靠。configured guard 防 repo-None 死循环（S4）。钩子形态即方案 3 的可扩展 needs_recovery；不对 session/ltm/generation 做无验收测试支撑的推广（已知潜在同类项，YAGNI） |
| **D3** | 分批方式 | 单批次（一个 spec + 一个 plan，任务按 A→B→C 排序） | 两个 P1 共享文件（idempotency.py/_lifecycle_persistence.py），P1-07 验收 #5 依赖 P1-06（组合跨重启），拆开切断验收闭环；本批范围小（2 P1 + 1 P2 文档），单 spec 足够 |
| **D4** | /ready 幂等门禁语义 | product 模式门禁 = ping AND `not needs_recovery`（非 durable） | §5.3.6 验收「只能在补偿完成后反映 durable=true」对降级记录场景的字面满足；用 needs_recovery 避免 S3（启动瞬时建连失败 + 零降级数据时 60s 误报 503）。dev 模式不变 |
| **D5** | store() 去重短路 | 由 fallback-only 扩展为 `fallback OR needs_recovery` 窗口 | 关闭 S2 的「down→up 且 recover 未跑时对内存 claim 重新 insert」重复处理窗口；正常路径保留 repo.claim 的 TAKEOVER 语义（未 reconcile 窗口内 TAKEOVER 兜底 IN_PROGRESS，recover 后恢复，可接受） |

## 8. 测试要求

### 8.1 BE-P1-06（`core/input/tests/test_idempotency_durable.py` + `core/storage/tests/test_idempotency_repo.py` 扩展）

**关键约束：必须模拟「清空进程内 store 后从 Mongo 重新读取」**——用 `reset_store()` + 同 repo 重 `configure_store()` 模拟重启，不能在 recover 后继续命中原内存。**测试 fake（`_FlipRepo`/`_FlipIdemRepo`）必须镜像新增的 `restore()` 方法面**（逐一委托 `self._impl`），否则 §8.1/§8.2 全部新用例 AttributeError、既有用例也受影响。

**新增 fixture 约束：必须新建 `is_connected` 与 `health_check` 解耦的 fake**（如 `_SplitIdemRepo`：独立 `connected`/`ping_ok` 字段）——既有 `_FlipIdemRepo`（test_lifecycle_idempotency.py:66-70）把两者绑在同一 `available`（configure 的 durable 初值来自 `is_connected`），无法表达「configure 时未连接（durable=False）但首轮 health_check 直接 true」这一 P1-07 核心前置；test_health.py 的 `_idem_repo(MagicMock)` 恒真值（durable 恒 True）同样不可表达。未解耦则 §8.2 新用例只能绕过真实前置（手工置 `_needs_recovery`）或断言失败。

**「重放逐字一致」断言层（#2/#3/#5/#8）**：断言**必须在 `reset_store()` + 重 `configure_store()` 之后**（Mongo-sourced lookup），不得在 recover() 后直接 lookup——recover 后内存 overlay 命中会假绿（验证的是内存副本而非补落内容）。§8.2#7 的组合跨重启用例同理。

**#7 并发恢复唯一性约束**：现有 `FakeDocumentDBBackend.update_one` 不强制唯一索引（同键重复 doc 可并存），`_DuplicateOnceFake` 只拦 `insert_one`——§8.1#7「并发恢复 Mongo winner 不被 loser 覆盖」需新增**强制唯一索引的 update_one fake**（第二次 upsert insert 抛 DuplicateKeyError）才能验证 E11000 防御层，否则测试静默假通过（get 返回随机一条）。

1. `test_recover_claimed_entry_survives_store_restart`——降级 + claim → recover → 重启 store → lookup 从 Mongo 命中。
2. `test_recover_accepted_entry_replays_identical_ack_after_restart`——降级 + complete → recover → 重启 → 重放 accepted ACK 完整 wire JSON 与首次一致。
3. `test_recover_rejected_entry_replays_identical_ack_after_restart`——reject（`RATE_LIMITED.retryAfterMs`）→ recover → 重启 → 重放逐字一致。
4. rejected 覆盖 `STORAGE_UNAVAILABLE.details.idempotency_durable`。
5. **Mongo 已有 claimed + 内存 accepted（S1）**：store 成功（Mongo=claimed）→ complete 写穿注入失败 → recover → 断言 Mongo 升级为 accepted 含完整负载；重启后重放逐字一致。
6. recover 完成前在完整终态写入处注入失败 → `store.durable is False`，下一轮可继续补偿。
7. 两个恢复者竞争同键 → Mongo winner 不被 loser 覆盖，repo 保持健康（health_check true）。
8. accepted/rejected 重放完整 JSON 与首次 ACK 比较（不只 status/code——含 receivedAt/clientMessageId/conversationId/details/retryAfterMs）。
9. `restore` 缺身份键 → ValueError（对齐 AttachmentRepository.create 先例）。

### 8.2 BE-P1-07（`core/agent/tests/test_lifecycle_idempotency.py` 扩展）

**fixture 继承 §8.1 全部约束**（fake 镜像 `restore()`、`is_connected`/`health_check` 解耦、断言在 reset+reconfigure 后）。**#8（/ready 与 server_info）落 `core/input/tests/test_health.py`**——用真实 `IdempotencyStore` 置 `_needs_recovery=True`，构造方式：复用 `test_idempotency_durable.py` 的 `_FlipRepo` 模式（`claim()` 先失败后成功）走真实降级写路径（Mongo 报错 → 内存降级 → 置 needs_recovery），**不是** `_SplitIdemRepo`（其 `claim` 未设计为可翻转失败，用它构造降级会退化为 never-recovers）。随后 `create_app(product_mode=True)` 调 `ready_handler` 断言 503→200、`build_server_info` 断言 `idempotency_durable` False→True；不用 MagicMock（无法表达 needs_recovery=True，断言会退化为恒真）。

1. Mongo 离线启动 + 内存保存一条记录；第一次 health round 直接返回 true → 断言当轮执行 recover、记录已落库、durable=true。
2. **recover 连续失败后重试**（报告 §9#3 不变量）：第一次 recover 失败、第二轮仍失败、第三轮成功 → 断言每一轮都重试（prev 保持 False / durable 保持 False → edge 与 needs_recovery 每轮重触发），最终落库、durable=true。单次失败即成功不足——需钉死「prev 不被成功前的失败轮污染跳过」的连续多轮不变量。
3. 启动时即健康且无降级记录 → 不产生破坏性重复补偿（recover no-op）。
4. **S2 无关操作不清零**：离线启动降级一条记录 → Mongo 恢复 + 另一 key 的 store() 成功 → 断言 durable 仍 False（needs_recovery 保持）→ 健康循环触发 recover → 落库、durable=true。
5. **S4 未配置不空转**：store 未配置（repo=None / reset_store）→ 健康循环 probe true → 断言不产生每轮 connect+recover 空转（`_dependency_needs_recovery` 返回 False）。
6. 运行中 true→false→true 既有恢复测试继续通过（不回归）。
7. 与 BE-P1-06 组合：首轮直接恢复 accepted/rejected → 模拟进程重启后 ACK 仍逐字一致。
8. `/ready`（product 模式）与新连接 `server_info.features.idempotency_durable` 只在补偿完成后反映 durable=true。
9. 既有测试 `test_offline_start_recovers_after_availability_flip` 的 docstring 同步（原「恢复仅在 False→True 边界触发…需两轮」语义过时——needs_recovery 后首轮即可触发）。

### 8.3 回归

报告 §2.1 的 230 项定向测试 + 既有并发 claim/DuplicateKey/三态恢复/限流/default session/readiness 测试不回归。重点复核：store() 短路扩展对既有「内存降级路径并发测试」与 TAKEOVER 用例的影响。

## 9. 完成门禁（报告 §9 逐条）

1. BE-P1-06、BE-P1-07 所有组合故障测试通过。
2. 必须包含清空/重建 IdempotencyStore 后从 Mongo 读取的断言。
3. 首轮 health probe 直接 true 与 recover 连续失败后重试均有测试。
4. accepted/rejected ACK 比较完整 wire JSON（时间、clientMessageId、conversationId、details、retryAfterMs）。
5. 既有并发 claim、DuplicateKey、三态恢复、限流、default session、readiness 测试不回归。
6. `python -m frontend_contract --check` 与 `python scripts/gen_contract_frames.py --check` 通过。
7. Runbook 不再要求当前 wire 中不存在的字段。
8. `todo.md` 与最终报告诚实区分「已修缺陷」与「BE-RISK-01 接受风险」。
9. 至少复跑报告 §2.1 的 230 项定向测试；提交前按仓库门禁执行 quick/normal。

联调状态统一口径（报告 §1）：

> 后端正常文本链路和并发 claim 主路径已加固；Mongo 降级恢复仍有两个缺口（终态负载丢失、首次健康探测漏补偿）——本批修复并完成跨重启故障注入后，方可认定 happy-path 联调候选；BE-RISK-01 残余窗口保持开放。
