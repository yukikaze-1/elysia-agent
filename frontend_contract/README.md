# frontend_contract — 前后端契约单源包

> 文档导航：[项目文档](../README.md)

前后端契约的**唯一权威来源**（可执行 Pydantic 模型）。契约模型 → `gen_ts.py` 发射器 →
`frontend/src/types/contract.generated.ts`（Vue TS 只读快照）+ `gen_schema.py` →
`contract.schema.json` + `gen_ts.generate_raw_ts` → `contract.raw.ts`（Electron 机器契约，批次 9B）。
后端发送点构造契约模型（`wire_dict()` 序列化）获得运行时校验；前端 TS 类型由生成器产出，**禁止手写**。
零依赖硬约束：仅 pydantic + stdlib，不 import `core/` / `Module/` 任何代码。

## 为什么需要单源

历史上前端 WS/REST/Debug 类型在后端手写 dict + 前端 `protocol.ts` / `debug/types.ts` 手写
interface 各维护一份，两端漂移只能靠联调发现。本包把契约收敛到 Python 一处：

- 后端（`core/input/` 等发送点）直接构造 `ContractModel` 子类，`wire_dict()` 序列化 →
  运行时校验 + wire 键统一
- 前端 `frontend/src/types/contract.generated.ts` 由 `gen_ts` 发射器生成（`// DO NOT EDIT` 只读快照）
- `make check-types`（`python -m frontend_contract --check`）做漂移门禁：改契约不跑生成 = CI 红

## 包结构

```
frontend_contract/
├── base.py                 # ContractModel 基类 + TsNull marker + wire_key + wire_dict 序列化
├── version.py              # TRANSPORT_VERSION="2"（握手头可用值）/ PROTOCOL_VERSION="2.0"（仅展示）——协议版本单源（批次 9B）
├── ws_message.py           # WS 消息契约：ServerMessage 判别联合（38 具名成员）+ ClientMessage 判别联合（批次 9B，不含 TextMessage）+ 支持类型
├── rest.py                 # REST 业务契约：providers / images / messages / api 响应（44 模型）
├── debug.py                # Debug API 契约：/debug/api 响应（DebugData 16 面板等 38 模型）
├── gen_ts.py               # TS 发射器：BaseModel → contract.generated.ts（拓扑排序 + 同名安全网）+ generate_raw_ts（raw wire 快照）
├── gen_schema.py           # JSON Schema 发射器：Annotated 判别联合（get_origin 收集）+ BaseModel → contract.schema.json
├── contract.schema.json    # 生成产物：机器契约 JSON Schema（transportVersion/protocolVersion/schemaHash，批次 9B）
├── contract.raw.ts         # 生成产物：Electron 可消费 raw wire 类型快照（批次 9B）
├── __main__.py             # python -m frontend_contract：动态收集子模块 → 生成 ts/schema/raw_ts 三产物 / --check 漂移检测
├── __init__.py             # 重导出全部契约模型 + __all__
└── tests/                  # 契约建模测试（marker=unit，无 I/O）
```

生成产物：`frontend/src/types/contract.generated.ts`（Vue 只读快照）+ `contract.schema.json` / `contract.raw.ts`
（Electron 机器契约，提交入库）。漂移门禁 `make check-types` = `python -m frontend_contract --check`（ts/schema/raw_ts）
+ `python scripts/gen_contract_frames.py --check`（golden frames）——改契约不跑生成 = CI 红。

> 契约已覆盖批次 5/6/7 全部新增面：多会话 REST（session_routes）、Desktop 附件上传、
> 附件/表情（AttachmentInfo/StickerEntry 等）、三种语音模式（Voice*/Dictation*）与
> TTS 播放控制（AudioStart/AudioCancelled）。<br>
> 历史包 `frontend-handoff-v2/` 已被本包取代，仅作 wire 形状参考。

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

## 生成 / 校验命令

```bash
# 生成 TS 快照（改动契约后必跑）
make gen-types

# 漂移检测：生成到临时文件后 git diff 校验（CI / 提交前）
make check-types
# 或: python -m frontend_contract --check

# 契约建模测试（marker=unit）
python -m pytest frontend_contract/tests/ -v
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 文档导航 |
| [docs/architecture.md](../docs/architecture.md) | 完整架构 |
| [docs/electron-backend-protocol-v2.md](../docs/electron-backend-protocol-v2.md) | Electron 后端联调协议（wire 格式说明） |
