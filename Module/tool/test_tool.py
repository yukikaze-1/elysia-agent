"""Tool use 模块单元测试。

覆盖: ToolParameter, ToolDefinition, ToolResult, ToolRegistry, safety, @tool decorator

运行: conda run -n Elysia python -m pytest Module/tool/test_tool.py -v --asyncio-mode=auto
"""

import asyncio
from typing import Literal

import pytest

from Module.tool import (
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolResult,
    get_tool_definition,
    pydantic_to_params,
    tool,
    type_hints_to_params,
)
from Module.tool.safety import execute_with_timeout, truncate_result, validate_args


# ===================================================================
# Step 1 — 数据模型
# ===================================================================

class TestToolParameter:
    """ToolParameter 单元测试。"""

    def test_basic_string_param(self):
        p = ToolParameter(type="string", description="search query")
        schema = p.to_json_schema_property()
        assert schema == {"type": "string", "description": "search query"}

    def test_integer_param(self):
        p = ToolParameter(type="integer", description="max results")
        schema = p.to_json_schema_property()
        assert schema["type"] == "integer"

    def test_number_param(self):
        p = ToolParameter(type="number")
        schema = p.to_json_schema_property()
        assert schema["type"] == "number"

    def test_boolean_param(self):
        p = ToolParameter(type="boolean", default=True, default_set=True)
        schema = p.to_json_schema_property()
        assert schema["type"] == "boolean"
        assert schema["default"] is True

    def test_param_with_enum(self):
        p = ToolParameter(type="string", enum=["fact", "plan", "note"])
        schema = p.to_json_schema_property()
        assert schema["enum"] == ["fact", "plan", "note"]

    def test_param_with_default(self):
        p = ToolParameter(type="integer", default=5, default_set=True)
        schema = p.to_json_schema_property()
        assert schema["default"] == 5

    def test_param_no_default_when_not_set(self):
        """default_set=False 时不输出 default（即使 default 有值）。"""
        p = ToolParameter(type="string", default=None, default_set=False)
        schema = p.to_json_schema_property()
        assert "default" not in schema

    def test_nested_object_param(self):
        child = ToolParameter(type="string", description="street name")
        p = ToolParameter(
            type="object",
            properties={"street": child},
        )
        schema = p.to_json_schema_property()
        assert schema["type"] == "object"
        assert "street" in schema["properties"]
        assert schema["properties"]["street"]["type"] == "string"

    def test_object_param_with_required_children(self):
        child = ToolParameter(type="string", required=True)
        p = ToolParameter(type="object", properties={"name": child})
        schema = p.to_json_schema_property()
        assert "required" in schema
        assert "name" in schema["required"]

    def test_array_param_with_items(self):
        items = ToolParameter(type="string")
        p = ToolParameter(type="array", items=items)
        schema = p.to_json_schema_property()
        assert schema["type"] == "array"
        assert schema["items"]["type"] == "string"


class TestToolDefinition:
    """ToolDefinition 单元测试。"""

    def test_basic_definition(self):
        d = ToolDefinition(
            name="get_time",
            description="获取当前时间",
        )
        assert d.name == "get_time"
        assert d.timeout == 30.0
        assert d.max_result_len == 4000
        assert d.func is None
        assert d.parameters == {}
        assert d.required is None

    def test_to_openai_no_params(self):
        d = ToolDefinition(name="ping", description="simple ping")
        result = d.to_openai()
        assert result["type"] == "function"
        assert result["function"]["name"] == "ping"
        assert result["function"]["description"] == "simple ping"
        assert result["function"]["parameters"]["type"] == "object"

    def test_to_openai_with_params(self):
        p = ToolParameter(type="string", description="query", required=True)
        d = ToolDefinition(
            name="search",
            description="search memory",
            parameters={"query": p},
        )
        result = d.to_openai()
        params = result["function"]["parameters"]
        assert "query" in params["properties"]
        assert "query" in params["required"]

    def test_to_anthropic_format(self):
        d = ToolDefinition(name="ping", description="ping test")
        result = d.to_anthropic()
        assert result["name"] == "ping"
        assert "input_schema" in result
        assert result["input_schema"]["type"] == "object"

    def test_to_gemini_format(self):
        d = ToolDefinition(name="ping", description="ping test")
        result = d.to_gemini()
        assert result["name"] == "ping"
        assert "parameters" in result
        assert result["parameters"]["type"] == "object"

    def test_cross_provider_schema_equivalence(self):
        """三个 provider 的 JSON Schema properties 应该一致。"""
        p = ToolParameter(type="string", description="query", required=True)
        d = ToolDefinition(name="search", description="search", parameters={"query": p})

        oai = d.to_openai()["function"]["parameters"]
        ant = d.to_anthropic()["input_schema"]
        gem = d.to_gemini()["parameters"]

        assert oai["properties"] == ant["properties"] == gem["properties"]

    def test_explicit_required_overrides_param_required(self):
        """self.required 如果显式设置，覆盖参数级别的 required 推导。"""
        p1 = ToolParameter(type="string", required=True)
        p2 = ToolParameter(type="integer", required=True)
        d = ToolDefinition(
            name="test",
            description="test",
            parameters={"a": p1, "b": p2},
            required=["a"],  # 只保留 a 为必填，b 变为可选
        )
        schema = d._build_json_schema()
        assert schema["required"] == ["a"]

    def test_required_empty_list_all_optional(self):
        """required=[] → 所有参数都不必填。"""
        p = ToolParameter(type="string", required=True)
        d = ToolDefinition(
            name="test",
            description="test",
            parameters={"x": p},
            required=[],  # 显式空列表 → 全部可选
        )
        schema = d._build_json_schema()
        assert "required" not in schema


class TestToolResult:
    """ToolResult 单元测试。"""

    def test_ok(self):
        r = ToolResult.ok("my_tool", "result text", duration_ms=5.0)
        assert r.success is True
        assert r.content == "result text"
        assert r.tool_name == "my_tool"
        assert r.duration_ms == 5.0
        assert r.truncated is False
        assert r.error is None

    def test_ok_truncated(self):
        r = ToolResult.ok("t", "x" * 100, duration_ms=1.0, truncated=True)
        assert r.truncated is True

    def test_fail(self):
        r = ToolResult.fail("my_tool", "something went wrong", duration_ms=2.0)
        assert r.success is False
        assert r.error == "something went wrong"
        assert r.tool_name == "my_tool"
        assert r.content == ""


# ===================================================================
# Step 2 — 注册中心
# ===================================================================

class TestToolRegistryRegister:
    """ToolRegistry 注册/卸载测试。"""

    def test_register_single(self):
        reg = ToolRegistry()
        d = ToolDefinition(name="test", description="a test tool")
        reg.register(d)
        assert len(reg) == 1
        assert "test" in reg
        assert reg.get("test") is d

    def test_register_many(self):
        reg = ToolRegistry()
        d1 = ToolDefinition(name="a", description="a")
        d2 = ToolDefinition(name="b", description="b")
        reg.register_many([d1, d2])
        assert len(reg) == 2
        assert reg.tool_names == ["a", "b"]

    def test_register_duplicate_overwrites(self):
        reg = ToolRegistry()
        d1 = ToolDefinition(name="test", description="old")
        d2 = ToolDefinition(name="test", description="new")
        reg.register(d1)
        reg.register(d2)
        assert reg.get("test").description == "new"

    def test_unregister_existing(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(name="test", description=""))
        assert reg.unregister("test") is True
        assert len(reg) == 0
        assert "test" not in reg

    def test_unregister_missing(self):
        reg = ToolRegistry()
        assert reg.unregister("nope") is False

    def test_get_missing(self):
        reg = ToolRegistry()
        assert reg.get("nope") is None

    def test_repr(self):
        reg = ToolRegistry()
        assert "empty" in repr(reg)
        reg.register(ToolDefinition(name="foo", description=""))
        assert "foo" in repr(reg)


