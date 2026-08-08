# GenerationContext.channel/kind 对齐契约层 StrEnum — 设计文档

> **来源**：用户咨询（2026-08-08）`open_segment(self, channel: str, kind: str = "text")` 是否有必要统一成 StrEnum。
> 排查发现**契约层已有权威枚举**——`frontend_contract/ws_message.py` 定义了 `MessageChannel`（inner_voice/public_reply/status）
> 与 `SegmentKind`（text/markdown + 6 种预留），且 wire 契约字段 `generation.segment.started/delta/completed` 的
> `channel: MessageChannel` / `kind: SegmentKind` 已用枚举。core 侧 `GenerationContext.channel/kind` 与 `open_segment`
> 参数却是裸 `str`，靠 pydantic 在 wire 发射处隐式转枚举——拼错（`"public_replay"`）静态检查不报。
>
> 这是 ID 批次（core 定义 NewType、frontend 裸 str）的**镜像**：类型权威归属相反——channel/kind 权威在契约层
> （frontend_contract），core 能引契约层（依赖方向 core → frontend_contract 合法，已有 15 个 core 文件 import），
> 故 core 应对齐契约枚举，而非 core 自建（自建会双份漂移，违背"契约单源"）。

**Goal**：`GenerationContext.channel/kind` 与 `open_segment` 参数从裸 `str` 对齐为契约层
`MessageChannel`/`SegmentKind` StrEnum，并让枚举贯通到 wire 发射边界（manager/websocket handler）。
**运行时零变化**（StrEnum 序列化输出值，wire 字节不变）。

**Architecture**：core 顶层复用契约枚举（frontend_contract 零依赖，无环）。改动链路：
`generation.py`（字段+签名）→ `loop.py`/`manager.py`（调用点+inner_voice 发射）→ `websocket.py`（handler 参数）→ 契约构造。
测试 7 文件适配。

**Tech Stack**：Python 3.12 `enum.StrEnum`。测试 pytest（marker=unit/service），conda 环境 `Elysia`。

## Global Constraints

- 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003）。
- 禁相对导入，一律绝对导入：`from frontend_contract.ws_message import MessageChannel, SegmentKind`。
- 代码注释/README 用中文；中英混合可接受。
- **复用契约层枚举，不 core 自建**——frontend_contract 是 wire 契约单源（`make check-types` 门禁），
  core 自建 `SegmentChannel` 会双份漂移。
- **StrEnum 是 str 子类**：`isinstance(x, MessageChannel)` 有效（与 NewType 不同，无 isinstance 陷阱）；
  `str()/json.dumps()/pydantic` 均输出成员值（`"public_reply"`），wire 不变。
- `MessageChannel` 成员（ws_message.py:21-26）：`INNER_VOICE="inner_voice"`、`PUBLIC_REPLY="public_reply"`、`STATUS="status"`。
- `SegmentKind` 成员（ws_message.py:29-39）：`TEXT="text"`、`MARKDOWN="markdown"` + 预留 `IMAGE/VIDEO/AUDIO/FILE/STICKER/LINK`（值不变）。

---

## 现状分析（触及点全量枚举）

### 契约层权威枚举（生产零改动，frontend_contract 已是单源）

| 枚举 | 位置 | 成员 |
|------|------|------|
| `MessageChannel` | `frontend_contract/ws_message.py:21-26` | INNER_VOICE / PUBLIC_REPLY / STATUS |
| `SegmentKind` | `frontend_contract/ws_message.py:29-39` | TEXT/MARKDOWN/IMAGE/VIDEO/AUDIO/FILE/STICKER/LINK |

wire 契约字段已用枚举：`generation.segment.started/delta/completed` 的 `channel: MessageChannel`、
`kind: SegmentKind`（ws_message.py:338,339,362,363）；`generation.completed` 的
`channel: Literal[MessageChannel.PUBLIC_REPLY]`（:374，恒 public_reply，**不在本批次**）。

### core 侧裸 str（本次改动）

