"""Tool registry — OpenAI function-calling compatible tool definitions.

Tools are async callables registered with a JSON Schema for their parameters.
Type coercion: JSON only has numbers/strings/booleans, so the registry inspects
the schema's "type" field and coerces argument values automatically.
"""

from typing import Any, Callable, Coroutine

ToolFunc = Callable[..., Coroutine[Any, Any, str]]


class ToolRegistry:
    """Registry of tools exposed to the LLM via OpenAI tool-calling schema."""

    def __init__(self):
        self._tools: dict[str, tuple[dict, ToolFunc, dict]] = {}
        self._history: list[dict] = []  # recent execute() calls

    def register(self, name: str, description: str, parameters: dict, func: ToolFunc) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }
        self._tools[name] = (schema, func, parameters)

    def tool_definitions(self) -> list[dict]:
        return [schema for schema, _, _ in self._tools.values()]

    def get_func(self, name: str) -> ToolFunc | None:
        entry = self._tools.get(name)
        return entry[1] if entry else None

    async def execute(self, name: str, arguments: dict) -> str:
        """Execute a tool by name with coerced arguments. Returns result string."""
        func = self.get_func(name)
        if func is None:
            return f"Error: unknown tool '{name}'"
        try:
            coerced = self._coerce(name, arguments)
            result = await func(**coerced)
            result_str = str(result)
            self._history.append({"name": name, "args": coerced, "result": result_str})
            if len(self._history) > 20:
                self._history = self._history[-20:]
            return result_str
        except Exception as e:
            err_msg = f"Error executing '{name}': {e}"
            self._history.append({"name": name, "args": arguments, "error": str(e)})
            if len(self._history) > 20:
                self._history = self._history[-20:]
            return err_msg

    def _coerce(self, name: str, arguments: dict) -> dict:
        """Coerce argument values based on the tool's parameter schema."""
        _, _, params = self._tools.get(name, ({}, None, {}))
        props = params.get("properties", {})
        coerced = dict(arguments)
        for key, value in coerced.items():
            prop = props.get(key, {})
            ptype = prop.get("type", "string")
            if ptype == "integer" and isinstance(value, str):
                try:
                    coerced[key] = int(value)
                except ValueError:
                    pass
            elif ptype == "number" and isinstance(value, str):
                try:
                    coerced[key] = float(value)
                except ValueError:
                    pass
            elif ptype == "boolean" and isinstance(value, str):
                coerced[key] = value.lower() in ("true", "1", "yes")
        return coerced

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