class TestToolRegistryDefinitions:
    """ToolRegistry definitions() 测试。"""

    @pytest.fixture
    def registry(self):
        reg = ToolRegistry()
        p = ToolParameter(type="string", description="query", required=True)
        reg.register(ToolDefinition(
            name="search", description="search memory",
            parameters={"query": p},
        ))
        return reg

    def test_definitions_default_openai(self, registry):
        defs = registry.definitions()
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        assert defs[0]["function"]["name"] == "search"

    def test_definitions_anthropic(self, registry):
        defs = registry.definitions("anthropic")
        assert len(defs) == 1
        assert defs[0]["name"] == "search"
        assert "input_schema" in defs[0]

    def test_definitions_gemini(self, registry):
        defs = registry.definitions("gemini")
        assert len(defs) == 1
        assert defs[0]["name"] == "search"
        assert "parameters" in defs[0]

    def test_definitions_empty(self):
        reg = ToolRegistry()
        assert reg.definitions() == []
        assert reg.definitions("anthropic") == []


class TestToolRegistryExecute:
    """ToolRegistry execute() 测试。"""

    @pytest.fixture
    def registry(self):
        reg = ToolRegistry()

        async def echo_func(text: str, count: int = 1) -> str:
            return (text + " ") * count

        p_text = ToolParameter(type="string", description="text to echo", required=True)
        p_count = ToolParameter(type="integer", description="repeat count", default=1, default_set=True)
        reg.register(ToolDefinition(
            name="echo", description="echo text",
            parameters={"text": p_text, "count": p_count},
            func=echo_func,
        ))
        return reg

    @pytest.mark.asyncio
    async def test_execute_success(self, registry):
        result = await registry.execute("echo", {"text": "hello"})
        assert result.success
        assert result.content.strip() == "hello"
        assert result.tool_name == "echo"
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_execute_with_default(self, registry):
        result = await registry.execute("echo", {"text": "hi", "count": 2})
        assert "hi hi" in result.content

    @pytest.mark.asyncio
    async def test_execute_missing_required(self, registry):
        result = await registry.execute("echo", {})
        assert not result.success
        assert "必填" in result.error
        assert result.tool_name == "echo"

    @pytest.mark.asyncio
    async def test_execute_type_error(self, registry):
        result = await registry.execute("echo", {"text": "hi", "count": "not_a_number"})
        assert not result.success
        assert "整数" in result.error

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, registry):
        result = await registry.execute("nonexistent", {})
        assert not result.success
        assert "未注册" in result.error

    @pytest.mark.asyncio
    async def test_execute_no_func(self, registry):
        d = ToolDefinition(name="bare", description="no func attached")
        registry.register(d)
        result = await registry.execute("bare", {})
        assert not result.success
        assert "未绑定函数" in result.error

    @pytest.mark.asyncio
    async def test_execute_history_recorded(self, registry):
        await registry.execute("echo", {"text": "hi"})
        hist = registry.history
        assert len(hist) == 1
        assert hist[0]["name"] == "echo"
        assert hist[0]["success"] is True
        assert "duration_ms" in hist[0]

    @pytest.mark.asyncio
    async def test_execute_history_errors_recorded(self, registry):
        await registry.execute("echo", {})
        hist = registry.history
        assert hist[0]["success"] is False
        assert "error" in hist[0]

    @pytest.mark.asyncio
    async def test_execute_history_capped(self, registry):
        for i in range(30):
            await registry.execute("echo", {"text": str(i)})
        assert len(registry.history) == 20

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        async def slow() -> str:
            await asyncio.sleep(0.5)
            return "late"

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="slow", description="slow tool",
            func=slow, timeout=0.05,
        ))
        result = await reg.execute("slow", {})
        assert not result.success
        assert "超时" in result.error

    @pytest.mark.asyncio
    async def test_execute_exception_isolation(self):
        async def bad() -> str:
            raise ValueError("boom")

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="bad", description="bad tool", func=bad,
        ))
        result = await reg.execute("bad", {})
        assert not result.success
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_execute_safe_catches_cancelled(self):
        """execute_safe 应该捕获 CancelledError 并转为 ToolResult。"""
        async def slow() -> str:
            await asyncio.sleep(10)
            return ""

        reg = ToolRegistry()
        reg.register(ToolDefinition(name="slow", description="", func=slow, timeout=10))

        task = asyncio.create_task(reg.execute_safe("slow", {}))
        await asyncio.sleep(0.05)
        task.cancel()
        result = await task
        assert not result.success
        assert "取消" in result.error

    @pytest.mark.asyncio
    async def test_execute_truncated_result(self):
        async def long_output() -> str:
            return "x" * 5000

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="long", description="long output",
            func=long_output, max_result_len=50,
        ))
        result = await reg.execute("long", {})
        assert result.success
        assert result.truncated
        assert len(result.content) <= 53  # 50 chars + "…"


# ===================================================================
# Step 3 — 安全机制
# ===================================================================

class TestValidateArgs:
    """validate_args() 测试。"""

    def test_coerce_integer_string(self):
        p = ToolParameter(type="integer", required=True)
        coerced, errors = validate_args({"n": p}, {"n": "42"})
        assert errors == []
        assert coerced["n"] == 42

    def test_coerce_integer_already_int(self):
        p = ToolParameter(type="integer")
        coerced, _ = validate_args({"n": p}, {"n": 42})
        assert coerced["n"] == 42

    def test_coerce_integer_bad_value(self):
        p = ToolParameter(type="integer")
        coerced, errors = validate_args({"n": p}, {"n": "abc"})
        assert errors
        assert "整数" in errors[0]

    def test_coerce_number(self):
        p = ToolParameter(type="number")
        coerced, _ = validate_args({"x": p}, {"x": "3.14"})
        assert coerced["x"] == 3.14

    def test_coerce_boolean_true_values(self):
        p = ToolParameter(type="boolean")
        for v in ("true", "1", "yes"):
            coerced, _ = validate_args({"b": p}, {"b": v})
            assert coerced["b"] is True

    def test_coerce_boolean_false_values(self):
        p = ToolParameter(type="boolean")
        for v in ("false", "0", "no"):
            coerced, _ = validate_args({"b": p}, {"b": v})
            assert coerced["b"] is False

    def test_coerce_boolean_already_bool(self):
        p = ToolParameter(type="boolean")
        coerced, _ = validate_args({"b": p}, {"b": True})
        assert coerced["b"] is True

    def test_coerce_boolean_bad_value(self):
        p = ToolParameter(type="boolean")
        _, errors = validate_args({"b": p}, {"b": "maybe"})
        assert errors

    def test_missing_required(self):
        p = ToolParameter(type="string", required=True)
        _, errors = validate_args({"name": p}, {})
        assert errors
        assert "必填" in errors[0]

    def test_optional_gets_default(self):
        p = ToolParameter(type="string", default="hello", default_set=True)
        coerced, errors = validate_args({"name": p}, {})
        assert errors == []
        assert coerced["name"] == "hello"

    def test_unknown_args_passthrough(self):
        """未定义的参数直接透传。"""
        coerced, errors = validate_args({}, {"extra": "value"})
        assert errors == []
        assert coerced["extra"] == "value"

    def test_boolean_not_coerced_from_int(self):
        """bool 是 int 的子类，ensure 42 (int) 不当作 bool。"""
        p = ToolParameter(type="boolean")
        _, errors = validate_args({"b": p}, {"b": 1})
        assert errors  # 1 is int but not bool — should fail


class TestTruncateResult:
    """truncate_result() 测试。"""

    def test_no_truncation_needed(self):
        content, truncated = truncate_result("hello", max_len=10)
        assert content == "hello"
        assert truncated is False

    def test_truncation(self):
        content, truncated = truncate_result("hello world", max_len=5)
        assert truncated is True
        assert len(content) <= 6  # 5 + "…"

    def test_exact_boundary(self):
        content, truncated = truncate_result("hello", max_len=5)
        assert truncated is False


