# Token 用量统一数据结构 + input/output 区分

> 2026-07-31 · 设计文档

## 动机

「一次 LLM 调用」的 token 消耗记录在系统内存在 **6 套定义**，字段和类型彼此不一致：

| 层 | 结构 | 问题 |
|----|------|------|
| 内存 | `TokenUsage`（dataclass，`token_tracker.py`） | timestamp: float |
| 持久化 | `TokenUsageEntry`（Pydantic，`core/schema/token.py`） | timestamp: IsoTimestamp，字段与 dataclass 1:1 重复 |
| 消息嵌入 | `TokenUsageRecord`（Pydantic） | 与 Entry 几乎重复 |
| 传输聚合 | `TokenUsageSummary`（Pydantic） | rounds + turn_total |
| 前端 | `TokenRound`/`TokenStats`/`TokenUsage`/`TokenUsageMessage`（TS） | 手动 `by_model`/`byModel` 双兼容（m18 已踩坑） |
| LLM 原始 | `usage: dict`（无 schema） | `record()` 裸 key 索引 `usage["prompt_tokens"]`，无验证 |

实害：timestamp 类型漂移（float vs ISO）、序列化分裂（`asdict()` vs `model_dump(mode="json")`）、provider 字段改名直接 KeyError、前端契约漂移。

## 设计决策

| 决策 | 结论 |
|------|------|
| 字段形态 | `input_tokens`/`output_tokens`/`total_tokens` + 可选 `cached_tokens`/`reasoning_tokens`（全明细） |
| 旧数据 | 不兼容，直接改名。旧 Mongo 文档/旧 pending 行 validate 失败时跳过，自然老化 |
| timestamp | `IsoTimestamp`（`core/schema/_utils.py` 已有，自动兼容旧 float Unix 输入） |
| 范围 | 全链路：权威模型 + 后端各层统一 + LLM 解析 + 前端 + 测试 |

## 目标结构

### 1. 权威模型 — `core/schema/token.py`

```python
class TokenUsage(BaseModel):
    """单次 LLM API 调用的 token 消耗。全系统唯一权威定义。"""
    model: str
    session_id: SessionId
    round_num: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int | None = None        # input 中缓存命中
    reasoning_tokens: int | None = None     # output 中思考 token
    timestamp: IsoTimestamp = Field(default_factory=now_iso)

    @classmethod
    def from_api(cls, usage: dict, *, model, session_id, round_num) -> "TokenUsage":
        """从 LLM 层归一化 usage dict 构造（含字段校验）。"""
```

**删除**：`TokenUsage`（dataclass，`token_tracker.py`）、`TokenUsageEntry`、`TokenUsageRecord`。
**保留**：`TokenUsageSummary`（`rounds: list[TokenUsage]` + `turn_total`，内嵌 `AssistantMessage.token_usage`，结构不变）。

> `session_id` 入模型：MongoDB 持久化必须带 session，而"统一为一个模型"优先于"内存瘦身"。WS/调试只显示它不用的字段，无害。

### 2. LLM 解析层 — `Module/llm/openai_compat.py`

三处 usage 提取（两处流式 + 一处非流式）抽为统一 helper，新增字段提取：

| 新字段 | 来源 |
|--------|------|
| `input_tokens` | `u.prompt_tokens` |
| `output_tokens` | `u.completion_tokens` |
| `cached_tokens` | `u.prompt_tokens_details.cached_tokens`（无则 None） |
| `reasoning_tokens` | `u.completion_tokens_details.reasoning_tokens`（无则 None） |
| `total_tokens` | `u.total_tokens` |

### 3. TokenTracker — `core/monitoring/token_tracker.py`

- `record()` 内部经 `TokenUsage.from_api(...)` 构造，内存累计 / WS 广播 / 持久化全部基于同一模型
- WS 广播 payload 用 `model_dump(mode="json")`（不再 `asdict()`）
- `_persist` 直接 `repo.record(TokenUsage)`；pending JSONL 写 `model_dump(mode="json")`
- `flush_pending` 用 `TokenUsage.model_validate`，旧格式行 try/except 跳过

### 4. Repo — `core/storage/token_repo.py`

- `record(entry: TokenUsage)` / `flush(entries: list[TokenUsage])` / `query_by_session` 返回 `list[TokenUsage]`
- `query_by_session` 对旧文档 `model_validate` 失败跳过（不兼容，自然老化）
- 聚合查询（`total_tokens_by_session`/`total_tokens_by_model`）不动——`total_tokens` 字段仍存在

### 5. 消费者

| 位置 | 改动 |
|------|------|
| `core/cognitive/loop.py` | 构造 `TokenUsageSummary` 处换新模型，其余透传 |
| `core/input/ws_handler.py` | `get_session_stats` 返回结构不变（`{total, by_model}`） |
| `core/debug/info.py` | `current_turn` 改为 `[u.model_dump() for u in ...]`，新字段自动带出 |
| `core/tools/runtime_config/plugin.py` | 只读 `session_total`/`per_model`，不动 |
| `frontend/src/types/protocol.ts` | `TokenRound` → `input_tokens/output_tokens/cached_tokens?/reasoning_tokens?`；删 `byModel` 双兼容（`TokenStats.by_model` 单一） |
| `frontend/src/components/chat/TokenLabel.vue` | 显示 In/Out，有值时显示 cached/reasoning |
| `frontend/src/debug/types.ts` | `TokenRoundItem` 同步 |

### 6. 测试

- `Module/llm/tests/` — openai_compat 新字段提取（cached/reasoning，流式 + 非流式）
- `core/monitoring/tests/test_token_tracker.py` — record/persist/flush 换新模型、字段改名
- `core/storage/tests/test_token_repo.py` — legacy 文档跳过
- 前端 TS 编译 + TokenLabel 显示

## 错误处理

- 旧 Mongo 文档：`query_by_session` validate 失败 → 跳过（不迁移、不清库、不崩）
- 旧 pending 行：`flush_pending` validate 失败 → 跳过
- `cached_tokens`/`reasoning_tokens`：provider 未返回即 None，前端有值才显示

## 不做

- 不做 cost 估算（本次只定数据结构，cost 字段/单价留待后续）
- 不写 MongoDB 迁移脚本、不清空 `token_usage` 集合
- 不改 WS `token_usage` 消息的外层 envelope 结构（`turn`/`session` 形状不变）
- 不改 `get_session_stats` 返回结构

## 验证

```bash
# 权威模型唯一性：无残留 dataclass TokenUsage / 旧字段名
grep -rn "prompt_tokens\|completion_tokens" core/ Module/llm/ --include="*.py" | grep -v test | grep -v __pycache__
# 预期：仅剩 LLM SDK 对象属性访问（u.prompt_tokens 等），无结构定义

# 前端无 byModel 双兼容
grep -rn "byModel" frontend/src/
# 预期空输出

make test-quick
```