| # | 来源 | 位置 | 说明 |
|---|------|------|------|
| 1 | `GenerationContext.channel/kind` 字段 | `generation.py:48-49` | `str | None`，dataclass 字段 |
| 2 | `open_segment(channel: str, kind: str = "text")` | `generation.py:51` | 签名裸 str |
| 3 | `open_segment` 内赋值/close 清空 | `generation.py:65-66,79-80` | `self.channel/kind = ...` |
| 4 | loop 调用点（3 处） | `loop.py:949,1340,1355` | 全是 `open_segment("public_reply", "text")` |
| 5 | manager inner_voice 调用点 | `manager.py:436` | `ctx.open_segment("inner_voice", "text")`（**初稿遗漏，review 补**） |
| 6 | manager inner_voice 发射 | `manager.py:442,449` | 硬编码 `channel="inner_voice", kind="text"` 字面量（**未读 ctx.channel/kind**，与 :376/:420 透传 ctx 不一致） |
| 7 | websocket handler 参数 | `websocket.py:239,259` | `on_generation_segment_started/completed(channel: str, kind: str)`——wire 契约构造边界 |

### 透传/发射点（对齐后自然贯通，需改的类型标注）

| # | 位置 | 说明 |
|---|------|------|
| A | `Module/output/manager.py:376,420` | `_broadcast(..., channel=ctx.channel, kind=ctx.kind)` 已透传 ctx——枚举化后直接传枚举，**无需改** |
| B | `Module/output/manager.py:442,449` | inner_voice 硬编码字面量 → 改 `channel=ctx.channel, kind=ctx.kind`（与 A 模式一致，去硬编码） |
| C | `Module/output/websocket.py:239,259` | handler 参数 `channel: str, kind: str` → `MessageChannel`/`SegmentKind`（wire 边界，枚举贯通到契约构造） |

### 测试触达面（7 文件）

| 文件 | 位置 | 现状 | 类型 |
|------|------|------|------|
| `core/cognitive/tests/test_generation.py` | :37,40,45,58-60 | `open_segment("inner_voice"/"public_reply")` + `:40 assert ctx.channel == "inner_voice"` 裸 str 比较 | open_segment 调用 + 字段比较 |
| `core/cognitive/tests/test_v2_empty_response.py` | :50 | `open_segment("public_reply", "text")` | open_segment 调用 |
| `Module/output/tests/test_generation_conversation_id.py` | :29,53,75 | `open_segment(...)` + websocket handler 调用 `channel="public_reply", kind="text"` | open_segment + handler 调用 |
| `Module/output/tests/test_output.py` | :791 | websocket handler 调用 `channel="public_reply", kind="text"` | handler 调用 |
| `Module/output/tests/test_manager_v2.py` | :25 | `open_segment("public_reply", "text")` | open_segment 调用 |
| `Module/output/tests/test_generation_model.py` | :33 | `open_segment("public_reply", "text")` | open_segment 调用 |
| `frontend_contract/tests/test_ws_message.py` | :98,125,183,205,216,225 | 契约模型构造 `channel="public_reply", kind="text"` 裸 str | 契约构造（第 7 文件，初稿遗漏） |

### 无关概念（明确不动）

- `core/text/` 的 `kind`（tokenizer `TokenKind`）——分词器概念，同名无关。
- `loop.py:981,1309` 的 `"inner_voice"` 字面量——日志/事件参数，非 open_segment。
- `websocket.py:273` `on_generation_completed(channel: str = "public_reply")`——父回复 channel（:374 对应），不在本批次。
- `frontend_contract` 生产代码——枚举已存在，零改动。

---

## 设计

### 1. `core/cognitive/generation.py` 对齐契约枚举

顶层 import（frontend_contract 零依赖，无环；`kind: SegmentKind = SegmentKind.TEXT` 默认值需真实对象，不能字符串注解）：

```python
from frontend_contract.ws_message import MessageChannel, SegmentKind
```

`GenerationContext` 字段（:48-49）：

```python
    channel: MessageChannel | None = None
    kind: SegmentKind | None = None
```

`open_segment` 签名（:51）：

```python
    def open_segment(self, channel: MessageChannel, kind: SegmentKind = SegmentKind.TEXT) -> SegmentId:
```

（body 的 `self.channel = channel`/`self.kind = kind`/close 清 None 自动一致——字段类型已对齐。）

### 2. 调用点（loop.py 3 处 + manager.py inner_voice）

`core/cognitive/loop.py:949,1340,1355`：

```python
ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)
```
（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`——loop.py 已有 lazy import 先例 :1022，此处改顶层 import。）

`Module/output/manager.py` inner_voice 路径（:436,442,449）：

```python
        ctx.open_segment(MessageChannel.INNER_VOICE, SegmentKind.TEXT)
        ...
        await self._broadcast(
            "on_generation_segment_started",
            generation_id=ctx.generation_id, segment_id=sid, segment_index=idx,
            channel=ctx.channel, kind=ctx.kind,          # 改：去硬编码字面量，透传 ctx（对齐 :376 模式）
            conversation_id=ctx.conversation_id,
            channels=v2,
        )
        await self._broadcast(
            "on_generation_segment_completed",
            generation_id=ctx.generation_id, segment_id=sid, segment_index=idx,
            channel=ctx.channel, kind=ctx.kind, content=text,   # 改：同上
            ...
        )
