# 前后端契约单源化（frontend_contract）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把前后端契约（WS 消息 + 业务 REST + Debug API）从「后端手写 dict + 前端手写 TS + markdown」三处同步，收敛为顶层独立包 `frontend_contract/` 的 Pydantic 模型（唯一权威），前端 TS 由内置发射器 `gen_ts.py` 生成，后端发送点构造模型获得运行时校验。

**Architecture:** 单源在后端（Pydantic 作源）；`ContractModel` 基类统一 `wire_dict()` 递归序列化（`exclude_none` 缺省省略 + `ts_null` 显式 null）；`gen_ts.py` 零依赖发射器输出 `frontend/src/types/contract.generated.ts`（只读快照）；`make check-types` CI 漂移检测。完整设计见 spec：`docs/superpowers/specs/2026-08-03-frontend-contract-single-source-design.md`（3 轮评审定稿）。

**Tech Stack:** Python 3.11+ / Pydantic 2 / aiohttp / Vue 3 + Pinia + TypeScript / Makefile。

## Global Constraints

- **零依赖**：`frontend_contract/` 不 import `core/` 或 `Module/`——只依赖 pydantic + stdlib（硬约束，对齐 `workflow_contract` 先例）。
- **契约视图模型**：全部模型是独立 wire 形状，不与 `core.schema` 共享领域模型；发送点把内部对象转成契约模型。
- **必填判定**：发射器按 Python 类型是否 `Optional` 判定（非看 schema required）——非 Optional（含带默认 `enabled: bool = True`）→ 必填。
- **ts_null vs X? 逐发送点按实际 wire 行为定**：键恒存在（含成功 null）→ `ts_null`；键缺省省略 → `X?`。禁止一刀切。
- **判别字段** `type` 走"非 Optional → 必填"；`UnknownMessage` 由发射器末尾追加。
- **建模硬性要求**：每个模型类写中文 docstring；**每个字段用 `Field(description=中文)` 携带注释**（这是生成 JSDoc 的唯一来源；可选字段注明"可缺省"/"可为 null"）。
- **同名类型命名策略**：不同子模块中形状不同的同名类型必须改名避免生成文件同名冲突——**debug 的 `ContentPart` → `DebugContentPart`、`TokenStats` → `DebugTokenStats`**（debug/types.ts 的 TokenStats 是 `{session_total, per_model, current_turn}`，与 ws_message 的 `{total, by_model}` 形状不同，无法复用同一模型）；`generate_ts` 检测到同名不同类型 → 报错退出（防静默合并成非法 TS）。
- **行为等价**：wire 格式与现有手写 dict 逐字段一致（.NET 第三方可见），以测试全绿为准。
- **门禁**：`make lint` 扩展为 `ruff check core/ Module/ frontend_contract/`；新测试加 marker（unit/service）；禁止相对导入（TID252）。
- 代码注释用中文；测试用 Elysia conda 环境（`conda run -n Elysia python -m pytest ...`）。

## File Structure

```
frontend_contract/                 # 新建顶层包（Task 1）
├── __init__.py                    # 重导出全部契约类型
├── base.py                        # ContractModel 基类（wire_dict 递归）+ TsNull marker
├── ws_message.py                  # WS 消息帧（Task 2）
├── rest.py                        # 业务 REST 响应（Task 4）
├── debug.py                       # Debug API 响应（Task 5）
├── gen_ts.py                      # B2 发射器（Task 1）
├── __main__.py                    # python -m frontend_contract → 跑 gen_ts（Task 1）
├── tests/                         # 模型测试 + 生成器快照测试
└── README.md                      # 面包屑 + 相关文档（Task 6）

frontend/src/types/contract.generated.ts   # 生成快照（// DO NOT EDIT）
frontend/src/types/protocol.ts             # 契约类型删除 → re-export（Task 3/4/5 逐步）
frontend/src/debug/types.ts                # 迁移到生成类型（Task 5）

修改：Makefile、.github/workflows/ci.yml、core/input/ws_handler.py、core/input/capabilities.py、
      core/input/api_routes.py、core/input/debug_routes/（7 文件）、docs/ 各文档
```

---

### Task 1: frontend_contract 包骨架 + ContractModel/TsNull + gen_ts 发射器 + 门禁

**Files:**
- Create: `frontend_contract/__init__.py`
- Create: `frontend_contract/base.py`
- Create: `frontend_contract/gen_ts.py`
- Create: `frontend_contract/__main__.py`
- Create: `frontend_contract/tests/test_gen_ts.py`
- Modify: `Makefile`（`gen-types` / `check-types` / `lint` 扩展）
- Modify: `.github/workflows/ci.yml`（加 `check-types` 步骤）

**Interfaces:**
- Produces: `frontend_contract.ContractModel`（基类，`wire_dict()` 递归 + `ts_null` 声明）、`frontend_contract.TsNull`（marker 类，`Annotated[X, TsNull()]` 标记"可为 null"）、`python -m frontend_contract`（生成器入口，写 `contract.generated.ts`）、`make gen-types` / `make check-types`。

