"""Schema 生成 — 从 type hints / Pydantic model 推导 ToolParameter。

两条路径:
  A) type_hints_to_params(func) — 从函数签名推导（@tool 装饰器路径）
  B) pydantic_to_params(model)  — 从 Pydantic BaseModel 推导（复杂工具路径）
"""

from __future__ import annotations

import inspect
import re
import typing
from typing import Any, Callable

from .protocol import ToolParameter

# Python type → JSON Schema type
_PY_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def _get_origin(tp: Any) -> Any:
    """typing.get_origin 的兼容包装。"""
    return getattr(tp, "__origin__", None)


def _get_args(tp: Any) -> tuple:
    """typing.get_args 的兼容包装。"""
    return typing.get_args(tp)


def _resolve_type(tp: Any) -> tuple[str, dict[str, Any]]:
    """将 Python type hint 解析为 JSON Schema type 字符串 + 额外属性。

    Returns:
        (json_type, extras) — extras 包含 items/params/enum/default 等
    """
    extras: dict[str, Any] = {}

    # typing.Literal["a", "b"] → enum
    if _get_origin(tp) is typing.Literal:
        args = _get_args(tp)
        if all(isinstance(a, str) for a in args):
            extras["enum"] = list(args)
        return "string", extras

    # typing.Optional[X] → required=False, unwrap inner
    if _get_origin(tp) is typing.Union:
        args = _get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            inner_type, inner_extras = _resolve_type(non_none[0])
            inner_extras["required"] = False
            return inner_type, inner_extras
        # Union[str, int] etc. → fallback to string
        return "string", extras

    # list[X] / typing.List[X] → array
    if _get_origin(tp) in (list, typing.List):
        args = _get_args(tp)
        if args:
            item_type, item_extras = _resolve_type(args[0])
            extras["items"] = ToolParameter(type=item_type, **item_extras)
        return "array", extras

    # dict / typing.Dict → object
    if _get_origin(tp) in (dict, typing.Dict):
        return "object", extras

    # Direct type mapping
    json_type = _PY_TO_JSON.get(tp, "string")
    return json_type, extras


def type_hints_to_params(func: Callable) -> tuple[dict[str, ToolParameter], list[str]]:
    """从函数签名自动生成 ToolParameter 映射和 required 列表。

    支持: str, int, float, bool, dict, list, Optional[X], Literal["a", "b"],
          list[X], dict
    不支持: Union[A, B]（非 optional）、Callable、Any（fallback 到 string）

    docstring 中的 :param name: description 用于填充 ToolParameter.description。
    """
    hints = typing.get_type_hints(func)
    sig = inspect.signature(func)

    # 解析 docstring 中的 :param 行
    param_descriptions: dict[str, str] = {}
    if func.__doc__:
        for line in func.__doc__.split("\n"):
            m = re.match(r"^\s*:param\s+(\w+):\s*(.*)", line)
            if m:
                param_descriptions[m.group(1)] = m.group(2).strip()

    params: dict[str, ToolParameter] = {}
    required: list[str] = []

    for pname, pobj in sig.parameters.items():
        # 跳过 self / cls
        if pname in ("self", "cls"):
            continue

        description = param_descriptions.get(pname, "")
        has_default = pobj.default is not inspect.Parameter.empty
        default = pobj.default if has_default else None

        if pname in hints:
            json_type, extras = _resolve_type(hints[pname])
        else:
            # 无 type hint → fallback string
            json_type, extras = "string", {}

        # required 优先级: type_resolve > 有默认值
        is_required = not has_default
        if "required" in extras:
            is_required = extras.pop("required")

        kw: dict[str, Any] = {
            "type": json_type,
            "description": description,
            "required": is_required,
        }
        if has_default:
            kw["default"] = default
            kw["default_set"] = True

        for key in ("enum", "items"):
            if key in extras:
                kw[key] = extras[key]

        params[pname] = ToolParameter(**kw)
        if is_required:
            required.append(pname)

    return params, required


def pydantic_to_params(model: type) -> tuple[dict[str, ToolParameter], list[str]]:
    """从 Pydantic BaseModel 推导 ToolParameter。

    使用 model.model_json_schema() 解析 properties/required/description/enum，
    支持嵌套 object、$ref 引用、array of objects。
    """
    try:
        schema = model.model_json_schema()
    except AttributeError:
        raise TypeError(f"{model.__name__} is not a Pydantic BaseModel")

    definitions = schema.get("$defs", {})
    params: dict[str, ToolParameter] = {}
    required: list[str] = list(schema.get("required", []))

    for prop_name, prop_schema in schema.get("properties", {}).items():
        param = _json_schema_to_param(prop_name, prop_schema, definitions,
                                       prop_name in required)
        params[prop_name] = param

    return params, required


def _json_schema_to_param(
    name: str, schema: dict, definitions: dict, is_required: bool,
) -> ToolParameter:
    """递归地将 JSON Schema property 转为 ToolParameter。

    处理 $ref、allOf、anyOf（Optional → required=False）、嵌套 object/array。
    """
    # Resolve $ref
    if "$ref" in schema:
        ref_path = schema["$ref"]
        # "#/$defs/Address" → "Address"
        ref_key = ref_path.split("/")[-1]
        if ref_key in definitions:
            resolved = definitions[ref_key]
            return _json_schema_to_param(
                name, resolved, definitions, is_required,
            )

    # anyOf with None → Optional (required=False)
    if "anyOf" in schema:
        types = [s.get("type") for s in schema["anyOf"] if "type" in s]
        has_null = any(s.get("type") == "null" for s in schema["anyOf"])
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if has_null and len(non_null) == 1:
            # Optional field — unwrap the non-null type
            return _json_schema_to_param(
                name, non_null[0], definitions, is_required=False,
            )
        # Fallback: use first type
        json_type = types[0] if types else "string"
    else:
        json_type = schema.get("type", "string")

    description = schema.get("description", "") or schema.get("title", "")
    enum = schema.get("enum")

    param = ToolParameter(
        type=json_type,
        description=description,
        required=is_required,
        enum=[str(e) for e in enum] if enum else None,
        minimum=schema.get("minimum"),
        maximum=schema.get("maximum"),
        pattern=schema.get("pattern"),
    )
    if "default" in schema:
        param.default = schema["default"]
        param.default_set = True

    # Nested object
    if json_type == "object" and "properties" in schema:
        obj_required = set(schema.get("required", []))
        sub_defs = schema.get("$defs", definitions)
        param.properties = {}
        for child_name, child_schema in schema["properties"].items():
            param.properties[child_name] = _json_schema_to_param(
                child_name, child_schema, sub_defs, child_name in obj_required,
            )

    # Array with items
    if json_type == "array" and "items" in schema:
        items_schema = schema["items"]
        param.items = _json_schema_to_param("items", items_schema, definitions, True)

    return param


# Keep old name for backward compat
_json_schema_property_to_param = _json_schema_to_param