class TestExecuteWithTimeout:
    """execute_with_timeout() 测试。"""

    @pytest.mark.asyncio
    async def test_returns_result(self):
        async def double(x: int) -> int:
            return x * 2
        result = await execute_with_timeout(double, {"x": 21}, timeout=1.0)
        assert result == 42

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        async def slow() -> str:
            await asyncio.sleep(0.5)
            return ""
        with pytest.raises(asyncio.TimeoutError):
            await execute_with_timeout(slow, {}, timeout=0.05)


# ===================================================================
# Step 4 — 装饰器
# ===================================================================

class TestToolDecorator:
    """@tool 装饰器测试。"""

    def test_basic_decorator(self):
        @tool(description="A test tool")
        async def my_func(x: str) -> str:
            return x

        d = get_tool_definition(my_func)
        assert d is not None
        assert d.name == "my_func"
        assert d.description == "A test tool"
        assert d.func is not None
        assert d.timeout == 30.0
        assert d.max_result_len == 4000

    def test_name_from_docstring(self):
        @tool
        async def get_time() -> str:
            """Returns the current time."""
            return "now"

        d = get_tool_definition(get_time)
        assert d.name == "get_time"
        assert d.description == "Returns the current time."

    def test_custom_name(self):
        @tool(name="custom_name", description="desc")
        async def original_name() -> str:
            return ""

        d = get_tool_definition(original_name)
        assert d.name == "custom_name"

    def test_type_hints_to_params(self):
        @tool(description="search tool")
        async def search(query: str, limit: int = 5) -> str:
            """Search."""
            return ""

        d = get_tool_definition(search)
        assert "query" in d.parameters
        assert d.parameters["query"].type == "string"
        assert d.parameters["query"].required is True
        assert d.parameters["limit"].type == "integer"
        assert d.parameters["limit"].required is False
        assert d.parameters["limit"].default == 5

    def test_literal_to_enum(self):
        @tool(description="save tool")
        async def save(content: str, type: Literal["fact", "plan", "note"] = "fact") -> str:
            """Save."""
            return ""

        d = get_tool_definition(save)
        assert d.parameters["type"].enum == ["fact", "plan", "note"]

    def test_optional_param(self):
        @tool(description="optional test")
        async def greet(name: str | None = None) -> str:
            """Greet."""
            return f"Hello {name}"

        d = get_tool_definition(greet)
        assert d.parameters["name"].required is False

    def test_bool_float_params(self):
        @tool(description="mixed types")
        async def mixed(dry_run: bool = False, temperature: float = 0.7) -> str:
            return ""

        d = get_tool_definition(mixed)
        assert d.parameters["dry_run"].type == "boolean"
        assert d.parameters["temperature"].type == "number"

    def test_docstring_param_descriptions(self):
        @tool(description="doc test")
        async def search(query: str, limit: int = 5) -> str:
            """
            Search memory.

            :param query: The search query
            :param limit: Number of results
            """
            return ""

        d = get_tool_definition(search)
        assert d.parameters["query"].description == "The search query"
        assert d.parameters["limit"].description == "Number of results"

    def test_manual_parameters_override(self):
        custom = ToolParameter(type="integer", description="custom param")
        @tool(description="manual", parameters={"value": custom})
        async def manual_tool(value: str = "ignored") -> str:
            return ""

        d = get_tool_definition(manual_tool)
        assert d.parameters["value"].type == "integer"
        assert d.parameters["value"].description == "custom param"

    def test_decorated_function_still_callable(self):
        @tool(description="test")
        async def add(a: int, b: int) -> int:
            return a + b

        # decorated function should still work
        result = asyncio.get_event_loop().run_until_complete(add(a=1, b=2))
        assert result == 3

    def test_wraps_preserves_metadata(self):
        @tool(description="test")
        async def my_func() -> str:
            """My docstring."""
            return ""

        assert my_func.__name__ == "my_func"
        assert "My docstring" in (my_func.__doc__ or "")


# ===================================================================
# Step 5 — type_hints_to_params / pydantic_to_params
# ===================================================================

class TestTypeHintsToParams:
    """type_hints_to_params() 测试。"""

    def test_empty_func(self):
        async def empty() -> str:
            return ""
        params, required = type_hints_to_params(empty)
        assert params == {}
        assert required == []

    def test_str_and_int(self):
        async def fn(name: str, age: int) -> str:
            return ""
        params, required = type_hints_to_params(fn)
        assert params["name"].type == "string"
        assert params["name"].required is True
        assert params["age"].type == "integer"
        assert required == ["name", "age"]

    def test_with_defaults(self):
        async def fn(x: int = 42) -> str:
            return ""
        params, required = type_hints_to_params(fn)
        assert params["x"].required is False
        assert required == []

    def test_list_param(self):
        async def fn(items: list[str]) -> str:
            return ""
        params, _ = type_hints_to_params(fn)
        assert params["items"].type == "array"
        assert params["items"].items is not None
        assert params["items"].items.type == "string"

    def test_no_type_hint_fallback(self):
        async def fn(x) -> str:
            return ""
        params, _ = type_hints_to_params(fn)
        assert params["x"].type == "string"  # fallback

    def test_skips_self(self):
        async def fn(self, x: str) -> str:
            return ""
        params, _ = type_hints_to_params(fn)
        assert "self" not in params
        assert "x" in params


class TestPydanticToParams:
    """pydantic_to_params() 测试。"""

    def test_simple_model(self):
        from pydantic import BaseModel

        class SimpleModel(BaseModel):
            name: str
            age: int = 18

        params, required = pydantic_to_params(SimpleModel)
        assert params["name"].type == "string"
        assert params["name"].required is True
        assert params["age"].type == "integer"
        assert params["age"].required is False
        assert "name" in required
        assert "age" not in required

    def test_with_descriptions(self):
        from pydantic import BaseModel, Field

        class DescModel(BaseModel):
            query: str = Field(description="search query")
            limit: int = Field(default=5, description="max results")

        params, _ = pydantic_to_params(DescModel)
        assert params["query"].description == "search query"
        assert params["limit"].description == "max results"

    def test_not_a_pydantic_model(self):
        class NotPydantic:
            pass
        with pytest.raises(TypeError, match="not a Pydantic"):
            pydantic_to_params(NotPydantic)

    def test_nested_object(self):
        from pydantic import BaseModel

        class Address(BaseModel):
            street: str
            city: str

        class Person(BaseModel):
            name: str
            address: Address

        params, _ = pydantic_to_params(Person)
        assert params["address"].type == "object"
        assert params["address"].properties is not None
        assert "street" in params["address"].properties
        assert "city" in params["address"].properties

    def test_enum_field(self):
        from enum import Enum as PyEnum
        from pydantic import BaseModel

        class ItemType(str, PyEnum):
            fact = "fact"
            plan = "plan"

        class MemoModel(BaseModel):
            type: ItemType = ItemType.fact

        params, _ = pydantic_to_params(MemoModel)
        assert params["type"].enum is not None

    def test_nested_model_no_ref_in_output(self):
        """Pydantic 嵌套模型的 Schema 输出不应包含 $ref。"""
        from pydantic import BaseModel

        class Address(BaseModel):
            street: str
            city: str

        class Person(BaseModel):
            name: str
            address: Address

        params, required = pydantic_to_params(Person)
        d = ToolDefinition(name="test", description="test", parameters=params)
        import json
        schema_json = json.dumps(d.to_openai())
        assert "$ref" not in schema_json
        assert "$defs" not in schema_json

    def test_pydantic_field_constraints(self):
        """Field(ge/le/pattern) 约束应导出到 JSON Schema。"""
        from pydantic import BaseModel, Field

        class OrderItem(BaseModel):
            product_id: str = Field(description="商品 ID", pattern=r"^[A-Z]{2}\d{6}$")
            quantity: int = Field(default=1, ge=1, le=100, description="数量")
            discount: float = Field(default=0.0, ge=0.0, le=1.0, description="折扣率")

        params, required = pydantic_to_params(OrderItem)
        # 数值约束
        assert params["quantity"].minimum == 1
        assert params["quantity"].maximum == 100
        assert params["discount"].minimum == 0.0
        assert params["discount"].maximum == 1.0
        # 正则约束
        assert params["product_id"].pattern == r"^[A-Z]{2}\d{6}$"
        # 验证导出到 OpenAI schema
        d = ToolDefinition(name="order", description="create order", parameters=params)
        oai = d.to_openai()
        qty_schema = oai["function"]["parameters"]["properties"]["quantity"]
        assert qty_schema["minimum"] == 1
        assert qty_schema["maximum"] == 100
        pid_schema = oai["function"]["parameters"]["properties"]["product_id"]
        assert "pattern" in pid_schema

