# TDD 测试文档 — Module/tool/

## 概述

Module/tool/ 采用测试驱动开发（TDD）方法构建。所有功能先写测试，再写实现，最后重构。本文档记录每个开发步骤的 TDD 循环、关键设计决策，以及测试如何驱动了架构演进。

## TDD 循环

每个 Step 遵循标准 TDD 三阶段：

1. **红（Red）** — 编写失败测试，明确期望行为
2. **绿（Green）** — 最小实现让测试通过
3. **重构（Refactor）** — 消除重复、改善设计，测试仍绿

## 开发步骤

### Step 1 — 数据模型（协议层）

**目标**: 定义跨 Provider 的中性工具表示，与具体 LLM 格式解耦。

#### 1.1 ToolParameter

**TDD 循环**:
```
RED   → test_basic_string_param: ToolParameter(type="string") → JSON Schema {"type": "string"}
GREEN → ToolParameter dataclass + to_json_schema_property() 最小实现
REFACTOR → 引入 __slots__ 优化内存

RED   → test_nested_object_param: object type + properties 递归
GREEN → to_json_schema_property() 递归处理 properties + items

RED   → test_param_no_default_when_not_set: default_set=False 不输出 default
GREEN → 引入 default_set 字段，区分"未设置"与"设置为 None"
```

**关键决策**: `default_set` 字段。Python 的 `default=None` 无法区分"没有默认值"和"默认值就是 None"。`default_set=True` 仅在显式设置时标记，解决了 Optional 参数的 schema 生成问题。

**测试覆盖**: 10 个用例 — string / integer / number / boolean / enum / default / nested object / array items / required children

#### 1.2 ToolDefinition

**TDD 循环**:
```
RED   → test_to_openai_no_params: 无参工具 → {"type": "function", "function": {...}}
GREEN → to_openai() 返回 OpenAI function-calling 格式

RED   → test_to_anthropic_format: Anthropic input_schema 格式
GREEN → to_anthropic() 包装为 {"name":..., "input_schema":...}

RED   → test_cross_provider_schema_equivalence: 三 provider properties 内容一致
GREEN → _build_json_schema() 提取为共享方法，三个 to_* 复用
```

**关键决策**: `_build_json_schema()` 是所有 provider 格式的共同核心。三个 `to_*()` 只是外层包装不同，内在的 JSON Schema 完全一致。

**测试覆盖**: 7 个用例 — OpenAI / Anthropic / Gemini 格式、跨 provider 等价性、required 覆盖

#### 1.3 ToolResult

**TDD 循环**:
```
RED   → test_fail: ToolResult.fail(name, error) → success=False, error 字段
GREEN → fail() classmethod + 基本字段

RED   → test_ok_truncated: truncated=True 标记
GREEN → ok() classmethod + truncated 参数
```

**测试覆盖**: 3 个用例 — ok / ok_truncated / fail

---

### Step 2 — 注册中心

**目标**: 工具的注册、查找、卸载、Schema 导出。

#### 2.1 注册/卸载

**TDD 循环**:
```
RED   → test_register_single: register → len=1, get 返回定义
GREEN → ToolRegistry with _tools dict, register()/get()/__len__

RED   → test_register_duplicate_overwrites: 同名注册 → 覆盖旧定义
GREEN → register() 直接赋值（同名覆盖是预期行为）

RED   → test_unregister_existing: unregister 存在 → True, len-1
GREEN → unregister() 检查 key 存在，del 后返回 True
```

**测试覆盖**: 7 个用例 — register / register_many / duplicate overwrite / unregister / get missing / repr

#### 2.2 Schema 导出

**TDD 循环**:
```
RED   → test_definitions_default_openai: 默认导出 OpenAI 格式
GREEN → definitions(format) 遍历 _tools，调用 to_*()

RED   → test_definitions_empty: 空 registry → []
GREEN → 空 dict 自然返回空列表（无需特殊处理）
```

#### 2.3 执行引擎

