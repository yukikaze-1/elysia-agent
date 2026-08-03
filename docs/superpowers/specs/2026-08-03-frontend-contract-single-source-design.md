# 前后端契约单源化设计（frontend_contract）

## Goal

把前后端数据交换契约（WS 消息 + 业务 REST + Debug API）从「后端手写 dict + 前端手写 TS + markdown 文档」三处手动同步，收敛为**一份可执行的 Pydantic 模型**（顶层独立包 `frontend_contract/`），前端 TypeScript 类型由它生成。后端发送点构造模型获得运行时校验，前端拿到编译期类型守护，契约变更只改一处。

## 背景与问题

1. **前端契约集中但后端散落**：`frontend/src/types/protocol.ts` 一个文件承载全部消息类型（`ServerMessage` union 19 个成员含 `UnknownMessage` + 客户端消息 + 支持类型），被 TS 编译器守护；后端**无任何 WS 消息帧的 Pydantic 模型**（`core/schema` 零命中 delta/audio/service_mode_result），手写 dict 散落在 `ws_handler.py`（~12 处 send_json，含 switch/service_mode_result）、`capabilities.py`（broadcast 1 处）、`api_routes.py`、`debug_routes/`（7 文件）。后端拼错字段名只在运行时暴露。
2. **三处手动同步**：本特性亲历 capabilities 从扁平改嵌套——需同时改后端 `build_capabilities` + 前端 `protocol.ts` + `frontend-api.md`，任何一处漏改就是静默漂移。
3. **文档是唯一权威但不可执行**：`docs/frontend-api.md` 是面向第三方（.NET/WPF）的完整契约，但是人维护的 markdown，不是可执行的类型。
4. **项目已有契约层先例**：`Module/workflow_contract`（core ↔ workflow_engine 跨边契约层，唯一类型来源、零业务逻辑）证明"抽契约层"在本仓库是成功实践——但未应用到前后端边界。

## 架构

```
frontend_contract/                # 顶层独立包，与 core/ Module/ 平级（Pydantic 唯一权威）
  ws_message.py                   # WS 消息帧：ServerMessage 判别联合 + 客户端消息 + 支持类型
  rest.py                         # 业务 REST：providers/images/messages/chat/health 响应
  debug.py                        # Debug API：DebugData 及子类型
        │
        ├── 后端消费：发送点构造模型 → 字段校验 → wire_dict() 序列化（exclude_none + ts_null 显式 null）
        │
        └── gen_ts.py（内置发射器，零依赖）← python -m frontend_contract
              │
              ▼
        frontend/src/types/contract.generated.ts   # 生成快照（// DO NOT EDIT）
              │
              └── 前端 import：protocol.ts re-export，现有 import 路径不变
```

**关键约定**：
- **单源在后端**（Pydantic 作源，非 TS）——后端是消息生产者，谁产生谁定义；与 `workflow_contract` 心智一致。
- **生成的 TS 是只读快照**——任何人不得手改；CI `make check-types` 用 git diff 检测漂移。
- **单向生成**（Python → TS）——后端是运行时源，前端类型是快照；"后端改字段前端立刻知道"靠 CI 漂移检测，不靠实时同步。
- `frontend-api.md` 降级为参考文档（契约改完跑生成器 + 同步文档）。

## 契约包：frontend_contract/

```
frontend_contract/
├── __init__.py              # 重导出全部契约类型（统一入口，对齐 core.schema 惯例）
├── base.py                  # ContractModel 基类（wire_dict 递归序列化）+ TsNull marker
├── ws_message.py            # WS 消息帧
├── rest.py                  # 业务 REST 响应
├── debug.py                 # Debug API 响应
├── gen_ts.py                # B2 发射器（零第三方依赖）
├── __main__.py              # python -m frontend_contract → 跑 gen_ts
├── tests/                   # 契约模型测试 + 生成器快照测试
└── README.md                # 面包屑 + 相关文档
```

