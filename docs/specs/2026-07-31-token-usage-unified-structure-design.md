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
| 旧数据 | 不兼容，直接改名。token_usage 集合/旧 pending 行 validate 失败时跳过；**消息内嵌路径（会话上下文）用加载点降级**——旧 token_usage 剥除、消息正文保留，避免整批加载失败丢会话 |
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
        """从 LLM 层归一化 usage dict 构造（含字段校验）。

        usage 键名契约（由 LLM 层 _extract_usage 产出）：
        {"input_tokens": int, "output_tokens": int, "total_tokens": int,
         "cached_tokens": int | None, "reasoning_tokens": int | None}
        契约语义：cached_tokens/reasoning_tokens 为可选键，provider 未返回时
        _extract_usage **可省略该键**；from_api 必须用 `.get()` 读取可选键
        （禁止方括号索引，否则省略键时 KeyError）。仅作校验 + 补
        model/session_id/round_num/timestamp。
        """
```

**删除**：`TokenUsage`（dataclass，`token_tracker.py`）、`TokenUsageEntry`、`TokenUsageRecord`。
**保留**：`TokenUsageSummary`（`rounds: list[TokenUsage]` + `turn_total`，内嵌 `AssistantMessage.token_usage`）。外层 envelope 不变，但 `rounds` 元素从 `TokenUsageRecord` 换为 `TokenUsage`——新增必填 `session_id`、字段改名 input/output、timestamp 改为 ISO。

> `session_id` 入模型：MongoDB 持久化必须带 session，而"统一为一个模型"优先于"内存瘦身"。WS/调试只显示它不用的字段，无害。

### 2. LLM 解析层 — `Module/llm/openai_compat.py`

三处 usage 提取（两处流式 + 一处非流式）抽为统一 helper `_extract_usage(u) -> dict`，产出第 1 节定义的 usage dict 契约：

| 新字段 | 来源 |
|--------|------|
| `input_tokens` | `u.prompt_tokens or 0` |
| `output_tokens` | `u.completion_tokens or 0` |
| `cached_tokens` | `getattr(u.prompt_tokens_details, "cached_tokens", None)`（无则 None） |
| `reasoning_tokens` | `getattr(u.completion_tokens_details, "reasoning_tokens", None)`（无则 None） |
| `total_tokens` | `u.total_tokens or 0` |

> **`or 0` 归一化必须保留**：现网三处提取点均为 `u.prompt_tokens or 0`，正是为「provider 返回 None」而设。`input_tokens`/`output_tokens`/`total_tokens` 是必填 int，若裸取值 None，`from_api` 校验失败，且 [loop.py:461-467](core/cognitive/loop.py#L461) 的 `record()` 调用无 try/except，异常会从流式循环向上抛。统一 helper 保留行为语义。

**helper 边界（防 M6 回归）**：`_extract_usage` **只接收 SDK usage 对象做字段映射**，不碰 chunk 遍历 / `choices` 空数组哨兵 / `is not None` 守卫——三处调用点各自保留原有的 chunk 层守卫（`_stream_structured` 的 truthy 检查与 `_call_api` 的 `not choices` 哨兵不对称是 M6 修复的核心，不得并入 helper）。

**details Optional 防护**：openai SDK 的 `prompt_tokens_details`/`completion_tokens_details` 为 Optional，Ollama 等 provider 不返回——必须 `getattr(u.prompt_tokens_details, "cached_tokens", None)` 式守卫。**禁止** `(u.prompt_tokens_details or {}).cached_tokens`（None 时 `{}.cached_tokens` 抛 AttributeError，恰在防护场景崩溃）也**禁止** `.get()`（details 是 Pydantic 对象非 dict）。

其他 backend（ollama/mimo/custom）均继承 `openai_compat` 或复用其流式路径，不单独处理 usage。

### 2b. 消息内嵌路径：加载点降级（会话上下文保护）

`TokenUsageSummary` 内嵌在 `AssistantMessage.token_usage` 随会话文档/归档持久化。旧会话消息含 `prompt_tokens`/`completion_tokens` 字段，新模型下整条消息校验失败。当前 `session_repo.load` / `session.load_from_jsonl` 是**整批 `TypeAdapter[Message]` 校验、无 per-message try/except**（[session_repo.py:150](core/storage/session_repo.py#L150)、[session.py:313](core/memory/session.py#L313)）——单条消息失败即整个会话加载失败，回退 JSONL 同样失败，内存变空会话，下次 `save()` 覆盖旧文档 → **旧会话上下文永久丢失**。

**方案**：`core/schema/adapters/message_adapter.py` 新增 `validate_message_legacy_compat(data: dict) -> Message`：

```python
from pydantic import TypeAdapter, ValidationError  # 需补 ValidationError（现文件仅导 TypeAdapter）