**TDD 循环**:
```
RED   → test_execute_success: execute("tool", args) → ToolResult.success=True
GREEN → execute() 调用 func(**coerced)，包装为 ToolResult

RED   → test_execute_unknown_tool: 未注册工具 → fail + error 信息
GREEN → execute() 开头检查 _tools.get(name)，None 则返回 fail

RED   → test_execute_history_recorded: 成功/失败都应写入 _history
GREEN → _record() 方法，execute() 每个出口都调用

RED   → test_execute_history_capped: history 最多 20 条
GREEN → _record() 末尾检查 len > _max_history，切片保留最后 20 条
```

**关键决策**: `_record()` 作为 execute() 的通用出口，记录所有执行（成功/失败），保证审计完整性。`CancelledError` 在 `_record()` 后 re-raise，保留取消语义。

**测试覆盖**: 14 个用例 — success / default / missing required / type error / unknown tool / no func / history / capped / timeout / exception isolation / execute_safe / truncated

---

### Step 3 — 安全机制

**目标**: 参数校验、类型强制、超时守卫、结果截断。

#### 3.1 validate_args — 参数校验与类型强制

**TDD 循环**:
```
RED   → test_coerce_integer_string: "42" → 42
GREEN → _coerce_value("integer", "42") → int("42")

RED   → test_coerce_boolean_true_values: 多种 true 表示 → True
GREEN → _coerce_value("boolean", v) 检查 v.lower() in {"true","1","yes"}

RED   → test_coerce_boolean_bad_value: "maybe" → error
GREEN → _coerce_value 对无法识别的 boolean 值返回错误

RED   → test_boolean_not_coerced_from_int: int 1 不当作 bool True
GREEN → 只有 str 类型的 true/false/1/0/yes/no 才触发 bool 强制，int 保持不变
```

**关键决策**: boolean 强制仅对 str 生效。int `1` 不自动转为 `True`，避免 LLM 传 `{"flag": 1}` 时发生意外行为。

**测试覆盖**: 12 个用例 — integer/number/boolean 强制、missing required、optional default、unknown passthrough、int-not-bool 边界

#### 3.2 truncate_result — 结果截断

```
RED   → test_truncation: 超长文本截断 + "…"
GREEN → 超过 max_len 时切片 + "…"

RED   → test_exact_boundary: 刚好等于 max_len 不截断
GREEN → len <= max_len 时直接返回原文
```

#### 3.3 execute_with_timeout — 超时守卫

```
RED   → test_timeout_raises: 超时任务 → TimeoutError
GREEN → asyncio.wait_for + timeout 参数
```

---

### Step 4 — @tool 装饰器

**目标**: 声明式工具定义，自动从 type hints 推导参数 schema。

**TDD 循环**:
```
RED   → test_basic_decorator: @tool(description=...) → _tool_definition 存在
GREEN → _decorate_tool() + tool() 工厂函数

RED   → test_type_hints_to_params: str + int → 正确 ToolParameter
GREEN → type_hints_to_params() 使用 typing.get_type_hints()

RED   → test_literal_to_enum: Literal["a","b"] → enum=["a","b"]
GREEN → typing.get_args() 提取 Literal 值

RED   → test_optional_param: Optional[str] → required=False
GREEN → 检测 Union[None, X] → required=False

RED   → test_bare_tool: @tool 不带括号也能工作
GREEN → tool() 检测 func_or_name 是 callable → 走 bare 路径

RED   → test_manual_parameters_override: parameters= 手动覆盖
GREEN → 装饰器接受 parameters dict，优先于 type hints 推导
```

**关键决策**: 
- 装饰器双模式（bare `@tool` 和 `@tool(...)`）通过检查第一个参数是否为 callable 实现
- `_hidden_params` 机制在此引入：自动检测 `ctx: ToolContext` 和 `emit: ToolEmitter` 参数，标记为对 LLM 隐藏
- docstring `:param name: desc` 自动提取为参数 description

**测试覆盖**: 11 个用例 — basic / name / custom name / type hints / Literal / Optional / bool+float / docstring / manual override / callable / wraps

---

### Step 5 — Pydantic 集成

