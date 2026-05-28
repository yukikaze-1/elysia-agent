# test_plan — Module/tool/

> 配套文档: [TDD.md](TDD.md) — TDD 开发过程 | [unit_test.md](unit_test.md) — 单元测试编写参考

## 运行

```bash
conda run -n Elysia python -m pytest Module/tool/test_tool.py -v --asyncio-mode=auto
```

---

## Step 1 — 数据模型

### TestToolParameter (10)

- [x] `test_basic_string_param` — type=string → JSON Schema
- [x] `test_integer_param` — type=integer
- [x] `test_number_param` — type=number
- [x] `test_boolean_param` — type=boolean + default
- [x] `test_param_with_enum` — enum 列表输出
- [x] `test_param_with_default` — default 值输出
- [x] `test_param_no_default_when_not_set` — default_set=False 时不输出 default
- [x] `test_nested_object_param` — object type + properties 嵌套
- [x] `test_object_param_with_required_children` — object 级 required 列表
- [x] `test_array_param_with_items` — array type + items 类型

### TestToolDefinition (7)

- [x] `test_basic_definition` — 默认值（timeout=30.0, max_result_len=2000, func=None）
- [x] `test_to_openai_no_params` — 无参数 → OpenAI 格式
- [x] `test_to_openai_with_params` — 有参数 → OpenAI 格式含 properties/required
- [x] `test_to_anthropic_format` — Anthropic input_schema 格式
- [x] `test_to_gemini_format` — Gemini parameters 格式
- [x] `test_cross_provider_schema_equivalence` — 三 provider properties 内容一致
- [x] `test_explicit_required_overrides_param_required` — self.required 非空时覆盖参数级 derived required

### TestToolResult (3)

- [x] `test_ok` — ToolResult.ok() 各字段
- [x] `test_ok_truncated` — truncated=True 标记
- [x] `test_fail` — ToolResult.fail() 各字段

---

## Step 2 — 注册中心

### TestToolRegistryRegister (7)

- [x] `test_register_single` — 单个注册 + len/contains/get
- [x] `test_register_many` — 批量注册
- [x] `test_register_duplicate_overwrites` — 同名覆盖
- [x] `test_unregister_existing` — 卸载存在 → True
- [x] `test_unregister_missing` — 卸载不存在 → False
- [x] `test_get_missing` — get 不存在 → None
- [x] `test_repr` — repr 含工具名 / "empty"

### TestToolRegistryDefinitions (4)

- [x] `test_definitions_default_openai` — 默认 OpenAI 格式
- [x] `test_definitions_anthropic` — Anthropic 格式
- [x] `test_definitions_gemini` — Gemini 格式
- [x] `test_definitions_empty` — 空 registry → 空列表

### TestToolRegistryExecute (14)

- [x] `test_execute_success` — 正常执行返回成功结果
- [x] `test_execute_with_default` — 使用参数默认值
- [x] `test_execute_missing_required` — 缺少必填参数 → fail
- [x] `test_execute_type_error` — 类型错误 → fail
- [x] `test_execute_unknown_tool` — 执行未注册工具 → fail
- [x] `test_execute_no_func` — 已注册但未绑定函数 → fail
- [x] `test_execute_history_recorded` — 成功记录写入 _history
- [x] `test_execute_history_errors_recorded` — 失败记录写入 _history
- [x] `test_execute_history_capped` — history 最多 20 条
- [x] `test_execute_timeout` — 超时 → fail + "超时"
- [x] `test_execute_exception_isolation` — 工具抛异常 → fail，不传播
- [x] `test_execute_safe_catches_cancelled` — execute_safe 捕获 CancelledError
- [x] `test_execute_truncated_result` — 超长结果截断 + truncated=True

---

## Step 3 — 安全机制

### TestValidateArgs (11)

- [x] `test_coerce_integer_string` — "42" → 42
- [x] `test_coerce_integer_already_int` — 42 → 42
- [x] `test_coerce_integer_bad_value` — "abc" → error
- [x] `test_coerce_number` — "3.14" → 3.14
- [x] `test_coerce_boolean_true_values` — "true"/"1"/"yes" → True
- [x] `test_coerce_boolean_false_values` — "false"/"0"/"no" → False
- [x] `test_coerce_boolean_already_bool` — True → True
- [x] `test_coerce_boolean_bad_value` — "maybe" → error
- [x] `test_missing_required` — 必填参数缺失 → error
- [x] `test_optional_gets_default` — 可选参数取默认值
- [x] `test_unknown_args_passthrough` — 未定义参数透传
- [x] `test_boolean_not_coerced_from_int` — int 1 不当作 bool→True

### TestTruncateResult (3)

- [x] `test_no_truncation_needed` — 短文本不截断
- [x] `test_truncation` — 长文本截断 + "…"
- [x] `test_exact_boundary` — 刚好等于 max_len 不截断

### TestExecuteWithTimeout (2)

- [x] `test_returns_result` — 正常返回
- [x] `test_timeout_raises` — 超时抛 TimeoutError

---

## Step 4 — 装饰器

### TestToolDecorator (11)