- [ ] **Step 1: 写失败测试** — `frontend_contract/tests/test_gen_ts.py`（marker=unit）：用三个代表模型（简单 `DeltaMessage`、嵌套+ts_null `CapabilitiesMessage`/`ServiceCapability`、客户端 `SetServiceMessage`）验证发射器输出：

```python
# 测试用的"已知模型集"——与 Task 2 的真实模型同构，先验证发射器机制
import pytest
from typing import Annotated, Literal, Optional
from pydantic import Field
from frontend_contract.base import ContractModel, TsNull
from frontend_contract.gen_ts import generate_ts

class DeltaMessage(ContractModel):
    """流式文字增量。"""
    type: Literal["delta"] = "delta"
    content: str = Field(description="文本内容")
    stream_id: Optional[int] = Field(default=None, description="TTS 流 ID，可缺省")

class ServiceCapability(ContractModel):
    """单服务运行时能力。"""
    available: bool
    enabled: bool
    model: Annotated[Optional[str], TsNull()] = None   # 可为 null：unavailable 时显式 null
    default: str

class CapabilitiesMessage(ContractModel):
    """服务能力声明（嵌套 ts_null 验证递归）。"""
    type: Literal["capabilities"] = "capabilities"
    services: dict[str, ServiceCapability]

class SessionList(ContractModel):
    """list 字段测试（SessionMessage.messages / env_vars 场景）。"""
    messages: list[str]
    events: Optional[list[str]] = None   # 可缺省 list → events?: string[]

class ApiOk(ContractModel):
    """bool Literal 测试（ApiOk.ok 场景）。"""
    ok: Literal[True] = True

@pytest.mark.unit
def test_emit_basic_interface():
    out = generate_ts([DeltaMessage, ServiceCapability, CapabilitiesMessage, SessionList, ApiOk])
    assert "export interface DeltaMessage" in out
    assert "type: 'delta'" in out                      # 判别字段必填（非 Optional）
    assert "content: string" in out                     # 非 Optional → 必填
    assert "stream_id?: number" in out                  # Optional → X?
    assert "model: string | null" in out                # TsNull → X | null
    assert "Record<string, ServiceCapability>" in out
    assert "messages: string[]" in out                  # list[str] → string[]（防 list 兜底 'list'）
    assert "events?: string[]" in out                   # Optional[list[str]] → events?: string[]
    assert "ok: true" in out                            # Literal[True] → true（小写，非 'True'）
    assert "import type" not in out                      # 拓扑序内联，无跨文件 import
    assert "服务能力" in out                             # docstring → JSDoc 保留中文

@pytest.mark.unit
def test_wire_dict_recursive_ts_null():
    """嵌套 ts_null：available=false 时 model 键恒存在且为 null。"""
    cap = CapabilitiesMessage(services={"stt": ServiceCapability(
        available=False, enabled=True, model=None, default="sense-voice")})
    d = cap.wire_dict()
    assert d["services"]["stt"]["model"] is None          # 递归补 null，键不被 exclude 丢掉
    assert d["services"]["stt"]["default"] == "sense-voice"

@pytest.mark.unit
def test_wire_dict_omits_optional_none():
    """可缺省字段 None → 键缺省省略（非显式 null）。"""
    d = DeltaMessage(content="hi", stream_id=None).wire_dict()
    assert "stream_id" not in d
    assert d["type"] == "delta"
```

- [ ] **Step 2: 运行确认失败**（无 `frontend_contract` 包）。

- [ ] **Step 3: base.py** —

```python
"""契约模型基类 + TsNull marker（零依赖，仅 pydantic）。"""
from typing import Annotated, Any, get_origin, get_args
from pydantic import BaseModel


class TsNull:
    """可为 null marker：声明字段 wire 显式 null（区别于"可缺省"缺省省略）。

    用法：`field: Annotated[Optional[str], TsNull()] = None`。
    """

    ts_null = True


def is_ts_null(annotation) -> bool:
    """检测字段是否带 TsNull marker（Annotated 元数据）。"""
    origin = get_origin(annotation)
    if origin is not Annotated:
        return False
    meta = get_args(annotation)[1:]
    return any(getattr(m, "ts_null", False) for m in meta)


class ContractModel(BaseModel):
    """契约模型基类：wire_dict() 统一序列化。

    - exclude_none=True：可缺省字段（None）缺省省略
    - ts_null 标记字段：None 时补显式 null
    - 嵌套递归：字段值为 ContractModel / dict[str, ContractModel] 时递归 wire_dict
    """

    def wire_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        for name, field in self.model_fields.items():
            if is_ts_null(field.annotation) and getattr(self, name) is None:
                data[name] = None
        # 嵌套递归：ContractModel / dict[str, ContractModel] / list[ContractModel]
        for name, value in self.__dict__.items():
            if isinstance(value, ContractModel):
                data[name] = value.wire_dict()
            elif isinstance(value, dict):
                data[name] = {
                    k: v.wire_dict() if isinstance(v, ContractModel) else v
                    for k, v in value.items()
                }
            elif isinstance(value, list):
                data[name] = [
                    v.wire_dict() if isinstance(v, ContractModel) else v
                    for v in value
                ]
        return data
```