**目标**: Pydantic BaseModel → ToolParameter 转换。

**TDD 循环**:
```
RED   → test_simple_model: Pydantic model → ToolParameter
GREEN → pydantic_to_params() 调用 model.model_json_schema()

RED   → test_nested_object: 嵌套 model → object type + properties
GREEN → _json_schema_to_param() 递归解析 $ref/$defs

RED   → test_enum_field: str Enum field → enum 列表
GREEN → anyOf 解析 → 提取 enum 值
```

**关键决策**: Pydantic 的 `model_json_schema()` 对嵌套模型产生 `$defs` + `$ref`，LLM API 不识别。`_json_schema_to_param()` 在转换时将 `$ref` 展开为内联定义，确保导出 schema 是自包含的扁平结构。

**测试覆盖**: 5 个用例 — simple / descriptions / not-pydantic / nested / enum

---

### Step 6-7 — 工具属性扩展

**目标**: `ToolDefinition` 增加 `requires` / `result_type` / `side_effects` 声明。

**TDD 循环**:
```
RED   → test_requires_parameter: @tool(requires=["web_search"])
GREEN → ToolDefinition 加 requires 字段，装饰器透传

RED   → test_result_type_parameter: @tool(result_type="image")
GREEN → result_type 字段，声明工具产出类型

RED   → test_side_effects_parameter: @tool(side_effects=True)
GREEN → side_effects 字段，标记外部副作用

RED   → test_llm_content_image_type: image 类型 → llm_summary 注入 LLM
GREEN → llm_content property 区分 text/image/audio/file 多态处理
```

**测试覆盖**: MediaItem 3 + ToolResult 扩展 7 + ToolDefinition 扩展 2 + @tool 扩展 5

---

### Step 8 — ToolResult passthrough

**目标**: 工具可返回 `ToolResult` 或 `str`，registry 智能处理。

**TDD 循环**:
```
RED   → test_tool_returns_toolresult_directly: ToolResult → 直接使用
GREEN → execute() 中 isinstance(raw, ToolResult) 检查

RED   → test_str_tool_still_works: str → 自动包装
GREEN → else 分支调用 truncate_result + ToolResult.ok()

RED   → test_toolresult_truncation_not_applied: ToolResult 不二次截断
GREEN → ToolResult 分支跳过 truncate_result
```

**测试覆盖**: 4 个用例

---

### Step 9 — ErrorType 错误分类

**目标**: 区分 6 种错误类型，帮助认知循环决策重试策略。

**TDD 循环**:
```
RED   → test_is_retryable: INVALID_ARGUMENT + TIMEOUT → True
GREEN → ErrorType enum + is_retryable property

RED   → test_execute_unknown_tool_has_error_type: UNKNOWN_TOOL → not retryable
GREEN → execute() 各 fail() 调用加 error_type 参数

RED   → test_llm_content_invalid_argument: 错误信息含 "review...and retry"
GREEN → _format_error_for_llm() 按 error_type 生成不同提示
```

**关键决策**: `is_retryable` property 而非方法，因为判断逻辑固定且无副作用。只有 `INVALID_ARGUMENT` 和 `TIMEOUT` 可重试。

**测试覆盖**: ErrorType 4 + ToolResult error_type 3 + Registry error_types 4 + LLM friendly errors 5

---

### Step 10 — ToolContext 上下文注入

**目标**: 运行时依赖注入，对 LLM 隐藏 ctx 参数。

**TDD 循环**:
```
RED   → test_ctx_param_hidden_from_schema: ctx 不出现在 JSON Schema
GREEN → @tool 自动检测 ToolContext 类型 → _hidden_params

RED   → test_ctx_required_detection: ctx 无默认值 → _ctx_required=True
GREEN → 检查参数默认值是否为 inspect.Parameter.empty

RED   → test_ctx_fail_fast: 必需 ctx 但未提供 → TOOL_ERROR
GREEN → execute() 检查 _ctx_required and ctx is None

RED   → test_registry_execute_with_ctx: ctx 正确注入工具函数
GREEN → execute() 将 ctx 注入 coerced["ctx"]
```