```
（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`。）

### 3. wire 边界 handler 参数（`Module/output/websocket.py`）

`on_generation_segment_started`（:239）/`on_generation_segment_completed`（:259）：

```python
    async def on_generation_segment_started(self, *, generation_id: str, segment_id: str,
                                            segment_index: int, channel: MessageChannel, kind: SegmentKind,
                                            conversation_id: str | None = None) -> None:
    async def on_generation_segment_completed(self, *, generation_id: str, segment_id: str,
                                              segment_index: int, channel: MessageChannel, kind: SegmentKind,
                                              content: str | None = None,
                                              conversation_id: str | None = None) -> None:
```
（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`；body 构造契约模型 `channel=channel, kind=kind` 直接传枚举。）

`on_generation_completed`（:273）`channel: str = "public_reply"` **不改**（父回复 channel，不在本批次）。

### 4. 测试适配（7 文件）

- `test_generation.py`：:37 `open_segment(MessageChannel.INNER_VOICE)`；:40 `assert ctx.channel == MessageChannel.INNER_VOICE`；
  :45,58-60 `open_segment(MessageChannel.PUBLIC_REPLY)`。
- `test_v2_empty_response.py:50`、`test_manager_v2.py:25`、`test_generation_model.py:33`、
  `test_generation_conversation_id.py:29`：`open_segment("public_reply", "text")` →
  `open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)`。
- **websocket handler 调用点**（handler 参数已枚举化）：`test_generation_conversation_id.py:53,75`、
  `test_output.py:791`：`channel="public_reply"` → `channel=MessageChannel.PUBLIC_REPLY`、
  `kind="text"` → `kind=SegmentKind.TEXT`。
- **frontend_contract 契约构造**：`test_ws_message.py:98,125,183,205,216,225`：
  `channel="public_reply"` → `channel=MessageChannel.PUBLIC_REPLY`、`kind="text"` → `kind=SegmentKind.TEXT`。
- 各文件 import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`。

### 5. 文档

- `docs/architecture.md`：generation.py 描述补 channel/kind 对齐契约枚举。
- `CLAUDE.md`：cognitive 描述补一句。

### 依赖方向说明

`core/cognitive/generation.py` 与 `Module/output/` 顶层依赖 `frontend_contract`——frontend_contract 零依赖叶子
（不引 core/Module），core/Module → frontend_contract 单向无环（已有 15 个 core 文件 import 先例）。channel/kind
是 wire 协议概念（分段通道/类型），领域持有它们作"待发射分段元数据"自然。**不做** core 自建枚举（双份漂移）。

---

## 行为变化（须在最终报告中声明）

1. **运行时零变化**：StrEnum 序列化输出值（`MessageChannel.PUBLIC_REPLY` → `"public_reply"`），
   wire 字节不变、前端无感、`make check-types` 不受影响（frontend_contract 生产零改动）。
2. **静态类型变化**：`open_segment` 参数、`GenerationContext.channel/kind` 字段、websocket handler 参数从裸 `str`
   变为 `MessageChannel`/`SegmentKind`。**价值为 IDE/编辑器级 + 运行时防成员名拼错**
   （`MessageChannel.PUBLIC_REPLAY` 若拼错成员名抛 AttributeError；裸字面量 `"public_replay"` 只在 pydantic wire
   校验处被拒）。**诚实声明：项目无 mypy/pyright 门禁（仅 ruff TID252+ANN），跨调用参数类型不强制**——
   与 ID 批次 NewType 同为 IDE 级文档标注，非 CI 强制。
3. **manager inner_voice 发射去硬编码**：`channel="inner_voice", kind="text"` 字面量 → `ctx.channel/ctx.kind`
   （消除重复，单一数据源）——**唯一一处行为对齐**（wire 值不变，仅数据来源统一）。
4. 测试中裸 str 直传统一为枚举——类型卫生。

## 测试策略

**新增（marker=unit）：**
- `test_generation.py` 补 `open_segment` 参数类型断言（`get_type_hints(GenerationContext.open_segment)`：
  `channel is MessageChannel`、`kind is SegmentKind`）。