from Module.tool.protocol import MediaItem, ResultType


class TestMediaItem:
    """MediaItem 单元测试。"""

    def test_url_item(self):
        m = MediaItem(url="https://example.com/img.png", mime_type="image/png", label="chart")
        assert m.url == "https://example.com/img.png"
        assert m.data == b""
        assert m.mime_type == "image/png"
        assert m.label == "chart"

    def test_data_item(self):
        m = MediaItem(data=b"\x89PNG", mime_type="image/png", label="screenshot")
        assert m.url == ""
        assert m.data == b"\x89PNG"

    def test_defaults(self):
        m = MediaItem()
        assert m.url == ""
        assert m.data == b""
        assert m.mime_type == ""
        assert m.label == ""


class TestToolResultExtended:
    """ToolResult 扩展字段测试。"""

    def test_defaults_backward_compat(self):
        """旧代码不传新字段 → 默认 text 类型，行为不变。"""
        r = ToolResult.ok("t", "hello", duration_ms=1.0)
        assert r.result_type == "text"
        assert r.images == []
        assert r.audio is None
        assert r.files == []
        assert r.llm_summary == ""
        assert r.llm_content == "hello"

    def test_llm_content_text_type(self):
        """result_type=text → llm_content = content"""
        r = ToolResult.ok("t", "plain text", duration_ms=1.0, result_type="text")
        assert r.llm_content == "plain text"

    def test_llm_content_image_type(self):
        """result_type=image → llm_content = llm_summary"""
        r = ToolResult.ok(
            "t", content="chart.png", duration_ms=1.0,
            result_type="image", llm_summary="图表显示营收增长 30%",
        )
        assert r.llm_content == "图表显示营收增长 30%"

    def test_llm_content_fallback_to_content(self):
        """llm_summary 为空时 fallback 到 content"""
        r = ToolResult.ok("t", "raw output", duration_ms=1.0, result_type="file")
        assert r.llm_content == "raw output"

    def test_with_images(self):
        img = MediaItem(url="https://x.com/a.png", mime_type="image/png")
        r = ToolResult.ok(
            "chart", "generated", 1.0,
            result_type="image", llm_summary="revenue chart",
            images=[img],
        )
        assert len(r.images) == 1
        assert r.images[0].url == "https://x.com/a.png"

    def test_with_audio(self):
        audio = MediaItem(data=b"WAV...", mime_type="audio/wav")
        r = ToolResult.ok(
            "tts", "audio output", 1.0,
            result_type="audio", audio=audio,
        )
        assert r.audio is not None
        assert r.audio.data == b"WAV..."

    def test_with_files(self):
        f = MediaItem(url="https://x.com/report.pdf", mime_type="application/pdf", label="report")
        r = ToolResult.ok(
            "export", "report generated", 1.0,
            result_type="file", files=[f],
        )
        assert len(r.files) == 1
        assert r.files[0].mime_type == "application/pdf"


# ===================================================================
# Step 7 — ToolDefinition 扩展
# ===================================================================

class TestToolDefinitionExtended:
    """ToolDefinition 新字段测试。"""

    def test_defaults_backward_compat(self):
        d = ToolDefinition(name="test", description="test")
        assert d.result_type == "text"
        assert d.requires == []
        assert d.side_effects is False

    def test_explicit_fields(self):
        d = ToolDefinition(
            name="web_search", description="search the web",
            result_type="mixed", requires=["web_search", "network"],
            side_effects=False,
        )
        assert d.result_type == "mixed"
        assert d.requires == ["web_search", "network"]
        assert d.side_effects is False


# ===================================================================
# Step 8 — @tool 装饰器扩展
# ===================================================================

class TestToolDecoratorExtended:
    """@tool 装饰器新参数测试。"""

    def test_requires_parameter(self):
        @tool(description="web search", requires=["web_search", "network"])
        async def web_search(query: str) -> str:
            return ""

        d = get_tool_definition(web_search)
        assert d.requires == ["web_search", "network"]

    def test_side_effects_parameter(self):
        @tool(description="write file", side_effects=True)
        async def write_file(path: str) -> str:
            return ""

        d = get_tool_definition(write_file)
        assert d.side_effects is True

    def test_result_type_parameter(self):
        @tool(description="generate chart", result_type="image")
        async def chart(data: str) -> str:
            return ""

        d = get_tool_definition(chart)
        assert d.result_type == "image"

    def test_combined_new_params(self):
        @tool(
            description="search and screenshot",
            requires=["web_search", "browser"],
            result_type="mixed",
            side_effects=True,
            timeout=60.0,
        )
        async def search_screenshot(query: str) -> str:
            return ""

        d = get_tool_definition(search_screenshot)
        assert d.requires == ["web_search", "browser"]
        assert d.result_type == "mixed"
        assert d.side_effects is True
        assert d.timeout == 60.0

    def test_bare_tool_defaults(self):
        @tool
        async def simple() -> str:
            return ""

        d = get_tool_definition(simple)
        assert d.requires == []
        assert d.result_type == "text"
        assert d.side_effects is False


# ===================================================================
# Step 9 — ToolRegistry execute() ToolResult passthrough
# ===================================================================

class TestRegistryToolResultPassthrough:
    """ToolRegistry 处理工具返回 ToolResult 的测试。"""

    @pytest.fixture
    def registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_tool_returns_toolresult_directly(self, registry):
        """工具返回 ToolResult → execute() 直接使用，不二次包装。"""
        async def image_tool() -> ToolResult:
            return ToolResult.ok(
                "chart", content="chart.png", duration_ms=50.0,
                result_type="image", llm_summary="营收增长 30%",
                images=[MediaItem(url="https://x.com/chart.png", mime_type="image/png")],
            )

        registry.register(ToolDefinition(
            name="generate_chart", description="generate chart",
            func=image_tool, result_type="image",
        ))
        result = await registry.execute("generate_chart", {})
        assert result.success
        assert result.result_type == "image"
        assert result.llm_summary == "营收增长 30%"
        assert len(result.images) == 1
        assert result.tool_name == "generate_chart"

    @pytest.mark.asyncio
    async def test_str_tool_still_works(self, registry):
        """返回 str 的工具仍然正常工作（向后兼容）。"""
        async def text_tool(x: str) -> str:
            return f"echo: {x}"

        registry.register(ToolDefinition(
            name="echo", description="echo",
            func=text_tool, result_type="text",
            parameters={"x": ToolParameter(type="string", required=True)},
        ))
        result = await registry.execute("echo", {"x": "hi"})
        assert result.success
        assert result.result_type == "text"
        assert result.content.strip() == "echo: hi"
        assert result.tool_name == "echo"

    @pytest.mark.asyncio
    async def test_toolresult_history_includes_result_type(self, registry):
        async def image_tool() -> ToolResult:
            return ToolResult.ok("img", "img.png", 1.0, result_type="image")

        registry.register(ToolDefinition(
            name="img", description="", func=image_tool, result_type="image",
        ))
        await registry.execute("img", {})
        hist = registry.history[0]
        assert hist["result_type"] == "image"

    @pytest.mark.asyncio
    async def test_toolresult_truncation_not_applied(self, registry):
        """ToolResult 已经由工具自己处理了截断，registry 不再重复截断。"""
        async def long_result() -> ToolResult:
            return ToolResult.ok("t", "x" * 5000, 1.0, truncated=False)

        registry.register(ToolDefinition(
            name="long", description="", func=long_result, max_result_len=50,
        ))
        result = await registry.execute("long", {})
        assert result.success
        # 工具返回的 ToolResult 不被 registry 截断
        assert len(result.content) == 5000
        assert not result.truncated