- [ ] **Step 4: gen_ts.py 发射器** — 核心逻辑（含必填判定 + 三态 + 判别 + 拓扑 + docstring→JSDoc）：

```python
"""B2 发射器：frontend_contract BaseModel → contract.generated.ts（零第三方依赖）。"""
from typing import Annotated, Literal, Union, get_args, get_origin
from pydantic import BaseModel
from frontend_contract.base import is_ts_null


def _literal(v) -> str:
    """Literal 值 → TS 字面量。统一单引号（对齐 protocol.ts 风格，如 type: 'delta'）；bool 小写。"""
    if isinstance(v, str):
        return f"'{v}'"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _ts_type(annotation) -> str:
    """类型 → TS 类型。Optional 语义（?/| null）由 _field_ts 集中处理，此处去 Optional 返回内层。"""
    origin = get_origin(annotation)
    if origin is Annotated:                       # 剥 Annotated
        return _ts_type(get_args(annotation)[0])
    if origin is Union:
        # 去 None 后 join 全部成员（Optional[X] → X；Optional[Union[A,B]] → A | B，
        # 不可只取第一个——否则 SessionMessageItem.content?: string | ContentPart[] 会丢类型）
        args = [a for a in get_args(annotation) if a is not type(None)]
        return " | ".join(_ts_type(a) for a in args)
    if origin is Literal:
        return " | ".join(_literal(v) for v in get_args(annotation))
    if origin is list:
        return f"{_ts_type(get_args(annotation)[0])}[]"
    if origin is dict:
        return f"Record<string, {_ts_type(get_args(annotation)[1])}>"
    if annotation is str: return "string"
    if annotation is bool: return "boolean"
    if annotation in (int, float): return "number"
    return annotation.__name__


def _is_optional(annotation) -> bool:
    """必填判定：Python 类型是否 Optional（非看 schema required）。"""
    origin = get_origin(annotation)
    return origin is Union and type(None) in get_args(annotation)


def _field_ts(name, field) -> str:
    """字段 → TS 行。?/| null 在此集中决定（ts_null → | null；Optional 非 ts_null → ?）。"""
    is_null = is_ts_null(field.annotation)
    optional = _is_optional(field.annotation) and not is_null
    base = _ts_type(field.annotation)
    t = f"{base} | null" if is_null else base
    q = "?" if optional else ""
    doc = field.description or ""
    line = f"  {name}{q}: {t}"
    return f"  /** {doc} */\n{line}" if doc else line


def generate_ts(models: list[type[BaseModel]],
                discriminated: dict[str, list[type[BaseModel]]] | None = None) -> str:
    """生成 contract.generated.ts。

    Args:
        models: 全部契约模型（拓扑排序后输出 interface）。同名不同类型（如两处 ContentPart
            形状不同）→ ValueError（防静默合并成非法 TS——见命名策略）。
        discriminated: 判别联合别名 → 成员类映射，如 {"ServerMessage": [DeltaMessage, ...]}。
            别名（Annotated）不是 BaseModel，不会出现在 models 里——必须由调用方（__main__）
            显式传入，gen_ts 不做模块内魔法发现。
    """
    # 1. 拓扑排序（DFS 后序：被引用模型先输出）
    ordered = _topo_sort(models)
    lines = ["// DO NOT EDIT — generated by python -m frontend_contract", ""]
    for cls in ordered:
        doc = f"/** {cls.__doc__.strip()} */" if cls.__doc__ else ""
        lines += [doc, f"export interface {cls.__name__} {{"]
        lines += [_field_ts(n, f) for n, f in cls.model_fields.items()]
        lines += ["}", ""]
    # 2. 判别联合（调用方显式传入）+ UnknownMessage 兜底
    for alias, members in (discriminated or {}).items():
        lines += [f"export type {alias} ="]
        lines += ["  | " + m.__name__ for m in members]
        lines += ["  | UnknownMessage", ""]
    lines += ["export interface UnknownMessage { type: string; [key: string]: unknown }", ""]
    return "\n".join(lines)
```

（`_topo_sort(models)`：按 `model_fields` 引用的嵌套 `ContractModel` 子类做 DFS 后序，保证依赖先输出。`__main__` 收集各子模块的 BaseModel 列表 + `ServerMessage` 判别联合别名成员（`get_args(get_args(ServerMessage)[0])`），显式传给 `generate_ts`。两者都是纯函数，快照测试锁行为。）

