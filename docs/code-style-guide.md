# 代码风格与类型约束指南

> 文档导航：[项目文档](../README.md)

收敛 2026-08 以来各批次（getattr 审计、MemoryEntry.memory_type 对齐、core ID NewType 化、channel/kind 对齐契约枚举）
沉淀的类型与风格纪律。**后续写代码前先读本文件**——它是类型权威归属、ID 流转、StrEnum/coerce 用法、getattr 使用的唯一参考。

---

## 1. 标识符 ID 体系（`typing.NewType`）

### 1.1 ID NewType 清单与权威归属

| ID | 定义处 | 权威归属 | 格式 |
|----|--------|---------|------|
| `MessageId` | `core/schema/types.py` | **core** | `msg_` + 26 hex |
| `ToolCallId` | `core/schema/types.py` | **core** | `call_` |
| `GenerationId` | `core/schema/types.py` | **core** | `gen_` + 26 hex |
| `SegmentId` | `core/schema/types.py` | **core** | `seg_` + 26 hex |
| `RequestId` | `core/schema/types.py` | **core** | `{req_counter:03d}`（三位计数器，非 UUID） |
| `AttachmentId` | `core/schema/types.py` | **core** | `att_` + 26 hex |
| `MemoryId` | `core/schema/types.py` | **core** | `mem_` + 26 hex（Milvus 旧 `msg_` 数据读回兼容） |
| `TaskId` | `core/schema/types.py` | **core** | `task_` + 26 hex |
| `SessionId` | `Module/workflow_contract/schema.py`（core 再导出） | **契约层** | `conv_` + 12 hex / `default` |
| `WorkflowId`/`AgentId`/`ApprovalId`/`NodeId` | `Module/workflow_contract/schema.py` | **契约层** | workflow 内部 |
| `ArtifactId` | `Module/workflow_engine/schema/types.py` | **契约层** | 产物 ID |

统一从 `core.schema` 导入（`from core.schema import MessageId, SessionId` 或 `from core.schema.types import ...`），
**禁相对导入**。生成函数（`new_message_id`/`new_generation_id`/`new_segment_id`/`new_attachment_id`/`new_memory_id`/`new_task_id`/`next_request_id`）返回类型标注对应 NewType。
`MemoryId`/`TaskId` 值空间与 `MessageId` **隔离**（独立前缀 `mem_`/`task_`，2026-08-09 定案）；Milvus 历史 `msg_` 记忆条目经 `MemoryId(...)` 收口读回兼容。

### 1.2 类型权威归属原则（核心）

> **谁的权威谁定义；另一侧对齐或裸传。** 由「权威定义处 + 依赖方向」共同决定，不要凭直觉选边。

| 场景 | 权威在 | 另一侧 | 规则 |
|------|--------|--------|------|
| ID（MessageId/ToolCallId/GenerationId/...） | **core**（schema 层） | frontend_contract | 契约层**保持裸 str**（frontend_contract 零依赖，不能引 core） |
| channel/kind（MessageChannel/SegmentKind） | **契约层**（frontend_contract） | core | core **复用契约枚举**（core 能引契约层），**不 core 自建**（自建会双份漂移，违背 `make check-types` 单源） |

**反例警示**：core 自建 `SegmentChannel` 枚举 = 双份漂移；frontend_contract 引 core 的 NewType = 循环依赖。两者都是错。

### 1.3 input 边界裸 str + 收口点

- **wire 输入保持裸 str**：`Agent.process_message` 签名、ws_handler/session_routes 从 `data.get(...)`/`match_info` 取值的变量——它们是不可信字符串入口，类型标注裸 `str` 是诚实的。
- **收口点在进入 core 内部处**：`MessageId(x) if x else None` / `SessionId(x) if x else None` 包一层，
  之后流转全程 NewType。典型收口点：`Agent.process_message` 开头、`loop.process` 构造 GenerationContext 处、`UserInputData` 构造点、registry/repo 调用处。
- **跨层边界同样裸 str**（core→Module.tool 协议层，2026-08-09 定案）：`ToolContext.session_id/trace_id/agent_id`、`ToolResult.async_task_id` 等 ID 字段保持裸 `str`——`Module.tool` 是后端无关库，不依赖 workflow_contract 的 NewType（同 frontend_contract 零依赖裸 str 模式）。core 侧传 NewType 值（NewType 是 str 子类，直传不拆包）；消费侧 `WorkflowId(...)` 包回即边界收口。**勿在此边界引入 workflow_contract NewType**——会闭合 `workflow_contract.schema → Module.tool.tool_descriptor` 的 import 环。
- **NewType 是 str 子类型**：可直接传给契约层裸 `str` 参数（wire 发射无需 `.value` 拆包）。

### 1.4 NewType 陷阱

- **`isinstance(x, NewType(...))` 在 Python 3.12 抛 `TypeError: isinstance() arg 2 must be a type`**（实测）——
  测试断言类型**必须用 `typing.get_type_hints()` 查注解**（单类型 `is`、联合类型 `==`），禁止 isinstance(x, NewType)。