# ===================================================================
# Step 10 — ErrorType
# ===================================================================

from Module.tool.protocol import ErrorType, ToolContext, ToolEmitter, ToolProgress


class TestErrorType:
    """ErrorType 枚举测试。"""

    def test_str_value(self):
        assert ErrorType.INVALID_ARGUMENT == "invalid_argument"
        assert ErrorType.TIMEOUT.value == "timeout"

    def test_is_retryable(self):
        assert ErrorType.INVALID_ARGUMENT.is_retryable is True
        assert ErrorType.TIMEOUT.is_retryable is True

    def test_is_not_retryable(self):
        assert ErrorType.UNAVAILABLE.is_retryable is False
        assert ErrorType.CANCELLED.is_retryable is False
        assert ErrorType.TOOL_ERROR.is_retryable is False
        assert ErrorType.UNKNOWN_TOOL.is_retryable is False


# ===================================================================
# Step 11 — ToolContext
# ===================================================================

class TestToolContext:
    """ToolContext 测试。"""

    def test_defaults(self):
        ctx = ToolContext()
        assert ctx.session_id == ""
        assert ctx.user_id == ""
        assert ctx.services == {}

    def test_get_existing_service(self):
        ctx = ToolContext(services={"memory": "mem_obj"})
        assert ctx.get("memory") == "mem_obj"

    def test_get_missing_returns_default(self):
        ctx = ToolContext()
        assert ctx.get("nonexistent") is None
        assert ctx.get("nonexistent", "fallback") == "fallback"

    def test_session_and_user(self):
        ctx = ToolContext(session_id="abc123", user_id="妖梦")
        assert ctx.session_id == "abc123"
        assert ctx.user_id == "妖梦"


# ===================================================================
# Step 12 — ToolProgress
# ===================================================================

class TestToolProgress:
    """ToolProgress 预留接口测试。"""

    def test_defaults(self):
        p = ToolProgress()
        assert p.status == "processing"
        assert p.percent is None
        assert p.message == ""
        assert p.data is None

    def test_with_values(self):
        p = ToolProgress(status="processing", percent=0.5, message="处理中...")
        assert p.percent == 0.5
        assert p.message == "处理中..."

    def test_json_serializable(self):
        """ToolProgress 能够安全地 JSON 序列化（dataclass → dict → JSON）。"""
        import json
        from dataclasses import asdict

        # 基础类型 data — 最常见场景
        p = ToolProgress(
            status="processing", percent=0.5, message="处理中...",
            data={"file": "report.pdf", "lines": 1500, "tags": ["critical", "urgent"]},
        )
        d = asdict(p)
        s = json.dumps(d, ensure_ascii=False)
        assert "processing" in s
        assert "report.pdf" in s
        assert "critical" in s

        # 纯文本 data — 简单进度更新
        p2 = ToolProgress(message="扫描完成", percent=1.0, data="done")
        json.dumps(asdict(p2))  # 不应抛异常

        # data=None — 无附加数据
        p3 = ToolProgress(message="正在连接...")
        json.dumps(asdict(p3))  # 不应抛异常

    def test_json_serializable_rejects_non_serializable(self):
        """非 JSON 兼容的 data 类型应抛出 TypeError（文档化边界）。"""
        import json
        from dataclasses import asdict

        # bytes 不能直接 JSON 序列化
        p = ToolProgress(message="处理音频", data=b"\x00\x01\x02")
        with pytest.raises((TypeError, UnicodeDecodeError)):
            json.dumps(asdict(p))


# ===================================================================
# Step 13 — ToolResult error_type
# ===================================================================

class TestToolResultErrorType:
    """ToolResult error_type 字段测试。"""

    def test_fail_default_error_type_none(self):
        """不传 error_type 时默认为 None（向后兼容）。"""
        r = ToolResult.fail("t", "error")
        assert r.error_type is None

    def test_fail_with_error_type(self):
        r = ToolResult.fail("t", "timeout", error_type=ErrorType.TIMEOUT)
        assert r.error_type == ErrorType.TIMEOUT

    def test_ok_has_no_error_type(self):
        r = ToolResult.ok("t", "ok", 1.0)
        assert r.error_type is None


# ===================================================================
# Step 14 — Hidden params (ctx) in schema
# ===================================================================

class TestHiddenParamsSchema:
    """ctx 参数对 LLM 隐藏的测试。"""

    def test_ctx_not_in_openai_schema(self):
        p = ToolParameter(type="string", description="query", required=True)
        d = ToolDefinition(
            name="search", description="search",
            parameters={"query": p},
            _hidden_params=set(),
        )
        # Without hidden params, query should appear
        oai = d.to_openai()
        assert "query" in oai["function"]["parameters"]["properties"]

    def test_hidden_param_excluded(self):
        """_hidden_params 中的参数不出现 in schema。"""
        p1 = ToolParameter(type="string", description="query", required=True)
        p2 = ToolParameter(type="string", description="secret context")
        d = ToolDefinition(
            name="search", description="search",
            parameters={"query": p1, "ctx": p2},
            _hidden_params={"ctx"},
        )
        oai = d.to_openai()
        props = oai["function"]["parameters"]["properties"]
        assert "query" in props
        assert "ctx" not in props

        # Required list should also exclude hidden param
        if "required" in oai["function"]["parameters"]:
            assert "ctx" not in oai["function"]["parameters"]["required"]


# ===================================================================
# Step 15 — @tool decorator ctx detection
# ===================================================================

class TestCtxDecorator:
    """@tool 装饰器自动检测 ctx 参数。"""

    def test_ctx_param_detected(self):
        @tool(description="test with ctx")
        async def tool_with_ctx(query: str, ctx: ToolContext) -> str:
            return ""

        d = get_tool_definition(tool_with_ctx)
        assert "ctx" in d._hidden_params
        # ctx should NOT appear in schema
        oai = d.to_openai()
        assert "ctx" not in oai["function"]["parameters"]["properties"]

    def test_optional_ctx_param_detected(self):
        @tool(description="test with optional ctx")
        async def tool_optional_ctx(query: str, ctx: ToolContext | None = None) -> str:
            return ""

        d = get_tool_definition(tool_optional_ctx)
        assert "ctx" in d._hidden_params

    def test_no_ctx_means_no_hidden_params(self):
        @tool(description="plain tool")
        async def plain_tool(query: str) -> str:
            return ""

        d = get_tool_definition(plain_tool)
        assert "ctx" not in d._hidden_params

    def test_ctx_not_in_schema_when_decorator_detected(self):
        @tool(description="search with context")
        async def search(query: str, ctx: ToolContext) -> str:
            return ""

        d = get_tool_definition(search)
        oai = d.to_openai()
        props = oai["function"]["parameters"]["properties"]
        assert "query" in props
        assert "ctx" not in props


# ===================================================================
# Step 16 — Registry ctx injection
# ===================================================================