**硬约束（对齐 `workflow_contract` 先例）**：
- **零业务逻辑**：纯类型定义 + 序列化基类（`ContractModel`，统一 `wire_dict()` 递归）+ 生成器；**不 import `core/` 或 `Module/`**——只依赖 pydantic + stdlib，任何工具（前端生成器、后端发送点、脚本）都能安全引用。
- **模型即运行时校验**：后端 `from frontend_contract import CapabilitiesMessage` 构造模型，字段拼错/漏字段在构造时抛 `ValidationError`。
- **契约视图模型（wire 形状，包级硬约束）**：**`ws_message.py` / `rest.py` / `debug.py` 全部模型都是独立的 wire 形状**（扁平 Pydantic），**不与 `core.schema` 共享领域模型**（`core.schema` 的 `Message`/`MessageRole`/`CompressionTrigger`/`Part` 是内部领域类型，不是 wire 契约——包括 WS 消息的 content parts，如 `MessageMessage.content`/`parts` 引用的 `ContentPart`，也必须在契约包自建 wire 形状）。后端发送点把内部对象**转换**成契约模型再序列化——`frontend_contract` 保持零依赖，`core.schema` 也不反向依赖它（单向：发送点 import 契约）。无双源：wire 契约唯一在 `frontend_contract`，`core.schema` 是内部表示。

### 类型映射约定（发射器规则，两端一致）

| Pydantic | TypeScript | 说明 |
|----------|-----------|------|
| `Literal["delta", "audio"]` | `'delta' \| 'audio'` | 字面量枚举 |
| `dict[str, X]` | `Record<string, X>` | providers 动态 key 用 |
| `X`（非 Optional，**无论有无默认**） | `X` | 必填字段 |
| `X \| None = None` | `X?` | **可缺省**（wire 缺省省略） |
| `Annotated[X \| None, ts_null]` | `X \| null` | **可为 null**（wire 显式 null） |
| `Annotated[..., Field(discriminator="type")]` | 判别联合（各成员 `type: 'delta'` 字面量） | |
| 嵌套 BaseModel | interface（拓扑序输出） | |

> **必填判定规则**：发射器按 **Python 类型是否 `Optional`（`Union[X, None]`）判定**，**不看 JSON Schema 的 `required`**。`enabled: bool = True`（非 Optional 带默认，如 `service_mode_result` 的 `enabled`）→ **必填** `enabled: boolean`——它在 schema 里非 required（Pydantic 默认值字段不在 required），但语义上该字段永远随消息出现。这一规则也**让判别字段 gotcha 自动消失**（`type: Literal["delta"] = "delta"` 非 Optional → 必填，无需特判）。

### 可缺省 vs 可为 null（关键语义决策）

Pydantic 中 `X | None = None` 与 `Annotated[X | None, ...]` 的 **schema 等价**（都是 anyOf [X, null]、非 required）——生成器无法仅凭类型区分两种语义。**必须用显式 marker（`ts_null`，经 `json_schema_extra` 声明）表达"可为 null"，否则默认按"可缺省"处理**：

- **可缺省**（默认）：`field: X | None = None` → 前端 `field?: X`，序列化 `exclude_none=True` → wire **缺省省略**。对齐 protocol.ts 主导风格（56 处 `field?: X`，占可选字段绝对多数）。
- **可为 null**（显式 marker）：`field: Annotated[X | None, ts_null]` → 前端 `field: X | null`，该字段在 wire 上**显式 null**。用于 `capabilities.model`（available=false 时 null）。
- **ts_null vs X? 必须逐发送点按实际 wire 行为定**（不能一刀切按字段名归类）：
  - **键恒存在**（含成功时 null）→ `ts_null`：如 `service_mode_result.error`（ws_handler `result.get("error")` 恒写，成功为 null）。
  - **键缺省省略**（成功时无此键）→ `X?`：如 `switch_model_result.error`（`**result` 展开，switch_* 成功返回 `{"ok": True}` 无 error 键，仅失败带）。**盲建模 ts_null 会让 switch_model_result 成功路径新增 `"error": null`**——正是"不做全 null 归一化"要避免的线上变更。

