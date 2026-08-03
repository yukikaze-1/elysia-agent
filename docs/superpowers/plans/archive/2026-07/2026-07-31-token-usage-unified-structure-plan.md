# Token 用量统一数据结构 + input/output 区分 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将「一次 LLM 调用」的 token 记录从 6 套定义统一为单一权威 Pydantic 模型 `TokenUsage`（input/output 语义 + cached/reasoning 全明细），全链路（LLM 解析 → TokenTracker → Repo → 消息嵌入 → 前端）改用该模型。

**Architecture:** `core/schema/token.py` 定义唯一模型（含 `from_api` 契约），LLM 层 `_extract_usage` 归一化 provider usage → 新键名 dict，TokenTracker/Repo/loop/debug 全部 `model_dump()` 序列化；旧数据不迁移——token_usage 集合/旧 pending 行跳过，消息内嵌路径用加载点降级保护会话上下文。

**Tech Stack:** Pydantic v2 (BaseModel/TypeAdapter/Field), OpenAI-compatible SDK, Vue 3 + TypeScript, pytest (unit/service markers), MongoDB motor。

## Global Constraints

- 权威模型：`TokenUsage`（`model/session_id/round_num/input_tokens/output_tokens/total_tokens` + 可选 `cached_tokens/reasoning_tokens`，`timestamp: IsoTimestamp`）。`from_api(usage, *, model, session_id, round_num)` 对可选键用 `.get()`（禁方括号，防省略键 KeyError）。
- `or 0` 归一化必须保留：`u.prompt_tokens or 0` 等，防 provider 返回 None 时必填 int 校验失败。
- `_extract_usage` 只做字段映射，不碰 chunk 遍历 / `choices` 空数组哨兵 / `is not None` 守卫（防 M6 回归，三处调用点守卫各自保留）。
- details 守卫必须 `getattr(u, "prompt_tokens_details", None)` 式；**禁止** `(u.xxx_details or {}).attr`（None 时 `{}.attr` AttributeError）与 `.get()`（details 是 Pydantic 对象非 dict）。
- 旧数据不兼容、不迁移、不清库：token_usage 集合/旧 pending 行跳过；消息内嵌旧 token_usage 用 `validate_message_legacy_compat` 剥除（保留消息正文）。
- 前端 `TokenStats.by_model` 单字段（删 `byModel` 双兼容）。
- 不改 `Module/task_system/schema/output.py` 的 `TokenUsage`（任务级，同名不同实体，范围外）。
- 代码注释用中文；README 用中文。

---

### Task 1: 权威模型 + 消息加载点降级

**Files:**
- Modify: `core/schema/token.py`
- Modify: `core/schema/__init__.py:95`
- Modify: `core/schema/adapters/message_adapter.py`
- Modify: `core/storage/session_repo.py:7,13,21-22,150`
- Modify: `core/storage/message_repo.py:6,17-18,34`
- Modify: `core/memory/session.py:18,44,313`
- Create: `core/schema/tests/test_token.py`
- Create: `core/schema/tests/test_message_degradation.py`

**Interfaces:**
- Produces: `core.schema.token.TokenUsage`（含 `from_api` classmethod）+ `TokenUsageSummary`（`rounds: list[TokenUsage]`）。`core.schema.adapters.message_adapter.validate_message_legacy_compat(data: dict) -> Message`。下游 Task 2 的 `_extract_usage` 产出 dict 需符合 `from_api` 的键名契约；Task 3 的 TokenTracker/Repo 使用 `TokenUsage`。

**背景：** 本任务是数据模型与其旧数据保护的一体变更——只改模型不接降级会让所有含 token_usage 的会话加载失败（当前数据也是 prompt/completion 格式）。本任务完成后 core 下旧结构消费者（loop.py:58、token_repo、token_tracker、测试）会 ImportError，属预期，Task 3 修复。

- [ ] **Step 1: 重写 `core/schema/token.py`**

```python
"""Token 用量记录类型 — Pydantic BaseModel。

单一权威定义，各层（内存/持久化/消息嵌入/WS 传输）统一使用。
"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


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
        _extract_usage 可省略该键；from_api 统一用 .get() 读取所有键
        （必填键缺失 → None → Pydantic ValidationError，契约违约即校验失败，
        而非 KeyError）。仅作校验 + 补 model/session_id/round_num/timestamp。
        """
        return cls(
            model=model,
            session_id=session_id,
            round_num=round_num,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            cached_tokens=usage.get("cached_tokens"),
            reasoning_tokens=usage.get("reasoning_tokens"),
        )


class TokenUsageSummary(BaseModel):
    """一次 Assistant 回复的 token 用量汇总。嵌入 AssistantMessage.token_usage。"""
    rounds: list[TokenUsage] = Field(default_factory=list)
    turn_total: int = 0
```