- [x] `test_basic_decorator` — @tool(description=...) 生成 ToolDefinition
- [x] `test_name_from_docstring` — 裸 @tool，name 取函数名，desc 取 docstring
- [x] `test_custom_name` — @tool(name=...) 覆盖
- [x] `test_type_hints_to_params` — str+int → 正确 ToolParameter
- [x] `test_literal_to_enum` — Literal["fact","plan"] → enum
- [x] `test_optional_param` — Optional[str] → required=False
- [x] `test_bool_float_params` — bool + float → 正确类型
- [x] `test_docstring_param_descriptions` — :param name: desc → description
- [x] `test_manual_parameters_override` — parameters= 手动覆盖
- [x] `test_decorated_function_still_callable` — 装饰后函数仍可调用
- [x] `test_wraps_preserves_metadata` — __name__/__doc__ 保留

---

## Step 5 — type_hints_to_params / pydantic_to_params

### TestTypeHintsToParams (6)

- [x] `test_empty_func` — 无参函数
- [x] `test_str_and_int` — 基本类型推导 + required
- [x] `test_with_defaults` — 有默认值 → required=False
- [x] `test_list_param` — list[str] → array<string>
- [x] `test_no_type_hint_fallback` — 无 type hint → string
- [x] `test_skips_self` — 跳过 self 参数

### TestPydanticToParams (6)

- [x] `test_simple_model` — 基本 Pydantic model → ToolParameter
- [x] `test_with_descriptions` — Field(description=...) → description
- [x] `test_not_a_pydantic_model` — 非 Pydantic → TypeError
- [x] `test_nested_object` — 嵌套 model → object type + properties
- [x] `test_enum_field` — str Enum → enum 列表
- [x] `test_pydantic_field_constraints` — Field(ge/le/pattern) 约束导出到 JSON Schema
- [x] `test_no_ref_in_schema` — 嵌套 model 的 $ref 展开为内联 schema

---

## Step 6 — MediaItem + ToolResult 扩展

### TestMediaItem (3)

- [x] `test_url_item` — URL-based MediaItem
- [x] `test_data_item` — data-based MediaItem (bytes)
- [x] `test_defaults` — 默认空值

### TestToolResultExtended (7)

- [x] `test_defaults_backward_compat` — 旧代码不传新字段，result_type="text"
- [x] `test_llm_content_text_type` — result_type=text → llm_content=content
- [x] `test_llm_content_image_type` — result_type=image → llm_content=llm_summary
- [x] `test_llm_content_fallback_to_content` — llm_summary 空 → fallback 到 content
- [x] `test_with_images` — ToolResult 带 images 列表
- [x] `test_with_audio` — ToolResult 带 audio MediaItem
- [x] `test_with_files` — ToolResult 带 files 列表

---

## Step 7 — ToolDefinition 扩展

### TestToolDefinitionExtended (2)

- [x] `test_defaults_backward_compat` — 默认 result_type="text", requires=[], side_effects=False
- [x] `test_explicit_fields` — 显式设置新字段

---

## Step 8 — @tool 装饰器扩展

### TestToolDecoratorExtended (5)

- [x] `test_requires_parameter` — @tool(requires=[...])
- [x] `test_side_effects_parameter` — @tool(side_effects=True)
- [x] `test_result_type_parameter` — @tool(result_type="image")
- [x] `test_combined_new_params` — 新参数组合使用
- [x] `test_bare_tool_defaults` — 裸 @tool，新字段取默认值

---

## Step 9 — ToolRegistry execute() ToolResult passthrough

### TestRegistryToolResultPassthrough (4)

- [x] `test_tool_returns_toolresult_directly` — 返回 ToolResult → 直接使用，不二次包装
- [x] `test_str_tool_still_works` — 返回 str → 自动包装（向后兼容）
- [x] `test_toolresult_history_includes_result_type` — history 记录 result_type
- [x] `test_toolresult_truncation_not_applied` — ToolResult 不由 registry 二次截断

---

## Step 5 — 集成

### TestIntegration (3)

- [x] `test_end_to_end` — @tool → register → execute → 验证结果
- [x] `test_definitions_cross_provider` — 三个 provider 格式属性一致
- [x] `test_multiple_tools_register_and_execute` — 多工具注册 + 分别执行

---

## 统计

| 测试类 | 用例数 |
|--------|--------|
| TestToolParameter | 10 |
| TestToolDefinition | 7 |
| TestToolResult | 3 |
| TestToolRegistryRegister | 7 |
| TestToolRegistryDefinitions | 4 |
| TestToolRegistryExecute | 14 |
| TestValidateArgs | 12 |
| TestTruncateResult | 3 |
| TestExecuteWithTimeout | 2 |
| TestToolDecorator | 11 |
| TestTypeHintsToParams | 6 |
| TestPydanticToParams | 6 |
| TestMediaItem | 3 |
| TestToolResultExtended | 7 |
| TestToolDefinitionExtended | 2 |
| TestToolDecoratorExtended | 5 |
| TestRegistryToolResultPassthrough | 4 |
| TestErrorType | 4 |
| TestToolContext | 4 |
| TestToolProgress | 4 |
| TestToolResultErrorType | 3 |
| TestHiddenParamsSchema | 2 |
| TestCtxDecorator | 4 |
| TestRegistryCtxInjection | 4 |
| TestRegistryErrorTypes | 4 |
| TestSchemaCache | 4 |
| TestCtxFailFast | 4 |
| TestLLMFriendlyErrors | 5 |
| TestExecuteStream | 6 |
| TestToolEmitter | 3 |
| TestExecuteStreamEmitter | 6 |
| TestIntegration | 3 |
| **总计** | **164** |
