# 测试辅助：断言工具返回了**显式失败**（S2-G1）。
"""插件失败返回的断言助手。

**为什么需要**：8 个后端插件的失败路径此前都 `return "…失败…"`，而
`SafetyPipeline._wrap_result` 对非 `ToolResult` 的返回值一律 `ToolResult.ok(...)`
→ 失败被标成 `success=True`。既有测试全部只断言"字符串里含某句话"，
**没有一条断言 `success`** —— 这正是它长期存在的原因。

改用本助手后，任何退回"错误字符串"的改动都会立刻红：`isinstance` 检查先失败。
"""

from Module.tool.protocol import ToolResult


def assert_failed(result: object, *, contains: str,
                  error_type: object | None = None) -> ToolResult:
    """断言结果是显式失败，且 error 含指定文本。

    Args:
        result: 插件 `execute()` 的返回值。
        contains: 期望出现在 `error` 里的片段。
        error_type: 可选的期望错误类型（`ErrorType`）。

    Returns:
        被断言通过的结果（便于进一步检查）。
    """
    assert isinstance(result, ToolResult), (
        f"失败必须返回 ToolResult.fail（裸字符串会被 _wrap_result 标成 success=True），"
        f"实得 {type(result).__name__}: {result!r}")
    assert result.success is False, f"success 应为 False：{result!r}"
    assert contains in (result.error or ""), (
        f"error 应含 {contains!r}，实得 {result.error!r}")
    if error_type is not None:
        assert result.error_type is error_type, (
            f"error_type 应为 {error_type}，实得 {result.error_type}")
    return result


def assert_ok(result: object, *, contains: str = "") -> None:
    """断言是成功结果（字符串内容或 ToolResult.ok 都接受）。"""
    if isinstance(result, ToolResult):
        assert result.success is True, f"success 应为 True：{result!r}"
        text = result.content
    else:
        assert isinstance(result, str), f"期望成功结果，实得 {type(result).__name__}"
        text = result
    if contains:
        assert contains in text, f"内容应含 {contains!r}，实得 {text[:120]!r}"