**序列化约定**：发送点**统一走 `wire_dict()`，不用裸 `model_dump`**。`exclude_none=True` 是**递归**的——嵌套模型里 `services.stt.model = None` 会被整层丢掉。因此 `wire_dict()` 必须递归补 null：
- 每个契约模型实现 `wire_dict()`：`model_dump(mode="json", exclude_none=True)` 后，对自身 `ts_null` 字段为 None 时补 `field: null`；
- **嵌套处理**：字段值为嵌套契约模型 → 递归调其 `wire_dict()`；`dict[str, ContractModel]` 的值 → 逐个递归。顶层消息模型的 `wire_dict()` 由此递归到两层深的 `services.stt.model`，保证"available=false 时 `model` 键恒存在且为 null"的契约成立。

> 包内用统一基类 `ContractModel(BaseModel)` 实现 `wire_dict()` 递归（声明 `ts_null` 字段名集合），避免各模型散落手写补 null。

> **为何不做"全 null 归一化"**：把全部 `X?` 字段改 wire 显式 null 是对 .NET 第三方客户端（frontend-api.md）的**线上格式变更**，且与 protocol.ts 主导风格（`field?: X`）冲突——改动大、收益低。

### 判别字段约束

`type: Literal["delta"] = "delta"` 带默认值——按上节"必填判定规则"（按 Python 类型非 Optional → 必填），**天然生成 `type: 'delta'`（不带 `?`）**，无需特判。模型保留默认值仅为后端构造便利（不必每次写 type）。PoC 曾发现"按 schema required 判定会产出 `type?`"的 gotcha——已被"按 Python 类型判定"的统一规则消除。

### UnknownMessage 兜底保留（向前兼容）

现有 `protocol.ts` 的 `ServerMessage` union 含 **`UnknownMessage`**（`{ type: string; [key: string]: unknown }`）——前端对未知消息的向前兼容兜底。它**无法进入 Pydantic 判别联合**（判别字段必须各成员 literal 互异，catch-all 进不去）。**生成器特判追加**：判别联合生成后，末尾追加 `UnknownMessage` 成员（发射器内固定 emit），保持前端兜底语义不丢。

## 生成器：gen_ts.py（B2 内置发射器）

**归属**：逻辑放契约包内 `frontend_contract/gen_ts.py`，入口 `python -m frontend_contract`，Makefile/CI 调用。

**输入 → 输出**：
- 输入：`frontend_contract` 全部 BaseModel（import 即收集）
- 输出：`frontend/src/types/contract.generated.ts`，文件头 `// DO NOT EDIT — generated by python -m frontend_contract`

**发射规则**：
1. **拓扑排序**：被引用模型先输出（`ServiceCapability` 先于 `CapabilitiesMessage`），保证 TS 引用顺序合法。
2. **docstring → JSDoc**：Python `"""..."""` → `/** ... */`。**前提是建模硬性要求**：每个契约模型 + 字段必须写中文 docstring（见"建模硬性要求"节）——否则生成器无注释可保留。
3. **判别联合**：收集 `Annotated[..., Field(discriminator="type")]` 别名 → `export type ServerMessage = A | B | ...`，**末尾追加 `UnknownMessage` 兜底**（见上节）。
4. **必填判定（统一规则）**：按 Python 类型是否 `Optional` 判定（**非看 schema required**）——非 Optional（含带默认的 `type`/`enabled`/`ok`）→ 必填 `X`；`X | None = None` → `X?`；`Annotated[X | None, ts_null]` → `X | null`。判别字段 `type: Literal[...]` 走"非 Optional → 必填"分支。