> 删除 `TokenUsageEntry`、`TokenUsageRecord`（被 `TokenUsage` 取代）。`IsoTimestamp` 自动兼容旧 float Unix 输入（`_normalize_iso_ts`），无需额外转换。

- [ ] **Step 2: 更新 `core/schema/__init__.py:95`**

```python
# ── Token ──
from core.schema.token import TokenUsage, TokenUsageSummary  # noqa: F401
```

- [ ] **Step 3: 写 `core/schema/tests/test_token.py`（failing）**

```python
"""TokenUsage 权威模型测试。"""

import pytest
from pydantic import ValidationError

from core.schema.token import TokenUsage, TokenUsageSummary
from core.schema.types import SessionId

pytestmark = pytest.mark.unit  # 无 marker 会被 make test-quick（-m "unit or service"）静默跳过


def test_from_api_full_keys():
    u = TokenUsage.from_api(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 30,
            "reasoning_tokens": 10,
        },
        model="deepseek/deepseek-chat",
        session_id=SessionId("default"),
        round_num=1,
    )
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.total_tokens == 150
    assert u.cached_tokens == 30
    assert u.reasoning_tokens == 10
    assert u.model == "deepseek/deepseek-chat"
    assert u.session_id == "default"
    assert u.round_num == 1


def test_from_api_optional_keys_omitted():
    """可选键缺失（provider 未返回）→ 对应字段 None。"""
    u = TokenUsage.from_api(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        model="ollama/llama3",
        session_id=SessionId("default"),
        round_num=0,
    )
    assert u.cached_tokens is None
    assert u.reasoning_tokens is None


def test_from_api_missing_required_key_raises():
    """必填键缺失 → ValidationError。"""
    with pytest.raises(ValidationError):
        TokenUsage.from_api(
            {"input_tokens": 10, "total_tokens": 15},
            model="ollama/llama3",
            session_id=SessionId("default"),
            round_num=0,
        )


def test_timestamp_accepts_legacy_float():
    """timestamp 兼容旧 float Unix 输入（IsoTimestamp 归一化）。"""
    u = TokenUsage(
        model="m",
        session_id=SessionId("default"),
        round_num=0,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        timestamp=1780000000.0,
    )
    assert isinstance(u.timestamp, str)
    assert u.timestamp.startswith("2026-")


def test_summary_rounds_accept_token_usage():
    s = TokenUsageSummary(
        rounds=[TokenUsage.from_api(
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            model="m", session_id=SessionId("default"), round_num=0,
        )],
        turn_total=2,
    )
    assert s.rounds[0].total_tokens == 2
```

- [ ] **Step 4: 运行验证失败**

```bash
python -m pytest core/schema/tests/test_token.py -v
```
预期：`ModuleNotFoundError: No module named 'core.schema.token'` 或导入旧类型失败（token.py 尚未含 TokenUsage）。

- [ ] **Step 5: 在 `core/schema/adapters/message_adapter.py` 增加降级 helper**

文件当前 import（L14）：`from pydantic import TypeAdapter`。改为：
```python
from pydantic import TypeAdapter, ValidationError
```
并在 `from ..part import ...` 之后追加：
```python
from ..message import Message
```
在 `_PARTS_ADAPTER`（L21）定义后新增：
```python
# ── Message 校验 + 旧 token_usage 降级 ──────────────────

_MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(Message)


def validate_message_legacy_compat(data: dict) -> Message:
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

- [ ] **Step 6: 三处加载点改用 helper**

`core/storage/session_repo.py`：
- 删除 L21-22 的 `_message_adapter: TypeAdapter[Message] = TypeAdapter(Message)`（及注释）
- 删除 import `from pydantic import TypeAdapter`；若 `Message`（L13）仅用于该 adapter 定义则一并删除，否则保留
- 新增 `from core.schema.adapters.message_adapter import validate_message_legacy_compat`
- L150 改为 `messages = [validate_message_legacy_compat(m) for m in msgs_raw]`

`core/storage/message_repo.py`：
- 删除 L17-18 的 `_message_adapter` 定义 + `from pydantic import TypeAdapter`
- 新增 `from core.schema.adapters.message_adapter import validate_message_legacy_compat`
- L34 改为 `msg = validate_message_legacy_compat(doc)`

`core/memory/session.py`：
- 删除 L44 的 `_message_adapter` 定义 + `from pydantic import TypeAdapter`（L18）
- 新增 `from core.schema.adapters.message_adapter import validate_message_legacy_compat`
- L313 改为 `converted = [validate_message_legacy_compat(m) for m in msgs_raw]`

- [ ] **Step 7: 写 `core/schema/tests/test_message_degradation.py`（failing）**

```python
"""validate_message_legacy_compat — 旧 token_usage 消息降级测试。"""

import pytest
from pydantic import ValidationError

from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.message import AssistantMessage

