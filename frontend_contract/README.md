# frontend_contract — 前后端契约单源包

> 文档导航：[项目文档](../README.md)

前后端契约的**唯一权威来源**（可执行 Pydantic 模型）。契约模型 → `gen_ts.py` 发射器 →
`frontend/src/types/contract.generated.ts`（Vue TS 只读快照）+ `gen_schema.py` →
`contract.schema.json` + `gen_ts.generate_raw_ts` → `contract.raw.ts`（Electron 机器契约，批次 9B）。
后端发送点构造契约模型（`wire_dict()` 序列化）获得运行时校验；前端 TS 类型由生成器产出，**禁止手写**。
零依赖硬约束：仅 pydantic + stdlib，不 import `core/` / `Module/` 任何代码。

**人类可读的 wire 契约**（事件序列、字段语义、错误码目录）在
[`docs/electron-backend-protocol-v2.md`](../docs/electron-backend-protocol-v2.md)；
本 README 只讲**本包怎么组织、怎么生成、怎么建模**。

## 为什么需要单源

历史上前端 WS/REST/Debug 类型在后端手写 dict + 前端 `protocol.ts` / `debug/types.ts` 手写
interface 各维护一份，两端漂移只能靠联调发现。本包把契约收敛到 Python 一处：

- 后端（`core/input/` 等发送点）直接构造 `ContractModel` 子类，`wire_dict()` 序列化 →
  运行时校验 + wire 键统一
- 前端 `frontend/src/types/contract.generated.ts` 由 `gen_ts` 发射器生成（`// DO NOT EDIT` 只读快照）
- `make check-types` 做漂移门禁：改契约不跑生成 = CI 红

## 包结构

```
frontend_contract/
├── base.py                 # ContractModel 基类 + TsNull marker + wire_key + wire_dict 序列化
├── version.py              # TRANSPORT_VERSION="2"（握手头可用值）/ PROTOCOL_VERSION="2.0"（仅展示）——协议版本单源（批次 9B）
├── ws_message/             # WS 消息契约**包**（ARCH-INC-P2-02 物理拆分，原单文件 → 9 模块）
│   ├── __init__.py         #   **单一装配点**：唯一 ServerMessage / ClientMessage 联合定义位置 + 显式 re-export
│   ├── _types.py           #   共享词汇：ContentPart 联合 + 全部非联合成员类型（零跨模块依赖）
│   ├── chat.py             #   chat 域（Delta/Message/Token/Audio/Expression/Live2D/State/Typing/Transcription…）
│   ├── session.py          #   session 域（Session/Capabilities/ServerInfo/Command/Wake/SwitchModel/ServiceMode…）
│   ├── tool.py             #   tool 域（ToolStarted/ToolCompleted/ToolRequest/ToolStatus/ToolCall*）
│   ├── generation.py       #   generation 域（Started/SegmentStarted/Delta/SegmentCompleted/Completed/Failed）
│   ├── voice.py            #   voice 域（Voice* / Dictation* + TTS 播放控制 Audio*）
│   ├── ack.py              #   ack 域 + UserAckMessage 嵌套联合
│   └── character.py        #   character 域（CharacterStateChanged）
├── rest.py                 # REST 业务契约（providers / images / messages / 多会话 / Desktop / 附件与表情 / 角色域）
├── debug.py                # Debug API 契约（/debug/api 响应，含 DebugData 面板模型）
├── errors.py               # 错误码单源（KNOWN_ERROR_CODES / KNOWN_ISSUE_CODES）
├── gen_ts.py               # TS 发射器：BaseModel → contract.generated.ts（拓扑排序 + 同名安全网）+ generate_raw_ts（raw wire 快照）
├── gen_schema.py           # JSON Schema 发射器：Annotated 判别联合（get_origin 收集）+ BaseModel → contract.schema.json
├── contract.schema.json    # 生成产物：机器契约 JSON Schema（transportVersion/protocolVersion/schemaHash，批次 9B）
├── contract.raw.ts         # 生成产物：Electron 可消费 raw wire 类型快照（批次 9B）
├── __main__.py             # python -m frontend_contract：动态收集子模块 → 生成 ts/schema/raw_ts 三产物 / --check 漂移检测
├── __init__.py             # 重导出全部契约模型 + __all__
└── tests/                  # 契约建模测试（marker=unit，无 I/O）
```

### `ws_message/` 的装配规则（拆包后最容易踩的一条）

`ServerMessage` / `ClientMessage` 两个判别联合**只**在 `ws_message/__init__.py` 定义，
其余模块只放成员类型。原因是产物顺序：`gen_ts` 此前按“定义在哪个文件”决定 TS 输出顺序，
把单文件拆成包会让产物重排（一次性、纯位移的 4296 行 diff）。因此：

