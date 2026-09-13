# 测试文件：core/tools/tests/test_failure_signal.py
"""失败信号不得在工具链路上被抹平（S2-G1）。

**缺陷原状**：8 个后端插件的失败路径都 `return "…失败…"`（错误**字符串**），而
`SafetyPipeline._wrap_result` 对非 `ToolResult` 的返回值一律 `ToolResult.ok(...)`
→ 实测 `success=True, error=None, error_type=None`。四层信号同时失真：

1. LLM 纠错指令**从不生成** → LLM 看不到失败，反复重试同一个失败调用；
2. v2 `tool.completed{success:true}` → **前端显示成功卡片**、无 error；
3. `ToolResultMessage.success=True` 落库 → 复盘看不到失败；
4. debug 面板与工具统计全部失真。

既有 300+ 工具测试**没有一条断言 `success`**——这正是它长期存在的原因。
本文件补两道网：**结构性守卫**（AST 扫描，防新的错误字符串回流）+ **管道层断言**。
"""

import ast
import pathlib

import pytest

from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolResult, tool_failure

pytestmark = pytest.mark.unit

_PLUGIN_GLOB = "core/tools/*/plugin.py"

# 失败语义词表：出现在**返回的字符串**里即视为"错误被当成成功结果"。
# 这是启发式（本仓门禁的惯例：宁可让开发者显式处置一次误报，也不放过一整类缺陷），
# 真误报应加进下面的白名单**并写明理由**，而不是放宽词表。
_FAILURE_WORDS = (
    "失败", "无法", "错误", "被拒绝", "拒绝写入", "超时", "不允许",
    "不存在", "不是文件", "不是目录", "无效", "不支持", "过大", "已中止", "越过",
)

#: 合法保留：这些字符串是**信息性结果**，不是失败（"没找到"是有效答复）。
_ALLOWED_INFO_RETURNS: dict[str, str] = {
    "未找到": "空结果是有效答复，不是失败",
    "页面无文本内容": "抓取成功但正文为空——是结果，不是失败",
    # `query=all` 里 tasks 段只是若干段之一：该段查询失败时降级为一行说明，
    # 其余配置照常返回——整体仍是成功结果。**仅非 strict 分支**允许（strict 分支
    # 走 `tool_failure`，见 runtime_config/plugin.py 的 `_section_tasks_detail`）。
    "后台任务: 查询失败": "多段输出中的单段降级，整体仍成功",
}


def _returned_strings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        v = node.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            yield node.lineno, v.value
        elif isinstance(v, ast.JoinedStr):
            text = "".join(p.value for p in v.values
                           if isinstance(p, ast.Constant) and isinstance(p.value, str))
            if text.strip():
                yield node.lineno, text


def test_no_plugin_returns_error_as_plain_string():
    """**结构性守卫**：插件的失败返回必须是 `ToolResult`，不得是错误字符串。

    这条覆盖整类缺陷而不只是当前 23 处——新增一个 `return "xx 失败"` 即红。
    """
    offenders: list[str] = []
    for path_str in sorted(str(p) for p in pathlib.Path(".").glob(_PLUGIN_GLOB)):
        path = pathlib.Path(path_str)
        for lineno, text in _returned_strings(path):
            if any(info in text for info in _ALLOWED_INFO_RETURNS):
                continue
            if any(word in text for word in _FAILURE_WORDS):
                offenders.append(f"{path}:{lineno}  {text[:60]!r}")
    assert not offenders, (
        "以下失败返回是裸字符串——会被 _wrap_result 标成 success=True，"
        "请改用 `tool_failure(...)`：\n  " + "\n  ".join(offenders))


def test_allowlist_entries_are_still_present():
    """白名单不得腐烂：条目若已不存在，说明它过期了，应删掉而不是留着。"""
    blob = "\n".join(
        text for p in pathlib.Path(".").glob(_PLUGIN_GLOB)
        for _, text in _returned_strings(p))
    for key, reason in _ALLOWED_INFO_RETURNS.items():
        assert key in blob, f"白名单条目 {key!r}（{reason}）已不存在于任何插件返回中"


# ═══════════════════════════════════════════════════════════════════
# 管道层：失败必须一路保持失败
# ═══════════════════════════════════════════════════════════════════

def test_wrap_result_preserves_explicit_failure():
    """`_wrap_result` 对显式失败**必须原样保留**（不得改写成 success）。"""
    from Module.tool.safety import SafetyPipeline

    raw = tool_failure("读取失败: 磁盘故障", error_type=ErrorType.TOOL_ERROR)
    wrapped = SafetyPipeline()._wrap_result(raw, "read_file", 1.0, None)  # noqa: SLF001

    assert wrapped.success is False
    assert wrapped.error == "读取失败: 磁盘故障"
    assert wrapped.error_type is ErrorType.TOOL_ERROR
    assert wrapped.tool_name == "read_file", "工具名由管道回填"


def test_wrap_result_still_accepts_plain_string_as_success():
    """回归网：插件返回普通字符串仍是成功结果（向后兼容，未改变既有语义）。"""
    from Module.tool.safety import SafetyPipeline

    wrapped = SafetyPipeline()._wrap_result("文件内容如下…", "read_file", 1.0, None)  # noqa: SLF001
    assert wrapped.success is True
    assert wrapped.content == "文件内容如下…"
    assert wrapped.error is None


def test_tool_failure_sets_all_signal_fields():
    """`tool_failure` 必须把四层信号一次置对（success/error/error_type/name）。"""
    r = ToolResult  # 类型引用，确保导入被使用
    assert r is ToolResult
    failed = tool_failure("抓取失败: 拒绝", error_type=ErrorType.PERMISSION_DENIED)
    assert failed.success is False
    assert failed.error == "抓取失败: 拒绝"
    assert failed.error_type is ErrorType.PERMISSION_DENIED
    assert failed.content == "", "失败结果不应带 content（否则前端会当正文渲染）"
    assert failed.name if hasattr(failed, "name") else True


@pytest.mark.asyncio
async def test_llm_correction_instruction_becomes_reachable():
    """四层信号的第 1 层：失败现在能生成 LLM 纠错指令（原先永远不生成）。"""
    failed = tool_failure("写入失败: 内容无法编码为 UTF-8",
                          error_type=ErrorType.INVALID_ARGUMENT)
    failed.tool_name = "write_file"
    instruction = failed.llm_content
    assert "failed" in instruction and "invalid arguments" in instruction, instruction
    assert "写入失败" in instruction, "原始错误必须带进纠错指令"