**测试覆盖**: ToolContext 4 + Hidden params 2 + Ctx decorator 4 + Ctx injection 3 + Fail fast 4

---

### Step 11 — execute_stream + ToolEmitter

**目标**: 流式执行，ToolEmitter 进度上报，keep-alive 超时检测。

**TDD 循环**:
```
RED   → test_tool_emitter_progress: emit.progress() → 进度事件入队
GREEN → ToolEmitter + asyncio.Queue + execute_stream() 轮询

RED   → test_emitter_detection: has_emitter 自动检测
GREEN → typing.get_type_hints() 检查 ToolEmitter 参数

RED   → test_emitter_is_cancelled: is_cancelled() 检测 task.cancel()
GREEN → emitter._task.cancelled() 代理

RED   → test_progress_timeout_keepalive: 超时无进度 → 取消
GREEN → execute_stream() keep-alive 时间检查 → task.cancel()

RED   → test_cancellable_yields_cancelled_then_raises: CancelledError → yield + re-raise
GREEN → CancelledError 处理: yield CANCELLED ToolResult → raise
```

**关键决策**: 
- 模式 B（ToolEmitter + 正常 return）优于模式 A（async generator + yield），因为模式 B 支持超时守卫和更清晰的返回类型
- keep-alive 超时通过 `progress_timeout` 配置，execute_stream() 轮询队列时检测
- `CancelledError` yield 后 re-raise，匹配 execute() 的语义

**测试覆盖**: execute_stream 6 + ToolEmitter 3 + execute_stream emitter 5

---

### Step 12 — Schema 缓存

**目标**: definitions() 结果缓存，register/unregister 时失效。

**TDD 循环**:
```
RED   → test_definitions_cached: 两次调用返回不同引用但相同内容
GREEN → _cache dict + copy.deepcopy()

RED   → test_cache_invalidated_on_register: register → cache cleared
GREEN → register() 开头 self._cache.clear()

RED   → test_cache_thread_safe: 并发调用不崩溃
GREEN → threading.Lock 保护 _cache 读写
```

**测试覆盖**: 4 个用例 — cache / invalidate / thread safety / deep copy

---

## TDD 原则总结

### 驱动设计的测试

以下设计决策直接来自测试驱动：

| 测试用例 | 驱动的设计决策 |
|---------|-------------|
| `test_param_no_default_when_not_set` | `default_set` 字段区分"未设置" / "设置为 None" |
| `test_boolean_not_coerced_from_int` | boolean 强制仅对 str 生效 |
| `test_cross_provider_schema_equivalence` | `_build_json_schema()` 共享核心 |
| `test_tool_returns_toolresult_directly` | `isinstance(raw, ToolResult)` 分支 |
| `test_ctx_param_hidden_from_schema` | `_hidden_params` 机制 |
| `test_progress_timeout_keepalive` | `progress_timeout` + keep-alive 检测 |
| `test_cancellable_yields_cancelled_then_raises` | CancelledError yield + re-raise 语义 |

### 边界条件优先

每个 Step 的测试都覆盖了核心路径和边界条件：
- 空参数 / 无参数
- 同名覆盖
- 不存在的工具
- 超时 / 取消
- 类型错误
- 超长结果截断
- 必需参数缺失

### 向后兼容保证

所有扩展（ErrorType / ToolContext / ToolEmitter / ToolResult passthrough）都通过测试确保旧代码不受影响：
- `test_defaults_backward_compat` 系列测试确保新字段有合理默认值
- `test_str_tool_still_works` 确保返回 str 的工具继续工作
- 不传新参数的调用路径全部覆盖

## 测试金字塔

```
         /\
        /集\    3 集成测试（端到端流程）
       /成测\
      /------\
     /        \    ~30 注册中心测试（execute / stream / ctx / error）
    /  Registry \
   /------------\
  /              \  ~50 单元测试（ToolParameter / ToolDefinition / ToolResult / safety）
 /     Models      \
/------------------\  ~80 装饰器 + schema 测试
```