- [ ] **Step 5: __main__.py** —
  - `python -m frontend_contract`：用 `importlib` **动态收集存在的子模块**（`ws_message`/`rest`/`debug` 到 Task 2/4/5 才存在——Task 1 只有 base.py + 测试，生成文件初始为用空模型跑 `generate_ts` 的实际输出，Task 2 起内容增长）。收集各子模块的 BaseModel 列表时**过滤基类**（`cls is not ContractModel`——`ContractModel` 经子模块 import 进入命名空间，不滤会生成无字段的 `export interface ContractModel {}` 噪音）→ 收集 `ServerMessage` 判别别名成员（`get_args(get_args(ServerMessage)[0])`）→ `generate_ts(models, discriminated)` → 写 `frontend/src/types/contract.generated.ts`。
  - **`--check` 模式**：生成到临时文件，与提交版 `git diff --exit-code` 校验漂移（非零退出码 → CI 红）。
  - **生成文件初始提交**：Task 1 提交**用空 `models`/`discriminated` 跑 `generate_ts` 的实际输出**（`// DO NOT EDIT` header + `UnknownMessage` interface——`generate_ts` 恒 emit 兜底类型，提交文件必须与生成器输出**字节一致**，否则 `check-types` 首次就红）。Task 2 起内容增长。

- [ ] **Step 6: 跑测试** — `conda run -n Elysia python -m pytest frontend_contract/tests/ -q` 全绿（3 个测试：发射器三态 + wire_dict 递归 + 缺省省略）。

- [ ] **Step 7: Makefile + CI** —
  - `Makefile` 加：`gen-types: python -m frontend_contract`、`check-types: python -m frontend_contract --check`（生成到临时文件，`git diff --exit-code` 校验无漂移）、`lint` 改为 `ruff check core/ Module/ frontend_contract/`。
  - `.github/workflows/ci.yml` 的 `quick` job 在 `Lint` 后加 `- name: Contract drift check\n  run: make check-types`。

- [ ] **Step 8: Commit** — `git commit -m "feat(contract): frontend_contract 包骨架 + ContractModel/TsNull + gen_ts 发射器 + 门禁"`

---

### Task 2: WS 消息契约建模（ws_message.py 全量）

**Files:**
- Create: `frontend_contract/ws_message.py`
- Create: `frontend_contract/tests/test_ws_message.py`
- Modify: `frontend_contract/__init__.py`（重导出）

**Interfaces:**
- Consumes: Task 1（`ContractModel`/`TsNull`）。
- Produces: `ServerMessage` 判别联合（19 成员）+ 客户端消息 + 支持类型（`ContentPart`/`ServiceCapability`/`TokenUsage`/`ToolCallData` 等），与现有 `frontend/src/types/protocol.ts` **逐字段对齐**。

- [ ] **Step 1: 写失败测试** — `frontend_contract/tests/test_ws_message.py`（marker=unit）：断言 19 个 ServerMessage 成员、每个 `type` 非 Optional（判别字段必填）、`switch_model_result.error` 是 `X?`（缺省省略）而 `service_mode_result.error` 是 `ts_null`、`CapabilitiesMessage.services.stt.model` 是 `ts_null`。

```python
import pytest
from frontend_contract import ws_message as w

@pytest.mark.unit
def test_server_message_members():
    from typing import get_args
    # ServerMessage = Annotated[Union[18 成员], Field(discriminator="type")]
    # get_args(Annotated) 返回 (Union[...], FieldInfo) —— 必须先剥 Annotated 再剥 Union
    members = get_args(get_args(w.ServerMessage)[0])
    assert len(members) == 18  # 19 = 18 具名 + UnknownMessage（发射器追加）
    assert all(m.__name__ != "UnknownMessage" for m in members)
    assert all(getattr(m, "__fields__", None) is not None for m in members)  # 均为 BaseModel

@pytest.mark.unit
def test_switch_vs_service_error_qualification():
    """switch_model_result.error 缺省省略（X?）；service_mode_result.error 可为 null（ts_null）。"""
    from frontend_contract.base import is_ts_null
    assert is_ts_null(w.SwitchModelResultMessage.model_fields["error"].annotation) is False
    assert is_ts_null(w.ServiceModeResultMessage.model_fields["error"].annotation) is True

@pytest.mark.unit
def test_capabilities_model_is_ts_null():
    from frontend_contract.base import is_ts_null
    assert is_ts_null(w.ServiceCapability.model_fields["model"].annotation) is True

@pytest.mark.unit
def test_wire_dict_list_recursive():
    """list[ContractModel] 元素递归 wire_dict（SessionMessage.messages 场景，base.py list 分支守护）。"""
    item = w.SessionMessageItem(role="assistant", content="hi")
    msg = w.SessionMessage(messages=[item], token_stats=w.TokenStats(total=1, by_model={}))
    d = msg.wire_dict()
    assert d["messages"][0]["role"] == "assistant"
    assert d["token_stats"]["total"] == 1
```

- [ ] **Step 2: 运行确认失败**（无 `ws_message`）。