- 新增 WS 消息 → 加到对应**领域模块**，再在 `__init__.py` 的联合里登记；
- 不要在领域模块里 import 联合本身（会成环，且 `make check-arch` 的 R4 会红）；
- 对外兼容面不变：`from frontend_contract.ws_message import X` 与拆分前逐字一致。

> **成员数不在文档里维护**：联合的成员集合由 `ws_message/__init__.py` 定义，
> 是唯一真值；README/architecture 都不抄写条数（抄写的那份不会被门禁发现）。

## 生成产物

| 产物 | 消费者 | 生成器 |
|---|---|---|
| `frontend/src/types/contract.generated.ts` | Vue 前端（只读快照） | `gen_ts.py` |
| `contract.schema.json` | Electron 机器契约 / replay 主源 | `gen_schema.py` |
| `contract.raw.ts` | Electron raw wire 类型快照 | `gen_ts.generate_raw_ts` |
| `docs/archive/frontend-handoff-v2/traces/*.jsonl` | golden frames（跨仓消费者） | `scripts/gen_contract_frames.py` |

golden frames 位于**历史路径**下但属**当前生效的契约产物**（CI 门禁 + 跨仓消费者）；
路径不得移动，理由与清单见
[`docs/archive/frontend-handoff-v2/traces/README.md`](../docs/archive/frontend-handoff-v2/traces/README.md)。

## 建模硬性要求

给 `frontend_contract/` 加新契约字段/消息时，必须遵守（违反会导致 `make check-types` 漂移或
`gen_ts` 报错）：

1. **每个字段必须带 docstring 注释**（`Field(description=...)`）——`gen_ts` 用 `inspect.getdoc`
   把字段注释发射为 TS JSDoc（`/** ... */`）。字段注释 = 前端类型文档的唯一来源。
2. **`ts_null` vs `X?` 定性**（必填/可缺省语义由发送点实际 wire 决定，不由 Python 类型惯性决定）：
   - 字段 wire **恒写**、可缺省时才写 `null` → `Annotated[Optional[T], TsNull()]`，生成器发射 `T | null`
   - 字段**可缺省省略** → `Optional[T] = None`，生成器发射 `T?`
   - 字段**恒在** → `T`（必填）
   - 示例：`service_mode_result.error` 恒写 → `TsNull`；`switch_model_result.error` 成功无键 → `X?`；
     `CapabilitiesMessage.services.*.model` available=false 时显式 null → `TsNull`
3. **wire 键与 Python 字段名可能不同**：用 `Field(serialization_alias=..., validation_alias=...)`
   建模（如归档消息 `_id` → `mongo_id` 字段 + serialization_alias），`wire_dict` 与 gen_ts
   都按 wire 键发射。
4. **跨消息同名但形状不同 → 必须改名**：`gen_ts` 有同名安全网（同名不同类型抛 ValueError，防静默
   合并成非法 TS）。如 debug 的 `ContentPart`→`DebugContentPart`、`TokenStats`→`DebugTokenStats`。
5. **零依赖硬约束**：禁止 `from core.` / `from Module.`——契约包不感知领域对象，内部领域对象
   （`core.schema.Message` 等）的 wire 形状自建"契约视图"。
6. **新增枚举成员必须有生产者**：`make check-contract` 会检查枚举值域是否有生产代码产出，
   预留值必须登记并写明「解除条件」。

## 生成 / 校验命令

按 `Makefile` 的真实定义（别手抄这份清单，直接读 `Makefile` 的 `gen-types` / `check-types`）：

```bash
# 重新生成 TS / schema / raw_ts 三产物（改动契约后必跑）
make gen-types

# 重新生成 golden frames
make gen-frames

# 漂移门禁（CI / 提交前）——三条命令
make check-types
#   = python -m frontend_contract --check        # ts / schema / raw_ts
#   + python scripts/gen_contract_frames.py --check   # golden frames
#   + python scripts/check_error_codes.py        # 错误码目录 ↔ errors.py 双向

# 契约建模测试（marker=unit）
python -m pytest frontend_contract/tests/ -v
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [docs/electron-backend-protocol-v2.md](../docs/electron-backend-protocol-v2.md) | **人类可读的 wire 契约**：事件序列、字段语义、错误码目录（权威） |
| [docs/electron-backend-security.md](../docs/electron-backend-security.md) | 契约相关的安全边界（认证/签名/限流/幂等） |
| [docs/archive/frontend-handoff-v2/traces/README.md](../docs/archive/frontend-handoff-v2/traces/README.md) | golden frames 清单 + 再生成/漂移检测 + 数据保护说明 |
| [docs/architecture.md](../docs/architecture.md) | 契约层在整体架构中的位置（§6.2 / §12） |
| [docs/readme-conventions.md](../docs/readme-conventions.md) | README 编写规范（含**易腐事实**纪律） |