pytestmark = pytest.mark.unit  # 无 marker 会被 make test-quick（-m "unit or service"）静默跳过


def _legacy_assistant() -> dict:
    """构造含旧格式 token_usage（prompt_tokens/completion_tokens）的 AssistantMessage dict。"""
    return {
        "role": "assistant",
        "content": [{"type": "text", "content": "你好"}],
        "message_id": "m1",
        "session_id": "default",
        "timestamp": "2026-07-01T00:00:00+00:00",
        "token_usage": {
            "rounds": [{
                "model": "deepseek/deepseek-chat",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "round_num": 0,
                "timestamp": 1780000000.0,
            }],
            "turn_total": 150,
        },
    }


def test_legacy_token_usage_stripped_message_preserved():
    """旧格式 token_usage 被剥除，消息正文保留。"""
    msg = validate_message_legacy_compat(_legacy_assistant())
    assert isinstance(msg, AssistantMessage)
    assert msg.content[0].content == "你好"
    assert msg.token_usage is None


def test_normal_message_passes_through():
    """无 token_usage 消息直接校验通过。"""
    d = _legacy_assistant()
    d["token_usage"] = None
    msg = validate_message_legacy_compat(d)
    assert msg.token_usage is None


def test_new_format_token_usage_passes():
    """新格式 token_usage 不触发剥除。"""
    d = _legacy_assistant()
    d["token_usage"] = {
        "rounds": [{
            "model": "deepseek/deepseek-chat",
            "session_id": "default",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "round_num": 0,
            "timestamp": "2026-07-01T00:00:00+00:00",
        }],
        "turn_total": 150,
    }
    msg = validate_message_legacy_compat(d)
    assert msg.token_usage is not None
    assert msg.token_usage.rounds[0].input_tokens == 100


def test_broken_message_raises_original_error():
    """token_usage 剥除后仍失败 → 抛原始异常（不吞掉）。"""
    d = _legacy_assistant()
    d["content"] = "not-a-list"  # content 类型错误，剥 token_usage 后仍失败
    with pytest.raises(ValidationError):
        validate_message_legacy_compat(d)
```

- [ ] **Step 8: 运行两个测试文件确认通过**

```bash
python -m pytest core/schema/tests/test_token.py core/schema/tests/test_message_degradation.py -v
```
预期：全部 PASS（旧格式消息被剥除、新格式直通、必填校验失败、timestamp 兼容 float）。

- [ ] **Step 9: 提交**

```bash
git add core/schema/token.py core/schema/__init__.py core/schema/adapters/message_adapter.py \
  core/storage/session_repo.py core/storage/message_repo.py core/memory/session.py \
  core/schema/tests/test_token.py core/schema/tests/test_message_degradation.py
git commit -m "feat(token): TokenUsage 权威模型 + 消息加载点降级保护旧会话"
```

> 预期本任务提交后 `make test-quick` 大量 FAIL——loop.py:58 / token_repo / token_tracker / 相关测试仍引用旧类型，Task 3 统一修复。继续 Task 2。

---

### Task 2: LLM 解析层 `_extract_usage`

**Files:**
- Modify: `Module/llm/openai_compat.py`
- Modify: `Module/llm/tests/test_llm.py`
- Modify: `Module/llm/protocol.py:52`
- Modify: `core/adapter/llm_adapter.py:102-103`

**Interfaces:**
- Consumes: Task 1 的 `TokenUsage.from_api` 键名契约（本任务产出该契约的 dict）。
- Produces: 模块级 `_extract_usage(u) -> dict`，三处调用点与 `last_usage` 均改用之。产出 dict 键：`input_tokens/output_tokens/total_tokens/cached_tokens/reasoning_tokens`。

- [ ] **Step 1: 在 `openai_compat.py` 增加 `_extract_usage`**

在类定义之前（import 之后）新增模块级函数：
```python
def _extract_usage(u) -> dict:
    """从 SDK usage 对象提取归一化 token 消耗 dict（input/output 语义）。

    产出契约（供 TokenUsage.from_api 消费）：
    {"input_tokens": int, "output_tokens": int, "total_tokens": int,
     "cached_tokens": int | None, "reasoning_tokens": int | None}
    cached/reasoning 始终带键，provider 未返回时值为 None（or 0 归一化保留，
    防必填 int 校验失败；getattr 守卫防 details Optional 下 AttributeError）。
    仅做字段映射，不碰 chunk 遍历 / choices 哨兵 / is-not-None 守卫（防 M6 回归）。
    """
    cached = getattr(u, "prompt_tokens_details", None)
    reasoning = getattr(u, "completion_tokens_details", None)
    return {
        "input_tokens": u.prompt_tokens or 0,
        "output_tokens": u.completion_tokens or 0,
        "total_tokens": u.total_tokens or 0,
        "cached_tokens": cached.cached_tokens if cached is not None else None,
        "reasoning_tokens": reasoning.reasoning_tokens if reasoning is not None else None,
    }