- [ ] **Step 3: 建模** — `ws_message.py` 全量模型（**逐字段对照现有 `protocol.ts` 定性 ts_null vs X?**）。消息清单（对照 protocol.ts L243-262 的 18 具名成员）：
  - **Server→Client**：`SessionMessage`、`CapabilitiesMessage`、`DeltaMessage`、`MessageMessage`、`TokenUsageMessage`、`ExpressionMessage`、`StateMessage`、`AudioMessage`、`AudioEndMessage`、`TypingMessage`、`TranscriptionMessage`、`ActionMessage`、`CommandMessage`、`WakeMessage`、`SwitchModelResultMessage`、`ServiceModeResultMessage`、`HeartbeatAckMessage`、`ErrorMessage`
  - **Client→Server**：`TextMessage`、`InterruptMessage`、`SwitchModelMessage`、`SetServiceMessage`、`ToolApprovalMessage`、`WakeToggleMessage`
  - **支持类型**：`ContentPart`（自建 wire 形状，含 `src` 非 `source`）、`ServiceCapability`、`TokenRound`、`TokenStats`、`ToolCallData`、`ToolCallResult`、`TokenUsage`
  - 判别联合别名 `ServerMessage = Annotated[..., Field(discriminator="type")]`

  代表模型示范（覆盖全部类型模式，其余同构）：

```python
class DeltaMessage(ContractModel):
    """流式文字增量。"""
    type: Literal["delta"] = "delta"
    content: str = Field(description="文本内容")
    stream_id: Optional[int] = Field(default=None, description="TTS 流 ID，可缺省")

class ServiceCapability(ContractModel):
    """单服务运行时能力（available=false 时 model 为 null）。"""
    available: bool = Field(description="后端连接可用")
    enabled: bool = Field(description="用户模式")
    model: Annotated[Optional[str], TsNull()] = Field(default=None, description="当前模型，unavailable 时为 null")
    default: str = Field(description="config.yaml 默认模型")

class SwitchModelResultMessage(ContractModel):
    """模型切换结果。error 仅失败带（成功缺省省略 → 不用 ts_null）。"""
    type: Literal["switch_model_result"] = "switch_model_result"
    service: str = Field(description="服务名（llm/stt/tts）")
    model: str = Field(description="目标模型标识")
    ok: bool = Field(description="切换是否成功")
    error: Optional[str] = Field(default=None, description="失败原因，可缺省")

class ServiceModeResultMessage(ContractModel):
    """服务模式开关结果。error 恒存在（成功为 null → 用 ts_null）。"""
    type: Literal["service_mode_result"] = "service_mode_result"
    service: Literal["stt", "tts", "llm"] = Field(description="服务名")
    enabled: bool = Field(description="有效模式（永远随消息出现）")
    ok: bool = Field(description="操作是否成功")
    error: Annotated[Optional[str], TsNull()] = Field(default=None, description="错误，无则 null")
```

  **定性要点**（对照 `ws_handler.py` 实际 wire）：
  - `switch_model_result.error`：`**result` 展开，成功无键 → `Optional[str]`（X?）。
  - `service_mode_result.error`：`result.get("error")` 恒写 → `Annotated[Optional[str], TsNull()]`。
  - `CapabilitiesMessage.services.stt.model`：`available=false` 时 null → `TsNull`。
  - 其余 `Optional` 字段（`stream_id`/`error?` 等）默认 X?。

- [ ] **Step 4: __init__.py 重导出** — 从 `ws_message`/`base` 重导出全部类型（对齐 `core.schema` 统一入口惯例）。

- [ ] **Step 5: 跑测试 + 生成器快照** —
  - `conda run -n Elysia python -m pytest frontend_contract/tests/ -q` 全绿。
  - `python -m frontend_contract` 生成 `contract.generated.ts`，人工比对现有 protocol.ts 的关键类型（DeltaMessage/ServiceCapability/SwitchModelResultMessage）字段一致性。
  - `make check-types` 首次通过（生成文件已提交）。

- [ ] **Step 6: Commit** — `git commit -m "feat(contract): ws_message 全量建模（19 ServerMessage + 客户端 + 支持类型，ts_null/X? 逐发送点定性）"`

---

### Task 3: WS 发送点改造 + 前端切换（迁移步 A 完成）

**Files:**
- Modify: `core/input/ws_handler.py`（~12 处 send_json 构造模型）
- Modify: `core/input/capabilities.py`（`build_capabilities` 构造模型 + broadcast）
- Modify: `core/input/server.py`（create_app 相关）
- Modify: `frontend/src/types/protocol.ts`（契约类型删除 → re-export）
- Modify: `core/input/tests/test_server.py`（wire 格式专项断言）
- Modify: `frontend_contract/tests/test_gen_ts.py`（快照更新为真实模型）

**Interfaces:**
- Consumes: Task 1/2（模型 + 生成器 + 生成文件）。
- Produces: WS 消息 wire 格式**逐字节等价**（除专项验证的 ts_null/X? 定性），前端 `ServerMessage` 等从生成文件获取。

- [ ] **Step 1: 写失败测试** — `core/input/tests/test_server.py` 加专项断言：