class TestRegistryCtxInjection:
    """ToolRegistry execute() ctx 注入测试。"""

    @pytest.fixture
    def registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_ctx_injected_when_declared(self, registry):
        """ctx 参数声明的工具，execute() 时注入 ctx。"""
        async def tool_with_ctx(query: str, ctx: ToolContext) -> str:
            return f"session={ctx.session_id}, query={query}"

        registry.register(ToolDefinition(
            name="search", description="search",
            func=tool_with_ctx,
            parameters={
                "query": ToolParameter(type="string", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
        ))
        ctx = ToolContext(session_id="sess-123")
        result = await registry.execute("search", {"query": "rust"}, ctx=ctx)
        assert result.success
        assert "session=sess-123" in result.content

    @pytest.mark.asyncio
    async def test_no_ctx_when_not_declared(self, registry):
        """不声明 ctx 的工具不受影响。"""
        async def plain(query: str) -> str:
            return query

        registry.register(ToolDefinition(
            name="echo", description="echo",
            func=plain,
            parameters={"query": ToolParameter(type="string", required=True)},
        ))
        result = await registry.execute("echo", {"query": "hello"}, ctx=ToolContext())
        assert result.success
        assert result.content.strip() == "hello"

    @pytest.mark.asyncio
    async def test_optional_ctx_receives_none_when_not_provided(self, registry):
        """Optional ctx 参数，没传 ctx 时传 None。"""
        async def maybe_ctx(x: int, ctx: ToolContext | None = None) -> str:
            return "no-ctx" if ctx is None else "has-ctx"

        registry.register(ToolDefinition(
            name="maybe", description="maybe ctx",
            func=maybe_ctx,
            parameters={
                "x": ToolParameter(type="integer", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
        ))
        # 不传 ctx
        result = await registry.execute("maybe", {"x": 1})
        assert result.success
        assert "no-ctx" in result.content

    @pytest.mark.asyncio
    async def test_ctx_injection_stateless_storage(self, registry):
        """注入纯存储层（模拟 Milvus L2 记忆）→ 对 LLM Schema 隐身。"""
        class MockMemoryStore:
            """模拟向量数据库 — 无状态检索存储。"""
            def __init__(self):
                self.queries: list[str] = []

            async def search(self, query: str, top_k: int = 5) -> list[str]:
                self.queries.append(query)
                return [f"result-{i}" for i in range(min(top_k, 3))]

        store = MockMemoryStore()

        async def search_with_store(query: str, limit: int = 5, ctx: ToolContext = None) -> str:
            memory = ctx.get("memory")
            results = await memory.search(query, top_k=limit)
            return f"found {len(results)} results for '{query}'"

        registry.register(ToolDefinition(
            name="memory_search", description="搜索长期记忆",
            func=search_with_store,
            parameters={
                "query": ToolParameter(type="string", required=True),
                "limit": ToolParameter(type="integer", required=False, default=5, default_set=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
        ))
        ctx = ToolContext(session_id="mem-test", services={"memory": store})
        result = await registry.execute("memory_search", {"query": "Rust"}, ctx=ctx)
        assert result.success
        assert "3 results" in result.content
        assert store.queries == ["Rust"]

        # 验证 ctx 对 LLM Schema 隐身
        oai = registry.definitions("openai")
        props = oai[0]["function"]["parameters"]["properties"]
        assert "ctx" not in props
        assert "query" in props
        assert "limit" in props


# ===================================================================
# Step 17 — Registry error_type in fail paths
# ===================================================================

class TestRegistryErrorTypes:
    """验证所有 fail 路径都有正确的 error_type。"""

    @pytest.fixture
    def registry(self):
        reg = ToolRegistry()
        async def echo(x: str) -> str:
            return x
        reg.register(ToolDefinition(
            name="echo", description="echo",
            func=echo,
            parameters={"x": ToolParameter(type="string", required=True)},
        ))
        return reg

    @pytest.mark.asyncio
    async def test_unknown_tool_error_type(self, registry):
        result = await registry.execute("nonexistent", {})
        assert result.error_type == ErrorType.UNKNOWN_TOOL

    @pytest.mark.asyncio
    async def test_invalid_argument_error_type(self, registry):
        result = await registry.execute("echo", {})  # 缺少必填 x
        assert result.error_type == ErrorType.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_timeout_error_type(self, registry):
        async def slow() -> str:
            await asyncio.sleep(0.3)
            return ""

        registry.register(ToolDefinition(name="slow", description="", func=slow, timeout=0.05))
        result = await registry.execute("slow", {})
        assert result.error_type == ErrorType.TIMEOUT

    @pytest.mark.asyncio
    async def test_tool_error_type(self, registry):
        async def bad() -> str:
            raise ValueError("boom")

        registry.register(ToolDefinition(name="bad", description="", func=bad))
        result = await registry.execute("bad", {})
        assert result.error_type == ErrorType.TOOL_ERROR


# ===================================================================
# Step 18 — Schema 缓存
# ===================================================================

class TestSchemaCache:
    """definitions() 缓存测试。"""

    def test_cache_hit(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(name="test", description=""))
        first = reg.definitions("openai")
        second = reg.definitions("openai")
        # 深拷贝 → 不是同一引用，但内容一致
        assert first == second
        assert first is not second

    def test_cache_invalidated_on_register(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(name="a", description=""))
        reg.definitions("openai")  # populate cache
        reg.register(ToolDefinition(name="b", description=""))
        # cache 已失效 → definitions 应包含两个工具
        assert len(reg.definitions("openai")) == 2

    def test_cache_invalidated_on_unregister(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(name="a", description=""))
        reg.register(ToolDefinition(name="b", description=""))
        reg.definitions("openai")  # populate cache
        reg.unregister("a")
        assert len(reg.definitions("openai")) == 1

    def test_multiple_providers_cached(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(name="test", description=""))
        oai = reg.definitions("openai")
        ant = reg.definitions("anthropic")
        gem = reg.definitions("gemini")
        # All cached independently
        assert len(oai) == 1
        assert len(ant) == 1
        assert len(gem) == 1


# ===================================================================
# Step 19 — ctx Fail Fast
# ===================================================================

class TestCtxFailFast:
    """ctx 必需但未提供 → Fail Fast。"""

    @pytest.fixture
    def registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_ctx_required_without_ctx_fails_fast(self, registry):
        """ctx 没有默认值且调用方未提供 → 立即失败。"""
        async def needs_ctx(query: str, ctx: ToolContext) -> str:
            return ctx.session_id

        d = ToolDefinition(
            name="search", description="search",
            func=needs_ctx,
            parameters={
                "query": ToolParameter(type="string", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
            _ctx_required=True,
        )
        registry.register(d)
        result = await registry.execute("search", {"query": "rust"})
        assert not result.success
        assert "ToolContext" in result.error
        assert result.error_type == ErrorType.TOOL_ERROR

    @pytest.mark.asyncio
    async def test_ctx_required_with_ctx_works(self, registry):
        """ctx 必需且提供了 → 正常执行。"""
        async def needs_ctx(query: str, ctx: ToolContext) -> str:
            return ctx.session_id

        d = ToolDefinition(
            name="search", description="search",
            func=needs_ctx,
            parameters={
                "query": ToolParameter(type="string", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
            _ctx_required=True,
        )
        registry.register(d)
        ctx = ToolContext(session_id="sess-456")
        result = await registry.execute("search", {"query": "rust"}, ctx=ctx)
        assert result.success
        assert "sess-456" in result.content

    def test_decorator_detects_required_ctx(self):
        """@tool 自动检测 ctx 无默认值 → _ctx_required=True。"""
        @tool(description="needs ctx")
        async def with_ctx(query: str, ctx: ToolContext) -> str:
            return ""

        d = get_tool_definition(with_ctx)
        assert d._ctx_required is True
        assert "ctx" in d._hidden_params

    def test_decorator_optional_ctx_not_required(self):
        """@tool 检测 Optional ctx → _ctx_required=False。"""
        @tool(description="optional ctx")
        async def with_optional_ctx(query: str, ctx: ToolContext | None = None) -> str:
            return ""

        d = get_tool_definition(with_optional_ctx)
        assert d._ctx_required is False
        assert "ctx" in d._hidden_params


# ===================================================================
# Step 20 — LLM 友好错误消息
# ===================================================================

class TestLLMFriendlyErrors:
    """ToolResult.llm_content 返回 LLM 友好的错误消息。"""

    def test_invalid_argument_error(self):
        r = ToolResult.fail("search", "缺少必填参数: query",
                            error_type=ErrorType.INVALID_ARGUMENT)
        msg = r.llm_content
        assert "invalid arguments" in msg
        assert "retry with corrected arguments" in msg
        assert "search" in msg

    def test_timeout_error(self):
        r = ToolResult.fail("scan", "工具执行超时 (30s)",
                            error_type=ErrorType.TIMEOUT)
        msg = r.llm_content
        assert "timed out" in msg
        assert "retry with a smaller scope" in msg

    def test_unavailable_error(self):
        r = ToolResult.fail("web_search", "web_search 后端未连接",
                            error_type=ErrorType.UNAVAILABLE)
        msg = r.llm_content
        assert "unavailable" in msg
        assert "Do not retry" in msg

    def test_tool_error(self):
        r = ToolResult.fail("bad", "something broke",
                            error_type=ErrorType.TOOL_ERROR)
        msg = r.llm_content
        assert "Do not retry" in msg
        assert "something broke" in msg

    def test_success_result_unchanged(self):
        r = ToolResult.ok("echo", "hello world", 1.0)
        assert r.llm_content == "hello world"


# ===================================================================
# Step 21 — execute_stream() 异步流式
# ===================================================================

class TestExecuteStream:
    """execute_stream() 异步生成器测试。"""

    @pytest.fixture
    def registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_stream_yields_progress_then_result(self, registry):
        """异步生成器工具: yield ToolProgress → return ToolResult。"""
        from Module.tool.protocol import ToolProgress

        async def scan_task(depth: int, ctx: ToolContext | None = None):
            for i in range(3):
                yield ToolProgress(percent=i / 3, message=f"step {i}")
            yield ToolResult.ok("scan", "scan complete: 3 items found", 100.0)

        registry.register(ToolDefinition(
            name="scan", description="deep scan",
            func=scan_task,
            parameters={
                "depth": ToolParameter(type="integer", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
        ))

        events = []
        async for event in registry.execute_stream("scan", {"depth": 5}):
            events.append(event)

        assert len(events) == 4  # 3 progress + 1 result
        assert events[0].percent == 0.0
        assert events[1].percent == 1 / 3
        assert events[2].percent == 2 / 3
        assert events[3].success is True
        assert "scan complete" in events[3].content

    @pytest.mark.asyncio
    async def test_stream_non_generator_fails(self, registry):
        """普通函数用 execute_stream → 返回错误。"""
        async def regular(x: int) -> str:
            return str(x)

        registry.register(ToolDefinition(
            name="regular", description="regular",
            func=regular,
            parameters={"x": ToolParameter(type="integer", required=True)},
        ))

        events = []
        async for event in registry.execute_stream("regular", {"x": 1}):
            events.append(event)

        assert len(events) == 1
        assert events[0].success is False
        assert "不支持流式执行" in events[0].error

    @pytest.mark.asyncio
    async def test_stream_ctx_injection(self, registry):
        """execute_stream 也支持 ctx 注入。"""
        from Module.tool.protocol import ToolProgress

        async def ctx_stream(x: int, ctx: ToolContext):
            yield ToolProgress(message=ctx.session_id)
            yield ToolResult.ok("s", ctx.session_id, 1.0)

        registry.register(ToolDefinition(
            name="s", description="",
            func=ctx_stream,
            parameters={
                "x": ToolParameter(type="integer", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
        ))

        ctx = ToolContext(session_id="stream-789")
        events = []
        async for event in registry.execute_stream("s", {"x": 1}, ctx=ctx):
            events.append(event)

        assert events[0].message == "stream-789"
        assert "stream-789" in events[1].content

    @pytest.mark.asyncio
    async def test_stream_tool_exception_yields_error(self, registry):
        """生成器工具抛异常 → yield fail ToolResult。"""
        async def broken_stream():
            yield None
            raise ValueError("mid-stream crash")

        registry.register(ToolDefinition(
            name="broken", description="",
            func=broken_stream, parameters={},
        ))

        events = []
        async for event in registry.execute_stream("broken", {}):
            events.append(event)

        assert events[-1].success is False
        assert "mid-stream crash" in events[-1].error
        assert events[-1].error_type == ErrorType.TOOL_ERROR

    @pytest.mark.asyncio
    async def test_stream_unknown_tool(self, registry):
        """execute_stream 未知工具 → 返回错误。"""
        events = []
        async for event in registry.execute_stream("nope", {}):
            events.append(event)

        assert len(events) == 1
        assert events[0].error_type == ErrorType.UNKNOWN_TOOL

    @pytest.mark.asyncio
    async def test_stream_fail_fast_ctx(self, registry):
        """execute_stream ctx 必需但未提供 → Fail Fast。"""
        async def needs_ctx_stream(x: int, ctx: ToolContext):
            yield None
            yield ToolResult.ok("x", "", 1.0)

        registry.register(ToolDefinition(
            name="ns", description="",
            func=needs_ctx_stream,
            parameters={
                "x": ToolParameter(type="integer", required=True),
                "ctx": ToolParameter(type="string"),
            },
            _hidden_params={"ctx"},
            _ctx_required=True,
        ))

        events = []
        async for event in registry.execute_stream("ns", {"x": 1}):
            events.append(event)

        assert len(events) == 1
        assert events[0].success is False
        assert "ToolContext" in events[0].error


# ===================================================================
# Step 22 — ToolEmitter (emit 模式)
# ===================================================================

class TestToolEmitter:
    """ToolEmitter 单元测试。"""

    def test_progress_no_queue_is_noop(self):
        """无 queue 时 progress() 是 no-op（普通 execute() 场景）。"""
        emit = ToolEmitter(_queue=None)
        # Should not raise
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            emit.progress(0.5, "test")
        )

    def test_decorator_detects_emit(self):
        @tool(description="scan with emit")
        async def scan(emit: ToolEmitter) -> ToolResult:
            return ToolResult.ok("scan", "ok", 0)

        d = get_tool_definition(scan)
        assert "emit" in d._hidden_params

    def test_decorator_emit_not_in_schema(self):
        @tool(description="scan with emit")
        async def scan(scope: str, emit: ToolEmitter) -> ToolResult:
            return ToolResult.ok("scan", "ok", 0)

        d = get_tool_definition(scan)
        oai = d.to_openai()
        assert "emit" not in oai["function"]["parameters"]["properties"]


class TestExecuteStreamEmitter:
    """execute_stream() ToolEmitter 模式测试。"""

    @pytest.fixture
    def registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_emitter_progress_then_return(self, registry):
        """emit.progress() 上报进度 → return ToolResult。"""
        @tool(description="scan with emit")
        async def scan_task(
            scope: str, emit: ToolEmitter,
        ) -> ToolResult:
            await emit.progress(0.0, "starting")
            await emit.progress(0.5, "half done")
            await emit.progress(1.0, "complete")
            return ToolResult.ok("scan", "scan done: no issues", 200.0)

        registry.register(scan_task._tool_definition)

        events = []
        async for event in registry.execute_stream("scan_task", {"scope": "all"}):
            events.append(event)

        assert len(events) == 4  # 3 progress + 1 result
        assert events[0].percent == 0.0
        assert events[0].message == "starting"
        assert events[1].percent == 0.5
        assert events[2].percent == 1.0
        assert events[3].success is True
        assert "scan done" in events[3].content

    @pytest.mark.asyncio
    async def test_emitter_without_progress_works(self, registry):
        """即使不调 emit.progress()，也能正常返回结果。"""
        @tool(description="fast scan")
        async def fast_scan(emit: ToolEmitter) -> str:
            return "all good"

        registry.register(fast_scan._tool_definition)

        events = []
        async for event in registry.execute_stream("fast_scan", {}):
            events.append(event)

        assert len(events) == 1  # only final result
        assert events[0].success is True
        assert "all good" in events[0].content

    @pytest.mark.asyncio
    async def test_emitter_with_ctx(self, registry):
        """emit 和 ctx 可以同时使用。"""
        @tool(description="scan with both")
        async def scan_both(emit: ToolEmitter, ctx: ToolContext) -> ToolResult:
            await emit.progress(0.5, ctx.session_id)
            return ToolResult.ok("s", f"session={ctx.session_id}", 1.0)

        registry.register(scan_both._tool_definition)

        ctx = ToolContext(session_id="emit-test-123")
        events = []
        async for event in registry.execute_stream("scan_both", {}, ctx=ctx):
            events.append(event)

        assert events[0].message == "emit-test-123"
        assert "emit-test-123" in events[1].content

    @pytest.mark.asyncio
    async def test_emitter_tool_exception(self, registry):
        """emit 模式下工具抛异常 → yield fail ToolResult。"""
        @tool(description="broken emitter")
        async def broken_emit(emit: ToolEmitter) -> str:
            await emit.progress(0.1, "about to crash")
            raise ValueError("crash during emit")

        registry.register(broken_emit._tool_definition)

        events = []
        async for event in registry.execute_stream("broken_emit", {}):
            events.append(event)

        assert events[-1].success is False
        assert "crash during emit" in events[-1].error
        assert events[-1].error_type == ErrorType.TOOL_ERROR

    @pytest.mark.asyncio
    async def test_emitter_cancelled(self, registry):
        """emit 模式下 cancel → yield CANCELLED。"""
        @tool(description="slow emitter")
        async def slow_emit(emit: ToolEmitter) -> ToolResult:
            await emit.progress(0.1, "starting")
            await asyncio.sleep(10)
            return ToolResult.ok("s", "", 1.0)

        registry.register(slow_emit._tool_definition)

        collected: list = []

        async def collect():
            async for event in registry.execute_stream("slow_emit", {}):
                collected.append(event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have at least the cancelled error event
        assert any(not e.success and e.error_type == ErrorType.CANCELLED for e in collected
                   if hasattr(e, "success"))


    @pytest.mark.asyncio
    async def test_emitter_keepalive_timeout(self, registry):
        """emit 间隔超过 progress_timeout → 工具被取消。"""
        @tool(description="stuck emitter", progress_timeout=0.2)
        async def stuck_emit(emit: ToolEmitter) -> ToolResult:
            await emit.progress(0.0, "start")
            await asyncio.sleep(1.0)  # 远超过 0.2s keep-alive
            return ToolResult.ok("s", "", 1.0)

        registry.register(stuck_emit._tool_definition)

        events = []
        async for event in registry.execute_stream("stuck_emit", {}):
            events.append(event)

        assert not events[-1].success
        assert "无进度更新" in events[-1].error
        assert events[-1].error_type == ErrorType.TIMEOUT

    @pytest.mark.asyncio
    async def test_emitter_keepalive_ok(self, registry):
        """频繁 emit 保持在 keep-alive 内 → 正常完成。"""
        @tool(description="alive emitter", progress_timeout=0.3)
        async def alive_emit(emit: ToolEmitter) -> ToolResult:
            for i in range(5):
                await emit.progress(i / 5, f"step {i}")
                await asyncio.sleep(0.05)  # 远小于 0.3s
            return ToolResult.ok("a", "done", 1.0)

        registry.register(alive_emit._tool_definition)

        events = []
        async for event in registry.execute_stream("alive_emit", {}):
            events.append(event)

        assert events[-1].success
        assert "done" in events[-1].content


    @pytest.mark.asyncio
    async def test_emitter_is_cancelled(self, registry):
        """CPU 循环中用 emit.is_cancelled() 检测打断。"""
        @tool(description="heavy loop")
        async def heavy_loop(emit: ToolEmitter) -> str:
            for _ in range(1000):
                if emit.is_cancelled():
                    return "cancelled mid-loop"
                await asyncio.sleep(0.01)
            return "completed"

        registry.register(heavy_loop._tool_definition)

        async def run_and_cancel():
            events = []
            async for event in registry.execute_stream("heavy_loop", {}):
                events.append(event)
            return events

        task = asyncio.create_task(run_and_cancel())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_emitter_external_task_cancellation_cleanup(self, registry):
        """外部 task.cancel() → 工具 finally 块正确触发，清理局部资源。"""
        cleanup_log: list[str] = []

        @tool(description="task with local resource")
        async def resource_task(emit: ToolEmitter) -> str:
            """模拟获取局部资源（临时文件/HTTP Session）并在 finally 释放。"""
            local_resource = {"name": "temp_session", "closed": False}
            cleanup_log.append("acquired")
            try:
                for i in range(20):
                    if emit.is_cancelled():
                        return "cancelled"
                    await emit.progress(i / 20, f"step {i}")
                    await asyncio.sleep(0.05)
                return "done"
            finally:
                local_resource["closed"] = True
                cleanup_log.append(f"released({local_resource['name']})")

        registry.register(resource_task._tool_definition)

        async def run_stream():
            events = []
            async for event in registry.execute_stream("resource_task", {}):
                events.append(event)
            return events

        # 外部取消：创建 task → 让它启动 → cancel → 等待结束
        task = asyncio.create_task(run_stream())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 最关键断言：finally 块被执行（局部资源被释放）
        assert "acquired" in cleanup_log
        assert any("released" in entry for entry in cleanup_log), \
            f"finally 块应释放资源，cleanup_log={cleanup_log}"


class TestTruncationNotice:
    """llm_content 截断通知测试。"""

    def test_truncated_notice_in_llm_content(self):
        r = ToolResult.ok("t", "hello world", duration_ms=1.0, truncated=True)
        msg = r.llm_content
        assert "已截断" in msg
        assert "hello world" in msg

    def test_untruncated_no_notice(self):
        r = ToolResult.ok("t", "hello", duration_ms=1.0, truncated=False)
        assert "已截断" not in r.llm_content


class TestEnhancedErrorMessages:
    """增强错误消息测试。"""

    def test_invalid_argument_suggests_abandon(self):
        r = ToolResult.fail("search", "缺少必填参数: query",
                            error_type=ErrorType.INVALID_ARGUMENT)
        msg = r.llm_content
        assert "retry with corrected arguments" in msg
        assert "abandon this approach" in msg


async def _collect_stream(registry, name, args):
    events = []
    async for event in registry.execute_stream(name, args):
        events.append(event)
    return events


# ===================================================================
# Step 5 — 集成
# ===================================================================

class TestIntegration:
    """端到端集成测试。"""

    @pytest.mark.asyncio
    async def test_end_to_end(self):
        """完整流程: 定义工具 → 注册 → 执行 → 验证。"""
        @tool(description="add two numbers")
        async def add(a: int, b: int) -> int:
            """Add two integers."""
            return a + b

        registry = ToolRegistry()
        registry.register(get_tool_definition(add))

        assert len(registry) == 1
        assert "add" in registry

        result = await registry.execute("add", {"a": 3, "b": 4})
        assert result.success
        assert "7" in result.content

    @pytest.mark.asyncio
    async def test_definitions_cross_provider(self):
        """三个 provider 的 schema 都应该合法输出。"""
        @tool(description="test tool")
        async def test_tool(query: str, limit: int = 10) -> str:
            """Test."""
            return ""

        registry = ToolRegistry()
        registry.register(get_tool_definition(test_tool))

        # All three formats should work
        oai = registry.definitions("openai")
        ant = registry.definitions("anthropic")
        gem = registry.definitions("gemini")

        assert len(oai) == len(ant) == len(gem) == 1

        # All should contain the tool name
        assert oai[0]["function"]["name"] == "test_tool"
        assert ant[0]["name"] == "test_tool"
        assert gem[0]["name"] == "test_tool"

        # All should have the same properties
        oai_props = oai[0]["function"]["parameters"]["properties"]
        ant_props = ant[0]["input_schema"]["properties"]
        gem_props = gem[0]["parameters"]["properties"]

        assert set(oai_props.keys()) == set(ant_props.keys()) == set(gem_props.keys())

    @pytest.mark.asyncio
    async def test_multiple_tools_register_and_execute(self):
        @tool(description="uppercase")
        async def upper(text: str) -> str:
            return text.upper()

        @tool(description="lowercase")
        async def lower(text: str) -> str:
            return text.lower()

        reg = ToolRegistry()
        reg.register_many([get_tool_definition(upper), get_tool_definition(lower)])

        assert len(reg) == 2
        assert reg.tool_names == ["upper", "lower"]

        r1 = await reg.execute("upper", {"text": "Hello"})
        assert r1.content.strip() == "HELLO"

        r2 = await reg.execute("lower", {"text": "HELLO"})
        assert r2.content.strip() == "hello"