```

- [ ] **Step 2: 替换三处 usage 提取点**

`_stream_structured`（L130-138）：
```python
            # 流式最后一个 chunk 可能携带 usage 统计（choices 通常为空）
            if getattr(chunk, "usage", None):
                self.last_usage = _extract_usage(chunk.usage)
                yield {"content": None, "tool_calls": None, "usage": self.last_usage}
                continue  # usage chunk 没有实质内容，跳过后续处理
```

`_call_api` 流式分支（L203-210）——**保留 M6 守卫顺序与理由注释**（`not chunk.choices` 哨兵 + `getattr(...) is not None`），只替换赋值：
```python
                # 先判 choices 空：usage chunk 的 choices 恒为空数组（真实 API 契约），
                # 不能先访问 chunk.choices[0] 否则 IndexError；也不能用 getattr(usage)
                # 判真——MagicMock 下恒真。空 choices 也可能是 provider 流结束哨兵。
                if not chunk.choices:
                    if getattr(chunk, "usage", None) is not None:
                        self.last_usage = _extract_usage(chunk.usage)
                    continue  # usage chunk / 空 choices 流结束哨兵，均无实质内容
```

`_call_api` 非流式分支（L227-232）：
```python
            if getattr(response, "usage", None):
                self.last_usage = _extract_usage(response.usage)
```

- [ ] **Step 3: 更新 `test_llm.py` 的 TestUsageTracking**

`test_stream_structured_yields_usage_chunk`（L509）：fixture 补 `usage_obj.prompt_tokens_details = None` / `usage_obj.completion_tokens_details = None`；断言改：
```python
        assert chunks[1] == {
            "content": None,
            "tool_calls": None,
            "usage": {
                "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                "cached_tokens": None, "reasoning_tokens": None,
            },
        }
        assert llm_backend.last_usage == {
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cached_tokens": None, "reasoning_tokens": None,
        }
```
`test_stream_structured_no_usage_when_none`（L550）：不变。
`test_call_api_nonstreaming_captures_usage`（L575）：fixture 补两处 details=None；断言改：
```python
        assert llm_backend.last_usage == {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "cached_tokens": None, "reasoning_tokens": None,
        }
```

> ⚠ 若漏设 `details = None`，裸 MagicMock 的 `getattr(u, "prompt_tokens_details", None)` 返回子 MagicMock，`cached_tokens` 变成 MagicMock，断言必失败。

- [ ] **Step 4: 新增 M6 守卫回归测试（同 `TestUsageTracking` 类内）**

```python
    @pytest.mark.asyncio
    async def test_call_api_streaming_empty_choices_usage_chunk(self, llm_backend):
        """_call_api 流式路径：空 choices + usage chunk 不抛 IndexError、捕获 usage、跳过 delta。

        M6 回归：不能先访问 chunk.choices[0]（usage chunk 的 choices 恒空，会 IndexError）。
        """
        from unittest.mock import MagicMock, AsyncMock

        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 100
        usage_obj.completion_tokens = 50
        usage_obj.total_tokens = 150
        usage_obj.prompt_tokens_details = None
        usage_obj.completion_tokens_details = None

        delta = MagicMock()
        delta.content = "hello"

        chunk_usage = MagicMock()
        chunk_usage.choices = []  # 空 choices —— 若先访问 choices[0] 会 IndexError（M6）
        chunk_usage.usage = usage_obj

        chunk_content = MagicMock()
        chunk_content.choices = [MagicMock(delta=delta)]
        chunk_content.usage = None

        async def async_iter():
            yield chunk_usage
            yield chunk_content

        llm_backend._client.chat.completions.create = AsyncMock(return_value=async_iter())

        chunks = []
        async for chunk in llm_backend.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert chunks == ["hello"]  # usage chunk 无内容跳过
        assert llm_backend.last_usage == {
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cached_tokens": None, "reasoning_tokens": None,
        }