**建模硬性要求**（生成注释 + 语义正确的前提）：
- 每个契约模型类写中文 docstring（生成 JSDoc 保留注释质量）。
- 每个字段写简短中文注释（可选字段注明"可缺省"或"可为 null"）。
- `ts_null` marker 用 `Annotated[X | None, Field(json_schema_extra={"ts_null": True})]` 或包内类型别名（`TsNull[X]`），发射器识别后生成 `X | null` 且该字段在 `wire_dict()` 保留显式 null。

**关键设计约束**：
- **可重复性**：同模型集两次运行字节一致（`check-types` 靠 diff 检测漂移的前提）。
- **纯函数**：只读契约包 + 写一个输出文件，无其他副作用。

## 集成与流程

### Makefile / CI 门禁

```makefile
gen-types:        # python -m frontend_contract → 写 contract.generated.ts
check-types:      # 跑生成器到临时文件，git diff 校验无漂移
```

- **`make lint` 扩展**：现为 `ruff check core/ Module/`，顶层新包 `frontend_contract/` 不在覆盖内——**必须改为 `ruff check core/ Module/ frontend_contract/`**，否则导入风格门禁（TID252 禁相对导入）对新包是假绿。
- **根 README 登记**：按 `docs/readme-conventions.md`，新包在根 README 文档导航注册 `frontend_contract/README.md`。
- **CI**：现有 `Lint` 步骤后加 `make check-types`——改 Pydantic 契约不跑生成器 = CI 红。前端构建依赖生成文件（vue-tsc 直接检查生成类型）。

### 前端切换

- 生成器输出 `frontend/src/types/contract.generated.ts`（独立文件）。
- `protocol.ts` 契约相关类型**删除**，改为 `export * from './contract.generated'`——现有 `ServerMessage` 路由、组件 import 路径不变（re-export 向后兼容）。
- **纯前端辅助类型**（chatStore 内部状态、UI 专用结构）**有意保留手写**，与生成区明确分隔。

### 后端发送点改造（迁移主体，拿到运行时校验）

- 手写 dict → 构造模型 + `wire_dict()` 序列化（`exclude_none=True`，`ts_null` 字段补显式 null）。
- **实际覆盖清单**（以代码为准，避免误归属）：
  - `ws_handler.py`（~12 处 send_json）——**含 `switch_model_result`（L226）与 `service_mode_result`（L246）的构造发送都在此**，不在 server.py。
  - `capabilities.py`——`broadcast_capabilities` 的发送处（`_send_json` 1 处）+ `build_capabilities` 组装改造为构造模型。
  - `api_routes.py`——providers/images/messages/chat 响应。
  - `debug_routes/`（7 文件）——响应改为契约模型 + 内部 `core.schema` 对象转契约视图（见"契约视图模型"硬约束）。
  - `core/agent/server.py`——`switch_*`/`set_service` **只返回 dict 不发帧**，无需改造发送；若结果结构纳入契约，改为返回契约模型。
- **行为等价重构**：字段名/默认值严格对齐现有 dict，以测试全绿为准（含 `.NET` 第三方可见的 wire 格式——见"可缺省 vs 可为 null"节的序列化约定）。

### 迁移分步（每步独立交付、可测、可回退）

| 步 | 内容 | 交付标志 |
|----|------|---------|
| A | `frontend_contract/` 包 + 生成器 + Makefile/CI + **WS 消息**全量建模 | WS 生成类型替换手写；发送点 WS 消息改造；test-quick + vue-tsc 绿 |
| B | **业务 REST** 建模 + 前端 providers/图片类型切换（`ModelSelector.vue` / `SettingsPanel.vue` 直接 fetch `/api/providers`，无独立 store）+ api_routes 改造 | REST 生成类型替换手写；test-quick 绿 |
| C | **Debug API** 建模 + 前端 debug/types.ts 切换 + debug_routes 改造 | Debug 类型生成替换；debug 页面构建绿 |