- **pyright 门禁（2026-08 引入）**：`make pyright`（`conda run -n Elysia pyright`）检 **全仓生产代码**（core/ + Module/ + frontend_contract/，2026-08-09 扩展），
  basic 模式 + 排除测试，须 **Elysia env** 运行（base env 无 aiohttp 会漏报 ~25 处）。无 mypy。
  NewType 运行时即 str、pydantic 无校验差异、`make check-types` 不受影响。

---

## 2. StrEnum 用法

### 2.1 存储归一：coerce 双入口（`MemoryType` 模式）

对**不可信字符串入口**（Milvus 行解析 / API body / LLM 输出）的记忆类型等枚举，用容错 coerce 归一：

```python
@classmethod
def try_coerce(cls, value: object) -> "MemoryType | None":   # 过滤边界用
    """精确匹配 → .title() 大小写不敏感 → 未知/None/非 str 返回 None（不抛）。"""
    ...

@classmethod
def coerce(cls, value: object) -> "MemoryType":               # 存储/构造边界用
    """try_coerce 的 NOTE 兜底版——未知值归一为 Note，不崩。"""
    return cls.try_coerce(value) or cls.NOTE
```

- **存储/构造边界用 `coerce`**（未知 → NOTE 兜底不抛）；**过滤比较边界用 `try_coerce`**（未知 → None → 不命中，保留"无效过滤值返回空"语义）。
- dataclass 用 `__post_init__` 归一；越过 `__post_init__` 的直接赋值在边界显式 coerce。
- 两个入口都幂等；合法成员/`.value` 原样通过。

### 2.2 契约层枚举复用（`MessageChannel`/`SegmentKind` 模式）

- 契约层（frontend_contract）定义枚举，wire 契约字段直接用枚举（`channel: MessageChannel`）。
- core 侧字段/参数对齐契约枚举（`from frontend_contract.ws_message import MessageChannel, SegmentKind`），
  贯通到 wire 发射边界（manager 透传 ctx.channel → websocket handler 参数 → 契约构造）。
- **StrEnum 是 str 子类**：`==` 成员值成立（`MessageChannel.INNER_VOICE == "inner_voice"`）、
  `str()/json.dumps()/pydantic/wire_dict()` 均输出成员值——wire 字节不变，前端无感。
- 顶层 import 契约枚举需默认值真实对象时（`kind: SegmentKind = SegmentKind.TEXT`）用真实成员，非字符串注解。

---

## 3. getattr 使用纪律

> 来源：getattr 全项目审计修复（2026-08）。审计分类即纪律。

| 模式 | 纪律 |
|------|------|
| **守卫链不完整**（只守最后一跳） | 双跳守卫必须逐跳 `getattr(getattr(x, "a", None), "b", None)`，只守最后是 bug |
| **present-but-None**（属性存在但为 None） | `getattr` 默认值不防 None——用 `or 默认值` 兜底 |
| **私有属性 + 默认值**（`getattr(self._x, "y", dflt)`） | 私有属性静默改名会无声吞错——尽量走公开接口（`agent.session` 而非 `agent._session`） |
| **冗余 getattr**（Pydantic/dataclass 恒存在字段） | 直接属性访问（`prep_result.missing_tools or []`），不 getattr |
| **数据驱动 getattr**（lifecycle `getattr(self, method_name)` 无默认） | 失败即 loud，符合意图，保留 |
| 真 None 守卫 + 有意义降级值（`getattr(agent.llm, "model", "unknown")`） | 保留（有降级语义） |

跨模块消费禁止 `agent._xxx`，一律走公开接口；debug 深度内省用 `# noqa: SLF001` 显式豁免。

---

## 4. 通用约束（既有门禁，`make lint` 强制）

- **禁相对导入**：一律绝对导入（ruff TID252；`__init__.py` 重导出与 `__main__.py` 豁免）。
- **参数类型注解**：生产代码函数参数必须类型注解（ruff ANN001/002/003；测试文件豁免——pytest fixture 动态类型惯例）。
- **契约单源**：`frontend_contract` 是可执行契约唯一权威 → `gen_ts` → `contract.generated.ts`；改契约必须跑 `make check-types`。
- **注释/README 用中文**（中英混合——中文嵌英文技术术语可接受）。
- **架构规则**：frontend_contract / workflow_contract 是零依赖叶子；core → frontend_contract 单向依赖合法。

---

## 5. 关联文档

| 文档 | 内容 |
|------|------|
| [CLAUDE.md](../CLAUDE.md) | 项目地图 + 模块说明 + 基础规则 |
| [docs/architecture.md](architecture.md) | 完整架构详解 |
| [docs/archive/data-structure-survey.md](archive/data-structure-survey.md) | 全项目数据结构普查（枚举/dataclass 清单，已归档） |
| [frontend_contract/README.md](../frontend_contract/README.md) | 前后端契约单源机制 |
| [docs/superpowers/specs/2026-08-08-core-id-newtype-design.md](superpowers/specs/2026-08-08-core-id-newtype-design.md) | core ID NewType 化设计 |
| [docs/superpowers/specs/2026-08-08-channel-kind-strenum-design.md](superpowers/specs/2026-08-08-channel-kind-strenum-design.md) | channel/kind 对齐契约枚举设计 |
| [docs/superpowers/specs/2026-08-08-memory-entry-memory-type-design.md](superpowers/specs/2026-08-08-memory-entry-memory-type-design.md) | MemoryEntry.memory_type 对齐设计 |