```python
@pytest.mark.service
async def test_switch_model_result_success_no_error_key(aiohttp_client):
    """switch_model_result 成功：wire 无 error 键（缺省省略，非 null）。"""
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    async with client.session.ws_connect(client.make_url("/ws")) as ws:
        await ws.send_json({"type": "switch_model", "service": "llm", "model": "deepseek/deepseek-v4-flash"})
        frame = None
        for _ in range(5):
            msg = await ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "switch_model_result":
                frame = json.loads(msg.data); break
        assert frame is not None and frame["ok"] is True
        assert "error" not in frame  # 成功路径不得出现 "error": null（ts_null 过度标记会破坏）

@pytest.mark.service
async def test_service_mode_result_success_error_null(aiohttp_client):
    """service_mode_result 成功：wire 恒有 error 键且为 null（ts_null）。"""
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    async with client.session.ws_connect(client.make_url("/ws")) as ws:
        await ws.send_json({"type": "set_service", "service": "tts", "enabled": False})
        frame = None
        for _ in range(5):
            msg = await ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                frame = json.loads(msg.data); break
        assert frame is not None and frame["ok"] is True
        assert frame["error"] is None  # 键恒存在，成功为 null
```

- [ ] **Step 2: 运行确认失败**（当前 wire 已符合，改造后测试须仍绿——这是"行为等价"守护）。

- [ ] **Step 3: ws_handler.py 发送点改造** — 每个 `send_json` 从手写 dict → 构造模型 + `wire_dict()`：
  - 连接时：`session`（现有结构，模型含 messages/token_stats）、`capabilities`（Task 2 已建 `CapabilitiesMessage`/`ServiceCapability`，改为构造模型）、`expression`、`command`(tool_approval 重播)。
  - 消息循环：`switch_model_result`（`SwitchModelResultMessage(...).wire_dict()`）、`service_mode_result`（`ServiceModeResultMessage(...).wire_dict()`）。
  - `do_transcription` 的 `transcription`、`send_wake` 的 `wake`。
  - 错误：`error` 消息、`Invalid JSON`。
  - **不在此文件的**：`delta`/`message`/`typing`/`action`/`command`(inner_voice/block)/`token_usage`/`audio` 等由 `loop.py`/`Module/output` 广播——见 Step 4 范围。

- [ ] **Step 4: 广播点改造**（`core/cognitive/loop.py` + `Module/output/manager.py` 的 `_broadcast`/`audio_chunk`/`audio_end`）——**行为等价，仅包一层**：`on_text_delta`/`on_text_block` 等事件 → 构造 `DeltaMessage`/`CommandMessage` 等 + `wire_dict()`。若改造面过大，最小化：只包**前端可见帧**（delta/message/typing/audio/audio_end/action/command/token_usage），`_broadcast` 内部透传。

- [ ] **Step 5: capabilities.py** — `build_capabilities` 改返回 `CapabilitiesMessage` 模型（`services` 用 `dict[str, ServiceCapability]`）；`broadcast_capabilities` 发 `model.wire_dict()`。

- [ ] **Step 6: 前端 protocol.ts 切换** —
  - 契约相关类型（ServerMessage 及成员、ContentPart、客户端消息、支持类型）**删除**——**显式含 `export interface UnknownMessage`（L230-233）**：漏删会与生成文件 re-export 的 UnknownMessage 重复声明 → vue-tsc 红。
  - 顶部加 `export * from './contract.generated'`。
  - **保留**纯前端辅助类型（chatStore 内部状态、UI 专用）。
  - `npx vue-tsc --noEmit` 绿——若生成类型与手写有细微差异（如字段可选性），以生成文件为准修前端消费点。

- [ ] **Step 7: 跑测试** — `conda run -n Elysia python -m pytest core/input/tests/test_server.py core/cognitive/ core/agent/tests/ -q` + `make lint` + `cd frontend && npx vue-tsc --noEmit && npm run build` 全绿。`make check-types` 绿。

- [ ] **Step 8: Commit** — `git commit -m "feat(contract): WS 发送点构造模型 + 前端协议切换（迁移步 A 完成，wire 行为等价）"`

---

### Task 4: 业务 REST 契约（rest.py + api_routes + 前端类型切换）

**Files:**
- Create: `frontend_contract/rest.py`
- Create: `frontend_contract/tests/test_rest.py`
- Modify: `frontend_contract/__init__.py`
- Modify: `core/input/api_routes.py`
- Modify: `frontend/src/components/ui/ModelSelector.vue` + `frontend/src/components/panels/SettingsPanel.vue`（providers 类型）
- Modify: `frontend/src/stores/`（图片上传相关类型，若有）

**Interfaces:**
- Consumes: Task 1（`ContractModel`）。
- Produces: `ProvidersResponse`（动态 key `dict[str, ProviderEntry]` + `custom` + `unconfigured`）、`ImageUploadResponse`、`MessagesResponse`、`ApiOk`（`{ok, data?}`）/`ApiError`（`{ok: False, error}`）通用包装，与 `/api/*` 实际响应逐字段对齐。