```

> `chat_stream`（L85）走 `_call_api(stream=True)`，即 M6 守卫所在路径。该测试放 `TestUsageTracking` 类（模块级 `pytestmark = pytest.mark.service` 自动继承）。

- [ ] **Step 5: 更新两处契约 docstring**

`Module/llm/protocol.py` L51-52：
```python
          {"content": None, "tool_calls": None, "usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int, "cached_tokens": int | None, "reasoning_tokens": int | None}}
```
`core/adapter/llm_adapter.py` L102-103：
```python
          {"content": None, "tool_calls": None, "usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int, "cached_tokens": int | None, "reasoning_tokens": int | None}}
```

- [ ] **Step 6: 运行测试确认**

```bash
python -m pytest Module/llm/tests/test_llm.py -v
```
预期：TestUsageTracking 全部 PASS（含新增 M6 回归），其余不变。

- [ ] **Step 7: 提交**

```bash
git add Module/llm/openai_compat.py Module/llm/tests/test_llm.py \
  Module/llm/protocol.py core/adapter/llm_adapter.py
git commit -m "feat(token): _extract_usage 归一化 input/output + M6 守卫回归测试"
```

---

### Task 3: TokenTracker + TokenRepository + 后端消费者

**Files:**
- Modify: `core/monitoring/token_tracker.py`
- Modify: `core/storage/token_repo.py`
- Modify: `core/monitoring/__init__.py:3,11`
- Modify: `core/cognitive/loop.py:58,701-711`
- Modify: `core/debug/info.py:336-351`
- Modify: `core/monitoring/tests/test_token_tracker.py`
- Modify: `core/storage/tests/test_token_repo.py`

**Interfaces:**
- Consumes: Task 1 的 `TokenUsage`/`TokenUsageSummary`；Task 2 的 `_extract_usage` 产出 dict（record 的 `usage` 参数已是新键名）。
- Produces: `TokenTracker.record(usage: dict, model, round_num)`（内部 `from_api`）、`current_turn_usages: list[TokenUsage]`；`TokenRepository.record(entry: TokenUsage)` / `flush(entries: list[TokenUsage])` / `query_by_session() -> list[TokenUsage]`（legacy 跳过）。

- [ ] **Step 1: 重写 `core/monitoring/token_tracker.py` 的记录与持久化路径**

删除 dataclass `TokenUsage`（L29-37）。import 改为：
```python
from core.schema.token import TokenUsage
```
`record`（L82-115）改：
```python
    async def record(self, usage: dict, model: str, round_num: int) -> None:
        """记录一次 API 调用的 token 消耗。

        每轮 LLM 调用完成后由 CognitiveLoop 调用一次。
        自动完成：内存累计、WS 广播、持久化。
        """
        u = TokenUsage.from_api(
            usage, model=model, session_id=self.session_id, round_num=round_num,
        )

        # 1. 内存累计
        self.session_total += u.total_tokens
        self.per_model[model] = self.per_model.get(model, 0) + u.total_tokens
        self.current_turn_usages.append(u)

        # 2. WS 广播（前端实时更新）
        await self._output.token_usage({
            "turn": {
                "rounds": [r.model_dump(mode="json") for r in self.current_turn_usages],
                "turn_total": sum(r.total_tokens for r in self.current_turn_usages),
            },
            "session": {
                "total": self.session_total,
                "by_model": dict(self.per_model),
            },
        })

        # 3. 持久化
        await self._persist(u)
```
`_persist`（L170-193）改：
```python
    async def _persist(self, usage: TokenUsage) -> None:
        """持久化单条记录：MongoDB 优先，失败则写本地 JSONL。"""
        if self._repo is not None:
            try:
                await self._repo.record(usage)
                return
            except Exception:
                logger.debug("MongoDB 不可用，token 记录写入 %s", self._pending_file)
        # 追加到本地 JSONL（session_id 已在 usage 内）
        os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
        doc = usage.model_dump(mode="json")
        with open(self._pending_file, "a") as f:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
```
`flush_pending`（L152-168）改：
```python
    async def flush_pending(self) -> None:
        """将本地 pending 文件中的数据批量导入 MongoDB，成功后删除文件。

        旧格式行（prompt/completion）validate 失败时跳过且不保留——
        整文件导入成功后删除，与"不兼容直接改名、旧数据自然老化"一致。
        """
        if self._repo is None:
            return
        if not os.path.exists(self._pending_file):
            return
        try:
            records: list[TokenUsage] = []
            skipped = 0
            with open(self._pending_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        records.append(TokenUsage.model_validate(json.loads(line)))
                    except Exception:
                        skipped += 1  # 旧格式行，跳过不保留
            if records:
                await self._repo.flush(records)
                logger.info("pending 文件导入完成: %d 条 token 记录已写入 MongoDB (跳过 %d 条旧格式)",
                            len(records), skipped)
            os.remove(self._pending_file)
        except Exception:
            logger.warning("pending 文件导入失败，下次启动重试", exc_info=True)
```
`get_session_stats` 与 `clear_all` 不变（pending 补充循环读 `session_id`/`model`/`total_tokens`，新 JSONL 格式键名一致）。

`core/monitoring/__init__.py`：
```python
from .token_tracker import TokenTracker
```
并从 `__all__` 移除 `"TokenUsage"`。

- [ ] **Step 2: 重写 `core/storage/token_repo.py`**

TYPE_CHECKING import 与各方法内 import 均 `TokenUsageEntry` → `TokenUsage`：
```python
    async def record(self, entry: "TokenUsage") -> None:
        """记录一条 token 用量。"""
        if self._db is None:
            return
        await self._db.insert_one(COLLECTION, entry.model_dump(mode="json"))

    async def flush(self, entries: list["TokenUsage"]) -> None:
        """批量写入 token 用量。"""
        if self._db is None or not entries:
            return
        docs = [e.model_dump(mode="json") for e in entries]
        await self._db.insert_many(COLLECTION, docs)

    async def query_by_session(self, session_id: str, *,
                               limit: int = 100) -> list["TokenUsage"]:
        """按会话查询 Token 用量记录。

        旧文档（prompt_tokens/completion_tokens 字段）validate 失败时跳过，
        不迁移、自然老化。
        """
        if self._db is None:
            return []
        from core.schema.token import TokenUsage
        docs = await self._db.find(COLLECTION, {"session_id": session_id}, limit=limit)
        result: list[TokenUsage] = []
        for d in docs:
            try:
                result.append(TokenUsage.model_validate(d))
            except Exception:
                continue  # 旧文档跳过
        return result
```
聚合查询（`total_tokens_by_session`/`total_tokens_by_model`）不变——`total_tokens` 字段仍在。

- [ ] **Step 3: 更新 `core/cognitive/loop.py`**

L58：`from core.schema.token import TokenUsageRecord, TokenUsageSummary` → `from core.schema.token import TokenUsageSummary`。
L701-711 映射循环删除，直通：
```python
                token_usage=TokenUsageSummary(
                    rounds=list(self._token_tracker.current_turn_usages),
                    turn_total=sum(r.total_tokens for r in self._token_tracker.current_turn_usages),
                ) if self._token_tracker and self._token_tracker.current_turn_usages else None,
```
> 不变式：round 与 summary 同属 `tracker.session_id`，与消息同会话必然一致（session_id 已由 `record()` 注入）。

- [ ] **Step 4: 更新 `core/debug/info.py` current_turn**

L336-351 改为：
```python
    tracker = agent._token_tracker
    if tracker is not None:
        info.token_stats = {
            "session_total": tracker.session_total,
            "per_model": dict(tracker.per_model),
            "current_turn": [
                u.model_dump(mode="json")
                for u in tracker.current_turn_usages[-10:]
            ],
        }
```

- [ ] **Step 5: 更新 `core/monitoring/tests/test_token_tracker.py`**

- L8：`from core.monitoring.token_tracker import TokenTracker`（TokenUsage 改从 `core.schema.token` 导入）
- L9：`from core.schema.token import TokenUsageEntry` → `from core.schema.token import TokenUsage`
- 删除 `TestTokenUsage.test_dataclass_fields`（dataclass 已删；模型测试在 `core/schema/tests/test_token.py`）
- 全部 `usage={"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}` → `usage={"input_tokens": ..., "output_tokens": ..., "total_tokens": ...}`
- record 后断言 `u.prompt_tokens` → `u.input_tokens`、`u.completion_tokens` → `u.output_tokens`
- `flush_pending` 相关测试若构造旧格式 pending 行（如 `test_flush_pending_imports_and_deletes_file`），更新为新格式；新增一个旧格式行被跳过且文件删除的用例：
```python
    async def test_flush_pending_skips_legacy_lines(self, output_mock, tmp_path):
        """旧格式 pending 行跳过不导入，文件仍删除（tmp_path 隔离，不污染 data/）。"""
        repo_mock = AsyncMock()
        tracker = TokenTracker(
            session_id=SessionId("default"), output_manager=output_mock, repo=repo_mock,
        )
        tracker._pending_file = str(tmp_path / "token_usage_pending.jsonl")
        with open(tracker._pending_file, "w") as f:
            f.write(json.dumps({
                "session_id": "default", "model": "m",
                "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                "round_num": 0, "timestamp": "2026-07-01T00:00:00+00:00",
            }) + "\n")
        await tracker.flush_pending()
        repo_mock.flush.assert_not_awaited()  # 无有效记录 → 不调用 flush
        assert not os.path.exists(tracker._pending_file)  # 整文件仍删除，旧行不保留
```

- [ ] **Step 6: 更新 `core/storage/tests/test_token_repo.py`**

- L5：`from core.schema.token import TokenUsage`（替代 TokenUsageEntry）
- `test_record_calls_insert_one`：用新字段构造 `TokenUsage(...)`，断言 `insert_one` 收到含 `input_tokens`/`output_tokens` 的 dict
- 新增 legacy 跳过测试：
```python
    async def test_query_by_session_skips_legacy_docs(self):
        """旧格式文档（prompt/completion 字段）被跳过。"""
        repo = TokenRepository(db=AsyncMock())
        repo._db.find = AsyncMock(return_value=[
            {
                "session_id": "default", "model": "m",
                "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                "round_num": 0, "timestamp": "2026-07-01T00:00:00+00:00",
            },
            {
                "session_id": "default", "model": "m",
                "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                "round_num": 0, "timestamp": "2026-07-01T00:00:00+00:00",
            },
        ])
        result = await repo.query_by_session("default")
        assert len(result) == 1
        assert result[0].input_tokens == 100
```

- [ ] **Step 7: 运行后端测试确认**

```bash
python -m pytest core/monitoring/tests/test_token_tracker.py core/storage/tests/test_token_repo.py core/cognitive/tests/ core/memory/tests/ core/storage/tests/ -v
```
预期：全部 PASS（后端链路统一后全绿）。

- [ ] **Step 8: 提交**

```bash
git add core/monitoring/token_tracker.py core/storage/token_repo.py core/monitoring/__init__.py \
  core/cognitive/loop.py core/debug/info.py \
  core/monitoring/tests/test_token_tracker.py core/storage/tests/test_token_repo.py
git commit -m "feat(token): TokenTracker/Repo/loop/debug 统一 TokenUsage 模型"
```

---

### Task 4: 前端

**Files:**
- Modify: `frontend/src/types/protocol.ts`
- Modify: `frontend/src/stores/chatStore.ts`
- Modify: `frontend/src/components/character/CharStatus.vue:17`
- Modify: `frontend/src/components/chat/TokenLabel.vue`
- Modify: `frontend/src/debug/types.ts`

**Interfaces:**
- Consumes: Task 3 的 WS `token_usage` 广播（rounds 为 `model_dump(mode="json")`，字段 input/output/cached/reasoning，timestamp ISO 字符串）与 `session` 消息的 `token_stats`（`{total, by_model}`）。
- Produces: TS 类型与渲染与新契约对齐，`byModel` 全清。

- [ ] **Step 1: 更新 `frontend/src/types/protocol.ts`**

```ts
export interface TokenRound {
  model?: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cached_tokens?: number
  reasoning_tokens?: number
  round_num: number
  timestamp?: string
}

export interface TokenStats {
  total: number
  by_model: Record<string, number>
}
```

- [ ] **Step 2: 更新 `frontend/src/stores/chatStore.ts`**

- L41：`const sessionTokens = ref<{ total: number; by_model: Record<string, number> }>({ total: 0, by_model: {} })`
- L202-203 双兼容改单字段：
```ts
    if (msg.session) {
      const s = msg.session as { total: number; by_model: Record<string, number> }
      sessionTokens.value = { total: s.total, by_model: s.by_model }
    }
```
- `restoreFromServer`（L241）死参数接通：
```ts
  function restoreFromServer(
    serverMessages: any[],
    tokenStats?: { total: number; by_model: Record<string, number> },
  ) {
    messages.value = []
    lastRole.value = ''
    lastMsgTime.value = 0
    nextBubbleQueue.value = []
    playingMessageId.value = null
    // 游标 = session 中最早一条消息（可能有 _id）；归档查询用 _id 游标
    earliestMessageId.value = serverMessages[serverMessages.length - 1]?._id || null

    // 恢复 session 累计 token（消除刷新/重连后 CharStatus 归零直到下次广播的缺口）
    if (tokenStats) {
      sessionTokens.value = { total: tokenStats.total, by_model: tokenStats.by_model }
    }

    for (const m of serverMessages) {
      const converted = convertServerMessage(m)
      for (const cm of converted) {
        messages.value.push(cm)
      }
    }

    finalizeDelta()
  }
```

- [ ] **Step 3: 更新 `CharStatus.vue:17`**

```ts
  const models = Object.entries(st.by_model || {})
```

- [ ] **Step 4: 更新 `TokenLabel.vue` 显示**

模板内（L38-42）：
```vue
        <span class="token-round-num">#{{ round.round_num }}</span>
        <span class="token-round-item">In:{{ fmt(round.input_tokens) }}</span>
        <span class="token-round-item">Out:{{ fmt(round.output_tokens) }}</span>
        <span v-if="round.cached_tokens != null" class="token-round-item">cached:{{ fmt(round.cached_tokens) }}</span>
        <span v-if="round.reasoning_tokens != null" class="token-round-item">think:{{ fmt(round.reasoning_tokens) }}</span>
        <span class="token-round-total">{{ fmt(round.total_tokens) }}</span>
```

- [ ] **Step 5: 更新 `frontend/src/debug/types.ts`**

```ts
export interface TokenRoundItem {
  model: string; input_tokens: number; output_tokens: number
  total_tokens: number; round_num: number
}
```

- [ ] **Step 6: 前端类型检查 + 构建**

```bash
cd frontend && npm run build
```
预期：`vue-tsc --noEmit` 通过（无 `byModel`/`prompt_tokens` 类型残留），`vite build` 成功输出到 `static/`。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/types/protocol.ts frontend/src/stores/chatStore.ts \
  frontend/src/components/character/CharStatus.vue frontend/src/components/chat/TokenLabel.vue \
  frontend/src/debug/types.ts
git commit -m "feat(token): 前端 protocol/展示切换到 input-output 字段 + 恢复链路接通"
```

---

### Task 5: 文档 + 最终验证

**Files:**
- Modify: `core/schema/README.md:19,47`
- Modify: `core/monitoring/README.md:48,59`
- Modify: `docs/data-structure-survey.md:42,342-343`
- Modify: `docs/database-storage.md:111-112`
- Modify: `docs/frontend-api.md:84,447-448`

- [ ] **Step 1: 更新 `core/schema/README.md`**

L19：`├── token.py     # TokenUsageEntry — Token 用量记录` → `├── token.py     # TokenUsage + TokenUsageSummary — Token 用量记录`
L47：`| token.py | TokenUsageEntry / TokenUsageRecord / TokenUsageSummary — Token 用量记录 |` → `| token.py | TokenUsage / TokenUsageSummary — Token 用量记录 |`

- [ ] **Step 2: 更新 `core/monitoring/README.md`**

L48：`- **累计统计**：current_turn_usages: list[TokenUsageRecord]，回合结束写入 AssistantMessage.token_usage` → `- **累计统计**：current_turn_usages: list[TokenUsage]，回合结束写入 AssistantMessage.token_usage`
L59：`├── token_tracker.py   — TokenTracker + TokenUsage` → `├── token_tracker.py   — TokenTracker（TokenUsage 在 core.schema.token）`

- [ ] **Step 3: 更新 `docs/data-structure-survey.md`**

L42：`| core/monitoring/token_tracker.py:25 | TokenUsage | model, prompt_tokens, completion_tokens, total_tokens, round_num, timestamp | Token 消耗记录 |` → `| core/schema/token.py | TokenUsage | model, session_id, round_num, input_tokens, output_tokens, total_tokens, cached_tokens?, reasoning_tokens?, timestamp | Token 消耗记录 |`
L342-343：
```python
    "input_tokens": int,
    "output_tokens": int,
    "total_tokens": int,
    "cached_tokens": int | None,
    "reasoning_tokens": int | None,
```

- [ ] **Step 4: 更新 `docs/database-storage.md:111-112`**

```markdown
| `input_tokens` | Int | 输入 token 数 |
| `output_tokens` | Int | 输出 token 数 |
| `total_tokens` | Int | 总 token 数 |
| `cached_tokens` | Int \| null | 缓存命中 token（可选） |
| `reasoning_tokens` | Int \| null | 思考 token（可选） |
```
（删除原 `prompt_tokens`/`completion_tokens` 两行，`total_tokens`/`round_num` 保留）

- [ ] **Step 5: 更新 `docs/frontend-api.md`**

L84：`"token_usage": { "rounds": [{ "round_num": 0, "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801 }], "turn_total": 1801 }` → `"token_usage": { "rounds": [{ "round_num": 0, "input_tokens": 1234, "output_tokens": 567, "total_tokens": 1801 }], "turn_total": 1801 }`
L447-448：
```python
        "input_tokens": 1234,
        "output_tokens": 567,
```

- [ ] **Step 6: 最终验证**

```bash
# 权威模型唯一性：core/ 下无旧字段结构定义（LLM 层 openai_compat 属性访问 u.prompt_tokens
# 与 docstring 契约属 Module/llm，不在 grep 范围；message_adapter.py 的降级 docstring
# 逐字描述旧字段，属设计要求保留，予以排除）
grep -rn "prompt_tokens\|completion_tokens" core/ --include="*.py" \
  | grep -v test | grep -v __pycache__ | grep -v "message_adapter.py"
# 预期空输出

# 前端 byModel 字段全部改名 by_model（无残留）
grep -rn "byModel" frontend/src/
# 预期空输出

# 全量快速测试（unit + service）
make test-quick
```

预期：两个 grep 空输出，`make test-quick` 全绿。

- [ ] **Step 7: 提交**

```bash
git add core/schema/README.md core/monitoring/README.md \
  docs/data-structure-survey.md docs/database-storage.md docs/frontend-api.md
git commit -m "docs: token 统一结构文档同步（README + 数据文档）"
```

---

## 验证命令总览

| 阶段 | 命令 | 预期 |
|------|------|------|
| Task 1 | `python -m pytest core/schema/tests/test_token.py core/schema/tests/test_message_degradation.py -v` | PASS |
| Task 2 | `python -m pytest Module/llm/tests/test_llm.py -v` | PASS（含 M6 回归） |
| Task 3 | `python -m pytest core/monitoring/tests/test_token_tracker.py core/storage/tests/test_token_repo.py -v` | PASS |
| Task 4 | `cd frontend && npm run build` | vue-tsc + vite 成功 |
| Task 5 | 两个 grep + `make test-quick` | 空输出 + 全绿 |