def validate_message_legacy_compat(data):
    """校验消息 dict → Message；旧格式 token_usage 剥除后重试。

    旧会话/归档消息的 token_usage 为 prompt_tokens/completion_tokens 旧字段，
    新模型无法校验。剥除 token_usage（置 None）保留消息正文，避免整批加载
    失败导致会话上下文丢失。token_usage 不存在或重试仍失败时抛原异常。
    """
    try:
        return _MESSAGE_ADAPTER.validate_python(data)
    except ValidationError:
        if isinstance(data, dict) and "token_usage" in data:
            try:
                return _MESSAGE_ADAPTER.validate_python({**data, "token_usage": None})
            except ValidationError:
                pass
        raise  # 重新抛出原始异常
```

**三处加载点统一改用该 helper**。`_MESSAGE_ADAPTER: TypeAdapter[Message]` 与 `validate_message_legacy_compat` 均定义在 `message_adapter.py`（`message.py` 不依赖 adapters，无循环 import）；三处加载点删除各自本地 `TypeAdapter[Message]`，改为 `from core.schema.adapters.message_adapter import validate_message_legacy_compat`：

| 加载点 | 现状 | 改为 |
|--------|------|------|
| `core/storage/session_repo.py:22,150` | 本地 `_message_adapter` + `[_message_adapter.validate_python(m) for m in msgs_raw]` | 删本地 adapter，`[validate_message_legacy_compat(m) for m in msgs_raw]` |
| `core/memory/session.py:44,313` | 同上（JSONL 恢复） | 同上 |
| `core/storage/message_repo.py:18,34` | 本地 `_message_adapter` + `validate_python(doc)`（归档读取） | 删本地 adapter，`validate_message_legacy_compat(doc)` |

> 新写入的会话无此开销：helper 仅在校验失败时才做第二次剥除重试，新格式消息首次校验即过。

### 3. TokenTracker — `core/monitoring/token_tracker.py`

- `record()` 内部经 `TokenUsage.from_api(usage, model=model, session_id=self.session_id, round_num=round_num)` 构造（session_id 取 `token_tracker.py:69` 的 `self.session_id`），内存累计 / WS 广播 / 持久化全部基于同一模型
- WS 广播 payload 用 `model_dump(mode="json")`（不再 `asdict()`）
- `_persist` 直接 `repo.record(TokenUsage)`；pending JSONL 写 `model_dump(mode="json")`
- `flush_pending` 用 `TokenUsage.model_validate`，旧格式行 try/except 跳过且**不保留**——整文件导入成功后删除（与"不兼容直接改名、旧数据自然老化"一致，跳过行随文件删除永久丢弃）

### 4. Repo — `core/storage/token_repo.py`

- `record(entry: TokenUsage)` / `flush(entries: list[TokenUsage])` / `query_by_session` 返回 `list[TokenUsage]`
- `query_by_session` 对旧文档 `model_validate` 失败跳过（不兼容，自然老化）
- 聚合查询（`total_tokens_by_session`/`total_tokens_by_model`）不动——`total_tokens` 字段仍存在

### 5. 消费者

| 位置 | 改动 |
|------|------|
| `core/cognitive/loop.py` | [L701-711](core/cognitive/loop.py#L701) 的手工 `TokenUsageRecord(model=r.model, prompt_tokens=...)` 映射循环**删除**，`rounds` 直接透传 `self._token_tracker.current_turn_usages`（已是 `list[TokenUsage]`，session_id 由 `record()` 注入）。不变式：round 与 summary 同属 tracker.session_id，与消息同会话必然一致 |
| `core/input/ws_handler.py` | `get_session_stats` 返回结构不变（`{total, by_model}`） |
| `core/debug/info.py` | `current_turn` 改为 `[u.model_dump() for u in ...]`，新字段自动带出 |
| `core/tools/runtime_config/plugin.py` | 只读 `session_total`/`per_model`，不动 |
| `frontend/src/types/protocol.ts` | `TokenRound` → `input_tokens/output_tokens/cached_tokens?/reasoning_tokens?`；`timestamp?: number` → `string`（后端 model_dump 产 ISO 字符串）；`TokenStats.byModel?:` **改名单字段**为 `by_model: Record<string, number>`（后端唯一键，非"删兼容"） |
| `frontend/src/stores/chatStore.ts` | `sessionTokens` 内部类型 `byModel` → `by_model`（L41）；`updateTokenUsage` 三处：**L202 as-cast 类型注解删 `byModel?:`**、**L203 赋值键 `byModel:` → `by_model:`**、双兼容 `s.by_model || s.byModel` → 只读 `s.by_model`；**接通 `restoreFromServer`（L241 死参数 `_tokenStats`）**——session 消息的 `token_stats`（ws_handler `get_session_stats` 产出）写入 `sessionTokens`，消除刷新/重连后 CharStatus token 归零直到下次广播的缺口 |
| `frontend/src/components/character/CharStatus.vue` | `st.byModel` → `st.by_model`（L17） |
| `frontend/src/components/chat/TokenLabel.vue` | 显示 In/Out，有值时显示 cached/reasoning |
| `frontend/src/debug/types.ts` | `TokenRoundItem` 同步 input/output |
| `core/schema/__init__.py` | re-export 改 `TokenUsage, TokenUsageSummary`（删 TokenUsageEntry/TokenUsageRecord） |
| `core/monitoring/__init__.py` | `from .token_tracker import TokenTracker`（删 dataclass TokenUsage re-export） |
| `core/schema/README.md` | `token.py` 行（L19/L47）更新为 TokenUsage |
| `core/monitoring/README.md` | TokenUsageRecord/TokenUsage 引用（L48/L59）更新 |
| `core/adapter/llm_adapter.py` | `stream_think`（[L96](core/adapter/llm_adapter.py#L96)，**方法名是 stream_think 非 stream_chat**）docstring 的 usage 键契约（L102-103）更新为 input/output |
| `Module/llm/protocol.py` | `stream_think`（[L45](Module/llm/protocol.py#L45)，非 stream_thinking）docstring 的 usage 键契约（L52）更新为 input/output |
| `docs/data-structure-survey.md` | `TokenUsage` 记录（L42）与 usage dict 样例（L342-343）更新（非 README，readme-conventions 不强制，但避免文档与实现漂移） |
| `docs/database-storage.md` | `token_usage` 集合字段表（L111-112）更新 |
| `docs/frontend-api.md` | WS `token_usage` 消息样例（L84, L447-448）更新 |

### 6. 测试

- `Module/llm/tests/` — `_extract_usage` 新字段提取（cached/reasoning，流式 + 非流式）；**details 缺失（Ollama 风格）→ None 不抛**；`test_llm.py:547,595` 的 last_usage 全 dict 相等断言（现期望 3 键 `{prompt_tokens,...}`）更新为 5 键 `{input_tokens,...}`（或省略 None 键的变体）。**⚠ fixture 必须同步设 `usage_obj.prompt_tokens_details = None` / `completion_tokens_details = None`**——现测试 fixture（[test_llm.py:513-516,579-582](Module/llm/tests/test_llm.py#L513)）用裸 `MagicMock()`，`getattr` 守卫在 MagicMock 上返回子 MagicMock 而非 None，5 键与省略 None 两种断言变体都会失败；**新增 M6 守卫不对称回归测试**：`_call_api` 流式路径（[openai_compat.py:200-210](Module/llm/openai_compat.py#L200)）构造「空 choices + usage chunk」chunk 序列，断言产出 usage 不抛 IndexError、且空 choices 无 content 不产出 delta——现 `TestUsageTracking` 仅覆盖 truthy 路径与非流式，该场景零覆盖，重构守卫或改顺序时无拦截。**放 `test_llm.py` 的 `TestUsageTracking` 类**（模块级 `pytestmark = pytest.mark.service` 自动继承，勿新建无 marker 文件，否则漏出 `make test-quick` 覆盖）
- `core/monitoring/tests/test_token_tracker.py` — record/persist/flush 换新模型、字段改名
- `core/storage/tests/test_token_repo.py` — legacy 文档跳过
- `core/tests/`（或 session/message_repo 现有测试）— `validate_message_legacy_compat`：旧 token_usage 消息剥除后保留正文；无 token_usage 消息直接过；剥除后仍失败抛原异常
- 前端 TS 编译 + TokenLabel 显示

## 错误处理

| 数据面 | 策略 |
|--------|------|
| token_usage 集合旧文档 | `query_by_session` validate 失败 → 跳过（不迁移、不清库、不崩） |
| pending 文件旧行 | `flush_pending` validate 失败 → 跳过 |
| **消息内嵌旧 token_usage** | **加载点降级：剥 token_usage、保留消息正文**（防会话上下文丢失） |
| `cached_tokens`/`reasoning_tokens` | provider 未返回即 None，前端有值才显示 |

## 不做

- 不做 cost 估算（本次只定数据结构，cost 字段/单价留待后续）
- 不写 MongoDB 迁移脚本、不清空 `token_usage` 集合
- 不改 WS `token_usage` 消息的外层 envelope 结构（`turn`/`session` 形状不变）
- 不改 `get_session_stats` 返回结构
- 不改 `Module/task_system/schema/output.py` 的 `TokenUsage`（[output.py:239](Module/task_system/schema/output.py#L239)，任务级 token 明细，同名不同实体，范围外）

## 验证

```bash
# 权威模型唯一性：core/ 下无旧字段结构定义（LLM 层 openai_compat 的属性访问 u.prompt_tokens
# 与 docstring 契约属于 Module/llm，不在本 grep 范围，且属性访问本身不可消除）
# 例外：message_adapter.py 的 validate_message_legacy_compat docstring 逐字描述旧字段
# （"prompt_tokens/completion_tokens 旧字段"），属本设计要求保留的说明性文本，予以排除。
grep -rn "prompt_tokens\|completion_tokens" core/ --include="*.py" \
  | grep -v test | grep -v __pycache__ | grep -v "message_adapter.py"
# 预期空输出

# 前端 byModel 字段全部改名 by_model（无残留）
grep -rn "byModel" frontend/src/
# 预期空输出

make test-quick
```