## 错误处理

- **后端**：发送点构造契约模型 → 字段错误构造时抛 `ValidationError`（开发期暴露，替代手写 dict 运行时静默坏）。发送侧统一 try/except → `logger.warning` 兜底（对齐现有 `_send_json` 容错风格）。
- **生成器**：模型不合法（判别字段缺失/类型无法映射）→ 明确报错 + 非零退出码，CI 直接红。
- **漂移检测**：`make check-types` 生成到临时文件 diff，不匹配即红——强制"改契约必须跑生成器"。
- **生成文件**：`// DO NOT EDIT` 头注释，禁止手改。

## 测试

| 层 | 覆盖 |
|----|------|
| 契约模型测试（`frontend_contract/tests/`，marker=unit） | 判别联合完整性、Literal 枚举、**必填判定（非 Optional 带默认 → 必填）**、**可缺省 vs 可为 null 语义（ts_null marker 判别）**、**`wire_dict()` 递归序列化（exclude_none 缺省省略 + 嵌套 ts_null 补显式 null）** |
| 生成器快照测试（marker=unit） | 已知模型集 → 输出与快照字节一致（可重复性 + 防回归）；含 UnknownMessage 兜底追加 |
| 发送点回归 | 改造后现有 `test_server.py` / `test_provider_registry` / debug 相关测试全绿（行为等价验证 + wire 格式未变）；**专项断言 `switch_model_result` 成功无 error 键、`service_mode_result` 成功 `error: null`**（ts_null vs X? 逐消息定性的守护） |
| 前端 | `vue-tsc`（生成类型被真实编译检查）+ `npm run build` |
| 门禁 | `make test-quick` + `make lint`（已含 `frontend_contract/`）+ `make check-types` 全绿 |

## 范围外（YAGNI）

- 不做 JSON Schema 中间产物。**术语**：B1 = Pydantic → JSON Schema → `json-schema-to-typescript`（npm 工具链，需新增 devDependency + 中间产物 + 输出风格不可控，评估时已排除）；B2 = 仓库内置 Python 发射器（本设计采用，零依赖）。B1 仅在此提及对照。
- 不做 TS → Python 反向生成（单向后端权威）。
- 不做运行时 schema 校验插件（前端按 TS 静态类型足够；后端模型构造已覆盖）。

## 风险

1. **半迁移退化**：生成段与手写段混用会漂移。对策：re-export 边界清晰 + `check-types` 门禁；纯前端辅助类型明确标注非契约。
2. **发射器维护**：新增类型模式（如新 Literal 枚举、Union 源）需扩展发射器。对策：快照测试锁住现有行为，PoC 已证核心映射。
3. **ts_null marker 双向风险**：
   - **漏标**（字段本"可为 null"但标成 X?）：生成 `X?`（缺省省略），前端语义从 `null` 变 `undefined`。
   - **过度标记**（字段本"缺省省略"但标成 ts_null）：wire 上**新增显式 null 键**——如 switch_model_result 成功路径误标 error → 新增 `"error": null`，违背行为等价（.NET 第三方可见）。
   对策：逐发送点按实际 wire 行为定（键恒存在→ts_null / 键缺省省略→X?，见"可缺省 vs 可为 null"节）；契约模型测试断言每字段映射 + 发送点回归测试验证 wire 格式未变。
4. **动态 key 模型**：providers 响应的 provider 名是动态 key → `dict[str, ProviderEntry]`；TS `Record<string, ProviderEntry>`，前端仍需局部收窄（既有模式不变）。
5. **行为等价回归**：发送点改造可能改变序列化细节（字段顺序/默认值/None 处理）。对策：`exclude_none=True` 保持缺省省略 + 测试全绿 + 字段默认值严格对齐现有 dict；`.NET` 第三方可见的 wire 格式以 frontend-api.md 对照。