**适配现有测试**：§4 清单（7 文件，open_segment 调用 + handler 调用 + 契约构造 + 字段比较改枚举）。

**回归**：`make test-normal` + 受影响模块定向跑（core/cognitive/tests、Module/output/tests、frontend_contract/tests、core/input/tests）。

## Out of Scope

- 不动 frontend_contract **生产代码**（枚举已存在，零改动）；其测试 `test_ws_message.py` 适配（类型卫生）。
- 不动 `core/text/` 的 `TokenKind`（分词器概念，同名无关）。
- 不动 `generation.completed` 的 `Literal[MessageChannel.PUBLIC_REPLY]` 与 `on_generation_completed` 的
  `channel: str = "public_reply"`（父回复 channel）。
- 不做 core 自建枚举。
- 不把 loop.py 其他 `"inner_voice"` 字面量（日志/事件）枚举化——非 open_segment 参数。

## 决策记录（用户 /goal 要求记录，最终报告提出）

1. **复用契约层枚举（非 core 自建）**：用户咨询后拍板。channel/kind 权威已在 frontend_contract
   （wire 契约单源），core 复用而非自建——自建 `SegmentChannel` 会双份漂移，违背 `make check-types` 单源原则。
   与 ID 批次方向相反（ID：core 定义 NewType、frontend 裸 str；channel/kind：契约层定义、core 对齐）——类型权威归属 + 依赖方向决定。
2. **贯通到 wire 边界（manager inner_voice + websocket handler）**（review 修复，范围扩大）：初稿只改
   generation.py/loop.py，review 发现 manager.py:436 直接调 open_segment + :442/:449 硬编码字面量、
   websocket.py handler 是 str 参数——为让枚举真正贯通到契约构造，并入 manager inner_voice 发射（去硬编码，
   读 ctx.channel/kind）与 websocket handler 参数（MessageChannel/SegmentKind）。
3. **generation.py/manager.py 顶层 import 契约层**：扩大核心逻辑对契约层的耦合。可接受（零依赖无环、
   channel/kind 是协议概念、loop.py:1022 先例）；代价是核心领域对象类型依赖 wire 契约——若契约形状变化，
   core 需同步。已权衡，选复用。
4. **诚实的价值声明**：项目无 mypy/pyright，跨调用参数类型不强制——本批次价值为 IDE 级 + 运行时防成员名拼错
   （AttributeError）+ wire 校验前置。非 CI 门禁，与 ID 批次 NewType 同等性质。

## Spec Review 修复记录（code-review 后并入）

| # | Review 发现 | 修复 |
|---|------------|------|
| 1 | manager.py:436 直接 `open_segment("inner_voice", "text")`，:442/:449 硬编码字面量——初稿漏 | 并入 manager inner_voice 调用点 + 发射去硬编码读 ctx.channel/kind |
| 2 | "测试适配 7 文件" vs 表列 6 文件，第 7 文件未命名 | 补 frontend_contract/tests/test_ws_message.py（6 处契约构造）为第 7 文件 |
| 3 | "拼错静态报错" 夸大——项目无 mypy 门禁 | 行为变化 §2 诚实声明：IDE 级 + AttributeError + wire 校验，非 CI 强制 |
| 4 | test_generation_conversation_id.py:53,75 / test_output.py:791 误标"契约构造点"——实为 websocket handler 调用 | 改标"websocket handler 调用点"，并入 handler 参数枚举化（websocket.py） |
| 5 | test_generation.py:40 `ctx.channel == "inner_voice"` 裸 str 比较漏配 | 补进适配清单（:40） |
| 6 | 行号 off-by-one：generation.py:47-48→48-49、ws_message.py:20-26→21-26、:29-38→29-39 | 全部修正 |

## Self-Review（已执行）

1. **Spec 覆盖**：字段对齐（§1）、调用点 loop+manager（§2）、wire 边界 handler（§3）、测试适配 7 文件（§4）、
   文档（§5）、依赖方向、行为声明（含诚实价值声明）、决策记录、review 修复记录——全部映射。
2. **占位符扫描**：无 TBD/TODO；每处含可执行签名与位置。
3. **类型一致性**：`MessageChannel`/`SegmentKind` 引用与契约层定义一致（成员名/值）；测试适配统一用枚举成员。
4. **已知耦合**：generation.py/manager.py/websocket.py 顶层 import 契约层（决策 3）；loop.py 与 manager.py 调用点
   原子变更；manager inner_voice 去硬编码是唯一行为对齐（wire 值不变）；wire 发射点 A（:376/:420）零改动。