- [ ] **Step 1: 写失败测试** — `frontend_contract/tests/test_rest.py`（marker=unit）：`ProvidersResponse` 反序列化 providers.yaml 形状、`ImageUploadResponse` 字段、`{ok, error}` 失败响应构造。

```python
@pytest.mark.unit
def test_providers_response_shape():
    resp = ProvidersResponse(
        llm={"deepseek": ProviderEntry(models=["deepseek-v4-flash"])},
        stt={}, tts={},
        custom={"llm": [CustomProvider(name="my", base_url="http://x")]},
        unconfigured={"llm": [UnconfiguredEntry(name="openai", env_vars=["OPENAI_API_KEY"])]},
    )
    d = resp.wire_dict()
    assert d["llm"]["deepseek"]["models"] == ["deepseek-v4-flash"]
    assert d["custom"]["llm"][0]["name"] == "my"
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: rest.py 建模** — 对照 `api_routes.py` + `provider_registry.py` 实际响应：
  - `ProviderEntry`（`models: list[str]`）、`ProvidersResponse`（`llm/stt/tts: dict[str, ProviderEntry]` + `custom: dict[str, list[CustomProvider]]` + `unconfigured: dict[str, list[UnconfiguredEntry]]`）。
  - `ImageUploadResponse`（`url/mime/size`）、**`ImageUploadError`（`{error: str}`——图片上传错误响应无 `ok` 键，与 ApiFail 形状不同，不可套用）**、`ApiOk`（`ok: Literal[True] = True` + `data?: object`）、`ApiFail`（`ok: Literal[False]` + `error: str`）——成功/失败用 Literal 判别（可选，若前端不依赖判别可简并）。
  - **`ProviderTestResult`（`{ok, latency_ms, error?, detail?}`——`/api/providers/test` 两个 level 形状不同：`usable` 返回 `{ok, latency_ms, detail}`；`connectivity` 直接返回 `backend.test()` 的 `{ok, latency_ms, error?}`（error 仅失败带，成功缺省）。模型含两字段并集且均 `?`——否则某 level 的 `error`/`detail` 键会被 Pydantic 忽略 → wire 丢字段）**。
  - `MessagesResponse`（`messages/has_more/total/error?`）。
  - **契约视图**：`Message` wire 形状自建（`role/parts/content/content_type/inner_voice/tool_calls/token_usage`），不引 `core.schema.Message`。

- [ ] **Step 4: api_routes.py 改造** — providers/images/messages/chat 响应 → 构造模型 + `wire_dict()`；内部 `core.schema.Message` → 契约 `MessageWire` 转换（`_archive_to_client` 逻辑保留）。

- [ ] **Step 5: 前端类型切换** — `ModelSelector.vue`/`SettingsPanel.vue` 的 providers fetch 类型改用生成类型；图片上传响应类型改用 `ImageUploadResponse`。

- [ ] **Step 6: 跑测试 + 构建** — `conda run -n Elysia python -m pytest core/input/tests/ frontend_contract/tests/ -q` + `make lint` + 前端 vue-tsc + build 全绿。`make check-types` 绿。

- [ ] **Step 7: Commit** — `git commit -m "feat(contract): 业务 REST 契约（rest.py + api_routes + 前端 providers/图片类型，迁移步 B 完成）"`

---

### Task 5: Debug API 契约（debug.py + debug_routes + 前端 debug 类型切换）

**Files:**
- Create: `frontend_contract/debug.py`
- Create: `frontend_contract/tests/test_debug.py`
- Modify: `frontend_contract/__init__.py`
- Modify: `core/input/debug_routes/`（7 文件响应改造）
- Modify: `frontend/src/debug/types.ts`（迁移到生成类型）

**Interfaces:**
- Consumes: Task 1（`ContractModel`）。
- Produces: `DebugData` 及子类型（16 个前端接口，对照 `frontend/src/debug/types.ts`），wire 形状独立（不引 `core.schema` 领域模型）。

- [ ] **Step 1: 写失败测试** — `frontend_contract/tests/test_debug.py`（marker=unit）：

```python
import pytest
from frontend_contract import debug as d

@pytest.mark.unit
def test_debug_data_key_fields():
    """DebugData 顶层字段存在（对照 frontend/src/debug/types.ts 的 OverviewTab 消费）。"""
    data = d.DebugData(
        fsm_state="idle", run_state="RUNNING", mood="平静", mood_intensity=0.5,
        llm_model="deepseek/deepseek-v4-flash", stt_ok=True,
        session_id="test", last_interaction_ago=0.0,
    )
    w = data.wire_dict()
    assert w["fsm_state"] == "idle"
    assert w["stt_ok"] is True

@pytest.mark.unit
def test_debug_nested_types_topo():
    """嵌套子类型（如 TasksTab 的 TaskData）可独立构造 + 序列化。"""
    task = d.TaskData(task_id="t1", status="active", title="任务")
    assert task.wire_dict()["status"] == "active"
```

（`DebugData`/`TaskData` 的具体字段在 Step 3 按 `frontend/src/debug/types.ts` 对齐建模；Step 1 仅锁定顶层字段存在性与嵌套可序列化。）

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: debug.py 建模** — 对照 `frontend/src/debug/types.ts` 的 17 个接口 + `debug_routes/` 实际响应：`DebugData`（16 面板字段）、`TaskData`、`LtmEntry`、`SessionInfo`、`ArchiveEntry` 等。内部 `core.schema.Message`/`CompressionTrigger` 相关字段用**契约视图**（wire 形状，如 `CompressionTrigger` 枚举值转 str）。**`ContentPart` → `DebugContentPart`、`TokenStats` → `DebugTokenStats`**（按命名策略改名——debug 的这两个形状与 ws_message 不同，同名会触发 generate_ts 报错）。

- [ ] **Step 4: debug_routes 改造** — 7 文件响应 → 构造模型 + `wire_dict()`；`_common.py` 的 `Message` 转换逻辑保留为内部→契约视图。

- [ ] **Step 5: 前端 debug/types.ts 切换** — 改 `export * from '../types/contract.generated'`（若契约类型与 debug 内部类型重叠则收窄 import）；保留 debug 页面专用局部类型。**`ContentPart`→`DebugContentPart`、`TokenStats`→`DebugTokenStats` 改名后，依赖原名的消费点（如 `SessionRecentItem.content: ContentPart[]`、`DebugData.token_stats: TokenStats` 及其 debug 组件引用）须同步改为 `Debug*` 名**——vue-tsc 会指出全部改点。

- [ ] **Step 6: 跑测试 + 构建** — `conda run -n Elysia python -m pytest core/input/tests/ frontend_contract/tests/ -q` + `make lint` + `cd frontend && npx vue-tsc --noEmit && npm run build`（含 debug 页）全绿。`make check-types` 绿。

- [ ] **Step 7: Commit** — `git commit -m "feat(contract): Debug API 契约（debug.py + debug_routes + 前端 debug 类型，迁移步 C 完成）"`

---

### Task 6: 文档同步

**Files:**
- Create: `frontend_contract/README.md`
- Modify: `README.md`（文档导航登记 frontend_contract）
- Modify: `CLAUDE.md`（Module index / 契约层说明 / make lint 描述）
- Modify: `docs/architecture.md`（frontend_contract 入架构树）
- Modify: `docs/frontend-api.md`（降级说明：契约唯一权威在 frontend_contract，本文为参考）
- Modify: `docs/testing.md`（新包测试 marker 说明）

- [ ] **Step 1: frontend_contract/README.md** — 面包屑 + 包结构 + 建模硬性要求（docstring/ts_null/X? 定性）+ 生成/校验命令 + 相关文档。按 `docs/readme-conventions.md` 模板。

- [ ] **Step 2: 根 README** — 文档导航加 `frontend_contract/README.md` 行（对齐其他模块格式）。

- [ ] **Step 3: CLAUDE.md** — `Module index`/架构段补 `frontend_contract`（契约层：唯一类型来源 + 生成器 + 门禁）；`make lint` 描述同步（已含 frontend_contract/）。

- [ ] **Step 4: docs/architecture.md** — 架构树加 `frontend_contract/`（与 core/ Module/ 平级，注明"零依赖、Pydantic 单源、TS 生成快照"）。

- [ ] **Step 5: docs/frontend-api.md** — 顶部加降级说明：契约唯一权威在 `frontend_contract/`（可执行），本文档为面向第三方的参考视图；协议变更流程 = 改 Pydantic → 跑生成器 → 同步本文档。

- [ ] **Step 6: docs/testing.md** — 补 `frontend_contract/tests/`（marker=unit）说明。

- [ ] **Step 7: Commit** — `git commit -m "docs: frontend_contract 文档登记（包 README + 根导航 + CLAUDE/architecture/frontend-api 降级说明）"`

---

### Task 7: 全量回归

- [ ] **Step 1: make test-quick** — `conda run -n Elysia make test-quick` 全绿。
- [ ] **Step 2: make lint** — 全绿（含 frontend_contract/）。
- [ ] **Step 3: make check-types** — 全绿（生成文件无漂移）。
- [ ] **Step 4: 前端构建** — `cd frontend && npx vue-tsc --noEmit && npm run build` 全绿。
- [ ] **Step 5: 一致性 grep**（本特性相关残留，均须为零，否则回退对应 Task 修）：
  - 后端手写 WS 消息帧 dict 残留：`grep -rn "send_json({\"type\"\|_send_json({\"type\"" core/ Module/`（应只剩构造模型 + `wire_dict()`）。
  - 前端手写契约类型残留：`protocol.ts` 中 `export interface DeltaMessage` / `export type ServerMessage`（应已删，由 re-export 替代）。
  - **契约包违规依赖**：`grep -rn "from core\.\|from Module\." frontend_contract/`（应为零——零依赖硬约束）。
- [ ] **Step 6: Commit**（若有回归修复）。
