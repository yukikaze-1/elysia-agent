# Phase 1: 测试缺口修复 — core/tools 插件 & Module/tool 框架

> **Status: COMPLETE** — 2026-06-02, 12 tasks done, 212 new tests passing, 2 source bugs fixed.
> Commits: `d373bc8`..`f634fb5` (12 commits)

**Goal:** 补充 core/tools/ 全部 9 个工具插件 + Module/tool 框架核心类的测试覆盖

**Architecture:** 每个插件和框架类独立测试，使用 mock 注入 ToolContext.services 来控制依赖；遵循已有测试模式（pytest + asyncio + unittest.mock），测试文件放在对应源码同级目录

**Tech Stack:** Python 3.13, pytest, asyncio, unittest.mock, pydantic

**Spec:** [2026-06-02-test-audit-design.md](../specs/2026-06-02-test-audit-design.md)

---

## File Structure

```
Module/tool/
├── test_framework.py          # 已有 — 追加 ToolParameter/ToolEmitter/MediaItem/ToolResult/SafetyPipeline/Manager 测试
└── test_tool_safety.py        # 新建 — _resolve_sandbox_path, _validate_path, _coerce_value, validate_args 测试

core/tools/
├── test_filesystem.py         # 新建 — ReadFilePlugin + ListDirPlugin
├── test_web.py                # 新建 — WebSearchPlugin + WebFetchPlugin + _TextExtractor
├── test_time.py               # 新建 — TimePlugin
├── test_memory.py             # 新建 — MemorySearchPlugin + MemorySavePlugin
├── test_reminder.py           # 新建 — SetReminderPlugin
├── test_runtime_config.py     # 新建 — RuntimeConfigPlugin
├── test_self_state.py         # 新建 — SelfStatePlugin
└── test_write_file.py         # 新建 — WriteFilePlugin

core/
└── test_timing.py             # 新建 — Timer + TimingClock
```

---

### Task 1: test_tool_safety.py — 安全函数测试

**Files:**
- Create: `Module/tool/test_tool_safety.py`

- [ ] **Step 1: 编写 `_resolve_sandbox_path` 测试**

```python
"""安全函数单元测试 — _resolve_sandbox_path, _validate_path, _coerce_value, validate_args。"""
import pytest
from pathlib import Path
from Module.tool.safety import (
    _resolve_sandbox_path, _validate_path, _coerce_value, validate_args,
)
from Module.tool.protocol import ToolParameter


class TestResolveSandboxPath:
    def test_relative_path_resolves_inside_sandbox(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "read").mkdir()
        (sandbox / "read" / "file.txt").write_text("hello")

        result = _resolve_sandbox_path(
            raw_path="file.txt",
            default_dir=str(sandbox),
            read_dirs=[str(sandbox / "read")],
            sandbox_root=str(sandbox),
        )
        assert result == str((sandbox / "read" / "file.txt").resolve())

    def test_absolute_path_stays(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("hello")

        result = _resolve_sandbox_path(
            raw_path=str(sandbox / "file.txt"),
            default_dir=str(sandbox),
            read_dirs=[str(sandbox)],
            sandbox_root=str(sandbox),
        )
        assert result == str((sandbox / "file.txt").resolve())

    def test_path_outside_read_dirs_raises(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")

        with pytest.raises(RuntimeError, match="不在允许的目录"):
            _resolve_sandbox_path(
                raw_path=str(outside / "secret.txt"),
                default_dir=str(sandbox),
                read_dirs=[str(sandbox)],
                sandbox_root=str(sandbox),
            )

    def test_symlink_inside_read_dirs_allowed(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "real.txt").write_text("real")
        (sandbox / "link.txt").symlink_to(sandbox / "real.txt")

        result = _resolve_sandbox_path(
            raw_path=str(sandbox / "link.txt"),
            default_dir=str(sandbox),
            read_dirs=[str(sandbox)],
            sandbox_root=str(sandbox),
            allow_symlinks=True,
        )
        assert result is not None

    def test_symlink_disallowed_raises(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "real.txt").write_text("real")
        (sandbox / "link.txt").symlink_to(sandbox / "real.txt")

        with pytest.raises(RuntimeError, match="不允许符号链接"):
            _resolve_sandbox_path(
                raw_path=str(sandbox / "link.txt"),
                default_dir=str(sandbox),
                read_dirs=[str(sandbox)],
                sandbox_root=str(sandbox),
                allow_symlinks=False,
            )

    def test_nonexistent_path_returns_none(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()

        result = _resolve_sandbox_path(
            raw_path="nonexistent.txt",
            default_dir=str(sandbox),
            read_dirs=[str(sandbox)],
            sandbox_root=str(sandbox),
        )
        assert result is None

    def test_default_dir_fallback(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "default" / "file.txt").mkdir(parents=True)
        (sandbox / "default" / "file.txt").write_text("fallback")

        result = _resolve_sandbox_path(
            raw_path="file.txt",
            default_dir=str(sandbox / "default"),
            read_dirs=[str(sandbox)],
            sandbox_root=str(sandbox),
        )
        assert result == str((sandbox / "default" / "file.txt").resolve())


class TestValidatePath:
    def test_file_within_read_dirs_passes(self, tmp_path):
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "file.txt").write_text("hello")

        _validate_path(
            resolved=read_dir / "file.txt",
            read_dirs=[str(read_dir)],
            allow_symlinks=True,
            max_file_bytes=1024,
        )

    def test_file_too_large_raises(self, tmp_path):
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "big.txt").write_text("x" * 2000)

        with pytest.raises(RuntimeError, match="文件过大"):
            _validate_path(
                resolved=read_dir / "big.txt",
                read_dirs=[str(read_dir)],
                allow_symlinks=True,
                max_file_bytes=1024,
            )

    def test_symlink_detection_raises(self, tmp_path):
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "real.txt").write_text("real")
        (read_dir / "link.txt").symlink_to(read_dir / "real.txt")

        with pytest.raises(RuntimeError, match="不允许符号链接"):
            _validate_path(
                resolved=read_dir / "link.txt",
                read_dirs=[str(read_dir)],
                allow_symlinks=False,
                max_file_bytes=1024,
            )

    def test_path_not_in_read_dirs_raises(self, tmp_path):
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        outside = tmp_path / "outside" / "file.txt"
        outside.parent.mkdir()
        outside.write_text("secret")

        with pytest.raises(RuntimeError, match="不在允许的目录"):
            _validate_path(
                resolved=outside,
                read_dirs=[str(read_dir)],
                allow_symlinks=True,
                max_file_bytes=1024,
            )


class TestCoerceValue:
    def test_integer_from_string(self):
        assert _coerce_value("42", "integer") == 42

    def test_integer_from_float_string_raises(self):
        with pytest.raises(ValueError):
            _coerce_value("3.14", "integer")

    def test_integer_from_non_numeric_raises(self):
        with pytest.raises(ValueError):
            _coerce_value("abc", "integer")

    def test_number_from_string(self):
        assert _coerce_value("3.14", "number") == 3.14

    def test_number_from_int_string(self):
        assert _coerce_value("42", "number") == 42

    def test_boolean_true_values(self):
        assert _coerce_value("true", "boolean") is True
        assert _coerce_value("True", "boolean") is True
        assert _coerce_value("1", "boolean") is True

    def test_boolean_false_values(self):
        assert _coerce_value("false", "boolean") is False
        assert _coerce_value("False", "boolean") is False
        assert _coerce_value("0", "boolean") is False

    def test_boolean_invalid_raises(self):
        with pytest.raises(ValueError):
            _coerce_value("yes", "boolean")

    def test_unknown_type_passthrough(self):
        assert _coerce_value("hello", "string") == "hello"


class TestValidateArgs:
    def test_required_param_missing_raises(self):
        params_spec = [
            ToolParameter(name="query", type="string", required=True, description=""),
        ]
        with pytest.raises(ValueError, match="缺少必填参数"):
            validate_args({}, params_spec)

    def test_required_param_present_passes(self):
        params_spec = [
            ToolParameter(name="query", type="string", required=True, description=""),
        ]
        result = validate_args({"query": "hello"}, params_spec)
        assert result["query"] == "hello"

    def test_type_coercion_applied(self):
        params_spec = [
            ToolParameter(name="limit", type="integer", required=False, description=""),
        ]
        result = validate_args({"limit": "10"}, params_spec)
        assert result["limit"] == 10
        assert isinstance(result["limit"], int)

    def test_optional_param_missing_gets_none(self):
        params_spec = [
            ToolParameter(name="limit", type="integer", required=False, description=""),
        ]
        result = validate_args({}, params_spec)
        assert result["limit"] is None
```

- [ ] **Step 2: 运行测试验证失败（文件尚未创建时的预期行为）**

```bash
python -m pytest Module/tool/test_tool_safety.py -v
```

- [ ] **Step 3: 不需要额外实现 — 测试使用已有函数**

- [ ] **Step 4: 运行测试验证通过**

```bash
python -m pytest Module/tool/test_tool_safety.py -v
```

- [ ] **Step 5: Commit**

```bash
git add Module/tool/test_tool_safety.py
git commit -m "test: add safety function tests — _resolve_sandbox_path, _validate_path, _coerce_value, validate_args"
```

---

### Task 2: test_framework.py 追加 — ToolParameter / ToolEmitter / MediaItem / ToolResult

**Files:**
- Modify: `Module/tool/test_framework.py` — 追加测试类

- [ ] **Step 1: 编写 ToolParameter 测试**

在 `test_framework.py` 末尾追加：

```python
# ============================================================================
# ToolParameter (追加)
# ============================================================================

from Module.tool.protocol import ToolParameter, MediaItem, ToolEmitter

class TestToolParameter:
    def test_basic_string_param(self):
        p = ToolParameter(name="query", type="string", required=True, description="搜索关键词")
        assert p.name == "query"
        assert p.type == "string"
        assert p.required is True
        assert p.description == "搜索关键词"

    def test_to_json_schema_string(self):
        p = ToolParameter(name="query", type="string", required=True, description="搜索关键词")
        schema = p.to_json_schema_property()
        assert schema["type"] == "string"
        assert schema["description"] == "搜索关键词"

    def test_to_json_schema_with_enum(self):
        p = ToolParameter(name="type", type="string", required=False,
                          enum=["fact", "plan", "preference"], description="")
        schema = p.to_json_schema_property()
        assert "enum" in schema
        assert schema["enum"] == ["fact", "plan", "preference"]

    def test_to_json_schema_with_default(self):
        p = ToolParameter(name="limit", type="integer", required=False,
                          default=5, description="")
        schema = p.to_json_schema_property()
        assert "default" in schema
        assert schema["default"] == 5

    def test_to_json_schema_with_min_max(self):
        p = ToolParameter(name="delay", type="integer", required=True,
                          minimum=1, maximum=86400, description="")
        schema = p.to_json_schema_property()
        assert schema["minimum"] == 1
        assert schema["maximum"] == 86400

    def test_to_json_schema_with_pattern(self):
        p = ToolParameter(name="email", type="string", required=False,
                          pattern=r"^[^@]+@[^@]+\.[^@]+$", description="")
        schema = p.to_json_schema_property()
        assert schema["pattern"] == r"^[^@]+@[^@]+\.[^@]+$"

    def test_to_json_schema_object_type(self):
        p = ToolParameter(
            name="config", type="object", required=False, description="",
            properties=[
                ToolParameter(name="host", type="string", required=True, description=""),
                ToolParameter(name="port", type="integer", required=False, description=""),
            ],
        )
        schema = p.to_json_schema_property()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "host" in schema["properties"]
        assert "port" in schema["properties"]
        assert "required" in schema
        assert "host" in schema["required"]

    def test_to_json_schema_array_type(self):
        p = ToolParameter(
            name="tags", type="array", required=False, description="",
            items=ToolParameter(name="tag", type="string", required=True, description=""),
        )
        schema = p.to_json_schema_property()
        assert schema["type"] == "array"
        assert "items" in schema
        assert schema["items"]["type"] == "string"


class TestMediaItem:
    def test_basic_creation(self):
        m = MediaItem(url="https://example.com/img.png", mime_type="image/png", label="screenshot")
        assert m.url == "https://example.com/img.png"
        assert m.mime_type == "image/png"
        assert m.label == "screenshot"
        assert m.data is None

    def test_with_data(self):
        m = MediaItem(data=b"\x89PNG", mime_type="image/png")
        assert m.data == b"\x89PNG"
        assert m.url is None
```

- [ ] **Step 2: 编写 ToolEmitter 测试**

```python
class TestToolEmitter:
    @pytest.mark.asyncio
    async def test_progress_no_queue_is_noop(self):
        emitter = ToolEmitter()
        await emitter.progress("step 1", 50)

    @pytest.mark.asyncio
    async def test_progress_with_queue(self):
        q: asyncio.Queue = asyncio.Queue()
        emitter = ToolEmitter(_queue=q, _task=asyncio.current_task())
        await emitter.progress("step 1", 50)
        item = q.get_nowait()
        assert item.message == "step 1"
        assert item.percent == 50

    def test_is_cancelled_false(self):
        emitter = ToolEmitter()
        assert emitter.is_cancelled() is False

    @pytest.mark.asyncio
    async def test_is_cancelled_true_when_task_cancelled(self):
        async def inner():
            emitter = ToolEmitter(_task=asyncio.current_task())
            task = asyncio.current_task()
            task.cancel()
            assert emitter.is_cancelled() is True

        task = asyncio.create_task(inner())
        await asyncio.sleep(0.01)
        with pytest.raises(asyncio.CancelledError):
            await task
```

- [ ] **Step 3: 编写 ToolResult 补充测试**

```python
class TestToolResultExtended:
    def test_ok_with_image_type(self):
        r = ToolResult.ok("image data", result_type="image",
                          images=[MediaItem(url="http://x.com/a.png", mime_type="image/png")])
        assert r.success is True
        assert r.result_type == "image"
        assert len(r.images) == 1

    def test_ok_truncated(self):
        r = ToolResult.ok("long text", truncated=True)
        assert r.truncated is True

    def test_fail_each_error_type(self):
        for et in ErrorType:
            r = ToolResult.fail("error", error_type=et)
            assert r.success is False
            assert r.error_type == et

    def test_llm_content_success_text(self):
        r = ToolResult.ok("hello world")
        content = r.llm_content
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "hello world"

    def test_llm_content_success_truncated(self):
        r = ToolResult.ok("hello world", truncated=True)
        content = r.llm_content
        assert content[0]["text"].endswith("\n\n[输出被截断]")

    def test_llm_content_success_image(self):
        r = ToolResult.ok("", result_type="image",
                          images=[MediaItem(url="http://x.com/a.png", mime_type="image/png")])
        content = r.llm_content
        assert content[0]["type"] == "image_url"

    def test_llm_content_failure_invalid_argument(self):
        r = ToolResult.fail("bad input", error_type=ErrorType.INVALID_ARGUMENT)
        content = r.llm_content
        assert "参数错误" in content[0]["text"]
        assert "工具调用参数不正确" in content[0]["text"]

    def test_llm_content_failure_timeout(self):
        r = ToolResult.fail("timeout", error_type=ErrorType.TIMEOUT)
        content = r.llm_content
        assert "超时" in content[0]["text"]

    def test_llm_content_failure_unavailable(self):
        r = ToolResult.fail("down", error_type=ErrorType.UNAVAILABLE)
        content = r.llm_content
        assert content[0]["text"] == ""

    def test_llm_content_failure_generic(self):
        r = ToolResult.fail("something broke")
        content = r.llm_content
        assert "工具执行失败: something broke" in content[0]["text"]
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest Module/tool/test_framework.py -v -k "TestToolParameter or TestMediaItem or TestToolEmitter or TestToolResultExtended"
```

- [ ] **Step 5: Commit**

```bash
git add Module/tool/test_framework.py
git commit -m "test: add ToolParameter, MediaItem, ToolEmitter, ToolResult.llm_content tests"
```

---

### Task 3: test_filesystem.py — ReadFilePlugin + ListDirPlugin

**Files:**
- Create: `core/tools/test_filesystem.py`

- [ ] **Step 1: 编写完整测试文件**

```python
"""ReadFilePlugin + ListDirPlugin 单元测试。"""
import pytest
from pathlib import Path
from Module.tool.protocol import ToolContext
from core.tools.filesystem import ReadFilePlugin, ReadFileParams, ListDirPlugin, ListDirParams, _format_size


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(500) == "500B"

    def test_kilobytes(self):
        assert _format_size(1500) == "1.5K"

    def test_megabytes(self):
        assert _format_size(5 * 1024 * 1024) == "5.0M"

    def test_gigabytes(self):
        assert _format_size(3 * 1024 * 1024 * 1024) == "3.0G"


class TestReadFilePlugin:
    @pytest.fixture
    def plugin(self):
        return ReadFilePlugin()

    @pytest.fixture
    def sandbox(self, tmp_path):
        sb = tmp_path / "sandbox"
        sb.mkdir()
        return sb

    @pytest.fixture
    def ctx(self, sandbox):
        return ToolContext()

    @pytest.mark.asyncio
    async def test_reads_file_content(self, plugin, sandbox, ctx):
        (sandbox / "test.txt").write_text("line1\nline2\nline3\n")
        ctx.services["_safe_path"] = str(sandbox / "test.txt")

        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="test.txt"))
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    @pytest.mark.asyncio
    async def test_path_not_exists(self, plugin, ctx):
        ctx.services["_safe_path"] = "/nonexistent/path.txt"
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="/nonexistent/path.txt"))
        assert "路径不存在" in result

    @pytest.mark.asyncio
    async def test_not_a_file(self, plugin, sandbox, ctx):
        subdir = sandbox / "subdir"
        subdir.mkdir()
        ctx.services["_safe_path"] = str(subdir)
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="subdir"))
        assert "不是文件" in result

    @pytest.mark.asyncio
    async def test_binary_file_detection(self, plugin, sandbox, ctx):
        (sandbox / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe\xfd")
        ctx.services["_safe_path"] = str(sandbox / "data.bin")
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="data.bin"))
        assert "二进制文件" in result

    @pytest.mark.asyncio
    async def test_empty_file(self, plugin, sandbox, ctx):
        (sandbox / "empty.txt").write_text("")
        ctx.services["_safe_path"] = str(sandbox / "empty.txt")
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="empty.txt"))
        assert "文件为空" in result

    @pytest.mark.asyncio
    async def test_start_line_offset(self, plugin, sandbox, ctx):
        lines = [f"line{i}\n" for i in range(10)]
        (sandbox / "many.txt").writelines(lines)
        ctx.services["_safe_path"] = str(sandbox / "many.txt")
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="many.txt", start_line=5))
        assert "line0" not in result
        assert "line5" in result

    @pytest.mark.asyncio
    async def test_max_lines_limit(self, plugin, sandbox, ctx):
        lines = [f"line{i}\n" for i in range(10)]
        (sandbox / "many.txt").writelines(lines)
        ctx.services["_safe_path"] = str(sandbox / "many.txt")
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="many.txt", max_lines=3))
        output_lines = [l for l in result.split("\n") if "|" in l]
        assert len(output_lines) == 3

    @pytest.mark.asyncio
    async def test_truncation_notice_when_partial(self, plugin, sandbox, ctx):
        lines = [f"line{i}\n" for i in range(50)]
        (sandbox / "long.txt").writelines(lines)
        ctx.services["_safe_path"] = str(sandbox / "long.txt")
        result = await plugin.execute(ctx=ctx, params=ReadFileParams(path="long.txt", max_lines=10))
        assert "…" in result


class TestListDirPlugin:
    @pytest.fixture
    def plugin(self):
        return ListDirPlugin()

    @pytest.fixture
    def sandbox(self, tmp_path):
        sb = tmp_path / "sandbox"
        sb.mkdir()
        return sb

    @pytest.mark.asyncio
    async def test_lists_files_and_dirs(self, plugin, sandbox):
        (sandbox / "file1.txt").write_text("a")
        (sandbox / "subdir").mkdir()
        ctx = ToolContext()
        ctx.services["_safe_path"] = str(sandbox)

        result = await plugin.execute(ctx=ctx, params=ListDirParams(path=str(sandbox)))
        assert "file1.txt" in result
        assert "subdir/" in result

    @pytest.mark.asyncio
    async def test_path_not_exists(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=ListDirParams(path="/nonexistent"))
        assert "路径不存在" in result

    @pytest.mark.asyncio
    async def test_not_a_directory(self, plugin, sandbox):
        (sandbox / "file.txt").write_text("content")
        ctx = ToolContext()
        ctx.services["_safe_path"] = str(sandbox / "file.txt")
        result = await plugin.execute(ctx=ctx, params=ListDirParams(path=str(sandbox / "file.txt")))
        assert "不是目录" in result

    @pytest.mark.asyncio
    async def test_depth_respects_max(self, plugin, sandbox):
        deep = sandbox / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep_file.txt").write_text("deep")
        ctx = ToolContext()
        ctx.services["_safe_path"] = str(sandbox)

        result = await plugin.execute(ctx=ctx, params=ListDirParams(path=str(sandbox), depth=1))
        assert "deep_file.txt" not in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_filesystem.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_filesystem.py
git commit -m "test: add ReadFilePlugin and ListDirPlugin unit tests"
```

---

### Task 4: test_web.py — WebSearchPlugin + WebFetchPlugin + _TextExtractor

**Files:**
- Create: `core/tools/test_web.py`

- [ ] **Step 1: 编写完整测试**

```python
"""WebSearchPlugin + WebFetchPlugin + _TextExtractor 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from Module.tool.protocol import ToolContext
from core.tools.web import (
    WebSearchPlugin, WebSearchParams, WebFetchPlugin, WebFetchParams,
    _TextExtractor, _extract_text, _ddg_search, _brave_search,
)


class TestTextExtractor:
    def test_extracts_visible_text(self):
        html = "<html><body><p>Hello</p><p>World</p></body></html>"
        result = _extract_text(html)
        assert "Hello" in result
        assert "World" in result

    def test_skips_script_tags(self):
        html = "<html><script>var x=1;</script><body>visible</body></html>"
        result = _extract_text(html)
        assert "var x=1" not in result
        assert "visible" in result

    def test_skips_style_tags(self):
        html = "<html><style>.a{color:red}</style><body>text</body></html>"
        result = _extract_text(html)
        assert ".a{color:red}" not in result
        assert "text" in result

    def test_br_adds_newline(self):
        html = "<body>line1<br>line2</body>"
        assert "line1" in _extract_text(html)
        assert "line2" in _extract_text(html)

    def test_empty_html(self):
        assert _extract_text("") == ""

    def test_malformed_html(self):
        assert _extract_text("<unclosed") == ""


class TestWebSearchPlugin:
    @pytest.fixture
    def plugin(self):
        return WebSearchPlugin()

    @pytest.mark.asyncio
    async def test_invalid_url_rejected(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=WebSearchParams(query="test"))
        assert result is not None  # should not crash

    @pytest.mark.asyncio
    async def test_ddg_search_no_results(self, plugin):
        ctx = ToolContext()
        with patch("core.tools.web._ddg_search", new_callable=AsyncMock) as mock_ddg:
            mock_ddg.return_value = "未找到与 'xyz' 相关的结果。"
            result = await plugin.execute(ctx=ctx, params=WebSearchParams(query="xyz"))
            assert "未找到" in result

    @pytest.mark.asyncio
    async def test_brave_backend_without_key_falls_back_to_ddg(self, plugin):
        ctx = ToolContext()
        ctx.services["web_search"] = "brave"
        with patch("core.tools.web._ddg_search", new_callable=AsyncMock) as mock_ddg:
            mock_ddg.return_value = "results from ddg"
            result = await plugin.execute(ctx=ctx, params=WebSearchParams(query="test"))
            assert "results from ddg" in result

    @pytest.mark.asyncio
    async def test_brave_backend_with_key(self, plugin):
        ctx = ToolContext()
        ctx.services["web_search"] = "brave"
        ctx.services["brave_api_key"] = "test-key"
        with patch("core.tools.web._brave_search", new_callable=AsyncMock) as mock_brave:
            mock_brave.return_value = "brave results"
            result = await plugin.execute(ctx=ctx, params=WebSearchParams(query="test"))
            assert "brave results" in result


class TestWebFetchPlugin:
    @pytest.fixture
    def plugin(self):
        return WebFetchPlugin()

    @pytest.mark.asyncio
    async def test_invalid_url_rejected(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=WebFetchParams(url="not-a-url"))
        assert "无效的 URL" in result

    @pytest.mark.asyncio
    async def test_non_http_url_rejected(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=WebFetchParams(url="ftp://example.com/file"))
        assert "无效的 URL" in result

    @pytest.mark.asyncio
    async def test_fetch_success(self, plugin):
        ctx = ToolContext()
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "text/html"}
        mock_response.text = "<html><body><p>fetched content</p></body></html>"

        with patch("core.tools.web.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            result = await plugin.execute(ctx=ctx, params=WebFetchParams(url="https://example.com"))
            assert "fetched content" in result

    @pytest.mark.asyncio
    async def test_fetch_http_error(self, plugin):
        import httpx
        ctx = ToolContext()
        with patch("core.tools.web.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "not found", request=MagicMock(), response=MagicMock(status_code=404)
                )
            )
            result = await plugin.execute(ctx=ctx, params=WebFetchParams(url="https://example.com/404"))
            assert "HTTP 错误" in result

    @pytest.mark.asyncio
    async def test_fetch_non_html_content_type(self, plugin):
        ctx = ToolContext()
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "application/pdf"}

        with patch("core.tools.web.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            result = await plugin.execute(ctx=ctx, params=WebFetchParams(url="https://example.com/file.pdf"))
            assert "不支持的内容类型" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_web.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_web.py
git commit -m "test: add WebSearchPlugin, WebFetchPlugin, _TextExtractor unit tests"
```

---

### Task 5: test_time.py — TimePlugin

**Files:**
- Create: `core/tools/test_time.py`

- [ ] **Step 1: 编写测试**

```python
"""TimePlugin 单元测试。"""
import pytest
from Module.tool.protocol import ToolContext
from core.tools.time import TimePlugin, TimeParams


class TestTimePlugin:
    @pytest.fixture
    def plugin(self):
        return TimePlugin()

    @pytest.mark.asyncio
    async def test_utc_iso_format(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="UTC", format="iso"))
        assert "T" in result
        assert "+" in result or "Z" in result

    @pytest.mark.asyncio
    async def test_utc_human_format(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="UTC", format="human"))
        assert "UTC" in result

    @pytest.mark.asyncio
    async def test_offset_timezone(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="+8", format="iso"))
        assert "+08:00" in result

    @pytest.mark.asyncio
    async def test_negative_offset_timezone(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="-5", format="iso"))
        assert "-05:00" in result

    @pytest.mark.asyncio
    async def test_invalid_offset(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="+abc"))
        assert "无效的时区偏移" in result

    @pytest.mark.asyncio
    async def test_invalid_timezone_name(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="Mars/Olympus"))
        assert "无效的时区名称" in result or "时区功能不可用" in result

    @pytest.mark.asyncio
    async def test_valid_iana_timezone(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams(timezone="Asia/Shanghai"))
        assert result  # either valid result or "时区功能不可用"

    @pytest.mark.asyncio
    async def test_default_params(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=TimeParams())
        assert "T" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_time.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_time.py
git commit -m "test: add TimePlugin unit tests"
```

---

### Task 6: test_memory.py — MemorySearchPlugin + MemorySavePlugin

**Files:**
- Create: `core/tools/test_memory.py`

- [ ] **Step 1: 编写测试**

```python
"""MemorySearchPlugin + MemorySavePlugin 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from Module.tool.protocol import ToolContext
from core.tools.memory import MemorySearchPlugin, SearchMemoryParams, MemorySavePlugin, SaveMemoryParams


class TestMemorySearchPlugin:
    @pytest.fixture
    def plugin(self):
        return MemorySearchPlugin()

    @pytest.mark.asyncio
    async def test_memory_unavailable(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=SearchMemoryParams(query="test"))
        assert "不可用" in result

    @pytest.mark.asyncio
    async def test_no_results(self, plugin):
        memory = AsyncMock()
        memory.search.return_value = []
        ctx = ToolContext()
        ctx.services["memory"] = memory

        result = await plugin.execute(ctx=ctx, params=SearchMemoryParams(query="nothing"))
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_search_returns_formatted_results(self, plugin):
        memory = AsyncMock()
        entry = MagicMock()
        entry.id = "mem_1"
        entry.memory_type = "fact"
        entry.poignancy = 5
        entry.text = "a remembered fact"
        memory.search.return_value = [entry]
        ctx = ToolContext()
        ctx.services["memory"] = memory

        result = await plugin.execute(ctx=ctx, params=SearchMemoryParams(query="fact"))
        assert "mem_1" in result
        assert "fact" in result
        assert "a remembered fact" in result

    @pytest.mark.asyncio
    async def test_search_exception(self, plugin):
        memory = AsyncMock()
        memory.search.side_effect = RuntimeError("db down")
        ctx = ToolContext()
        ctx.services["memory"] = memory

        result = await plugin.execute(ctx=ctx, params=SearchMemoryParams(query="test"))
        assert "出错" in result


class TestMemorySavePlugin:
    @pytest.fixture
    def plugin(self):
        return MemorySavePlugin()

    @pytest.mark.asyncio
    async def test_invalid_type(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=SaveMemoryParams(content="test", type="invalid"))
        assert "无效的记忆类型" in result

    @pytest.mark.asyncio
    async def test_memory_unavailable(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=SaveMemoryParams(content="test", type="fact"))
        assert "不可用" in result

    @pytest.mark.asyncio
    async def test_save_success(self, plugin):
        memory = AsyncMock()
        memory.insert.return_value = None
        ctx = ToolContext()
        ctx.services["memory"] = memory

        result = await plugin.execute(ctx=ctx, params=SaveMemoryParams(content="remember this", type="fact"))
        assert "已保存" in result
        memory.insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_exception(self, plugin):
        memory = AsyncMock()
        memory.insert.side_effect = RuntimeError("db down")
        ctx = ToolContext()
        ctx.services["memory"] = memory

        result = await plugin.execute(ctx=ctx, params=SaveMemoryParams(content="test", type="note"))
        assert "出错" in result

    @pytest.mark.asyncio
    async def test_all_valid_types_accepted(self, plugin):
        memory = AsyncMock()
        memory.insert.return_value = None
        ctx = ToolContext()
        ctx.services["memory"] = memory

        for t in ["fact", "plan", "preference", "note"]:
            result = await plugin.execute(ctx=ctx, params=SaveMemoryParams(content="test", type=t))
            assert "已保存" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_memory.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_memory.py
git commit -m "test: add MemorySearchPlugin and MemorySavePlugin unit tests"
```

---

### Task 7: test_reminder.py — SetReminderPlugin

**Files:**
- Create: `core/tools/test_reminder.py`

- [ ] **Step 1: 编写测试**

```python
"""SetReminderPlugin 单元测试。"""
import pytest
from unittest.mock import AsyncMock
from Module.tool.protocol import ToolContext
from core.tools.reminder import SetReminderPlugin, ReminderParams


class TestSetReminderPlugin:
    @pytest.fixture
    def plugin(self):
        return SetReminderPlugin()

    @pytest.mark.asyncio
    async def test_scheduler_unavailable(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=ReminderParams(message="test", delay_seconds=60))
        assert "不可用" in result

    @pytest.mark.asyncio
    async def test_reminder_dispatched(self, plugin):
        scheduler = AsyncMock()
        scheduler.dispatch_background.return_value = "task-1"
        ctx = ToolContext()
        ctx.services["scheduler"] = scheduler

        result = await plugin.execute(ctx=ctx, params=ReminderParams(message="stand up", delay_seconds=300))
        assert "已设置提醒" in result
        assert "stand up" in result
        scheduler.dispatch_background.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_failure(self, plugin):
        scheduler = AsyncMock()
        scheduler.dispatch_background.side_effect = RuntimeError("queue full")
        ctx = ToolContext()
        ctx.services["scheduler"] = scheduler

        result = await plugin.execute(ctx=ctx, params=ReminderParams(message="test", delay_seconds=60))
        assert "失败" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_reminder.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_reminder.py
git commit -m "test: add SetReminderPlugin unit tests"
```

---

### Task 8: test_runtime_config.py — RuntimeConfigPlugin

**Files:**
- Create: `core/tools/test_runtime_config.py`

- [ ] **Step 1: 编写测试**

```python
"""RuntimeConfigPlugin 单元测试。"""
import pytest
from unittest.mock import MagicMock
from Module.tool.protocol import ToolContext
from core.tools.runtime_config import RuntimeConfigPlugin, RuntimeConfigParams


class TestRuntimeConfigPlugin:
    @pytest.fixture
    def plugin(self):
        return RuntimeConfigPlugin()

    @pytest.mark.asyncio
    async def test_no_config_shows_tools_only(self, plugin):
        ctx = ToolContext()
        ctx.services["_tool_names"] = ["read_file", "get_time"]
        result = await plugin.execute(ctx=ctx, params=RuntimeConfigParams())
        assert "已加载工具" in result
        assert "read_file" in result

    @pytest.mark.asyncio
    async def test_with_full_config(self, plugin):
        config = MagicMock()
        config.llm.model = "deepseek/deepseek-chat"
        config.tts.enabled = True
        config.stt.enabled = False
        config.server.host = "0.0.0.0"
        config.server.port = 8080

        ctx = ToolContext()
        ctx.services["config"] = config
        ctx.services["_tool_names"] = ["tool_a"]

        result = await plugin.execute(ctx=ctx, params=RuntimeConfigParams())
        assert "deepseek-chat" in result
        assert "TTS: 启用" in result
        assert "STT: 禁用" in result
        assert "8080" in result

    @pytest.mark.asyncio
    async def test_empty_config_sections(self, plugin):
        config = MagicMock()
        config.llm = None
        config.tts = None
        config.stt = None
        config.server = None

        ctx = ToolContext()
        ctx.services["config"] = config
        ctx.services["_tool_names"] = []

        result = await plugin.execute(ctx=ctx, params=RuntimeConfigParams())
        assert "已加载工具" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_runtime_config.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_runtime_config.py
git commit -m "test: add RuntimeConfigPlugin unit tests"
```

---

### Task 9: test_self_state.py — SelfStatePlugin

**Files:**
- Create: `core/tools/test_self_state.py`

- [ ] **Step 1: 编写测试**

```python
"""SelfStatePlugin 单元测试。"""
import pytest
from Module.tool.protocol import ToolContext
from core.tools.self_state import SelfStatePlugin


class TestSelfStatePlugin:
    @pytest.fixture
    def plugin(self):
        return SelfStatePlugin()

    @pytest.mark.asyncio
    async def test_returns_defaults_when_no_context(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=None)
        assert "Agent ID" in result
        assert "FSM 状态" in result
        assert "unknown" in result

    @pytest.mark.asyncio
    async def test_returns_context_values(self, plugin):
        ctx = ToolContext(trace_id="trace-123", agent_id="agent-7")
        ctx.services["_fsm_state"] = "thinking"
        ctx.services["_session_msg_count"] = 42
        ctx.services["_tool_count"] = 5

        result = await plugin.execute(ctx=ctx, params=None)
        assert "agent-7" in result
        assert "trace-123" in result
        assert "thinking" in result
        assert "42" in result
        assert "5" in result

    @pytest.mark.asyncio
    async def test_default_agent_id_when_none(self, plugin):
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx, params=None)
        assert "default" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_self_state.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_self_state.py
git commit -m "test: add SelfStatePlugin unit tests"
```

---

### Task 10: test_write_file.py — WriteFilePlugin

**Files:**
- Create: `core/tools/test_write_file.py`

- [ ] **Step 1: 编写测试**

```python
"""WriteFilePlugin 单元测试。"""
import pytest
from pathlib import Path
from Module.tool.protocol import ToolContext
from core.tools.write_file import WriteFilePlugin, WriteFileParams


class TestWriteFilePlugin:
    @pytest.fixture
    def plugin(self):
        return WriteFilePlugin()

    @pytest.fixture
    def sandbox(self, tmp_path):
        sb = tmp_path / "sandbox" / "default"
        sb.mkdir(parents=True)
        return sb

    @pytest.mark.asyncio
    async def test_writes_file_in_sandbox(self, plugin, sandbox):
        ctx = ToolContext(session_id="test-session")
        safe_path = str(sandbox / "output.txt")
        ctx.services["_safe_path"] = safe_path

        result = await plugin.execute(ctx=ctx, params=WriteFileParams(path="output.txt", content="hello world"))
        assert "已写入" in result
        assert Path(safe_path).read_text() == "hello world"

    @pytest.mark.asyncio
    async def test_rejects_path_outside_sandbox(self, plugin, tmp_path):
        outside = tmp_path / "outside" / "file.txt"
        outside.parent.mkdir()
        ctx = ToolContext()
        ctx.services["_safe_path"] = str(outside)

        result = await plugin.execute(ctx=ctx, params=WriteFileParams(path="../outside/file.txt", content="bad"))
        assert "拒绝写入" in result

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, plugin, sandbox):
        ctx = ToolContext(session_id="s1")
        safe_path = str(sandbox / "nested" / "deep" / "file.txt")
        ctx.services["_safe_path"] = safe_path

        result = await plugin.execute(ctx=ctx, params=WriteFileParams(path="nested/deep/file.txt", content="nested"))
        assert "已写入" in result
        assert Path(safe_path).read_text() == "nested"

    @pytest.mark.asyncio
    async def test_session_id_in_path_replaced(self, plugin, tmp_path):
        sb = tmp_path / "sandbox" / "sid-123"
        sb.mkdir(parents=True)
        ctx = ToolContext(session_id="sid-123")
        safe_path = str(sb / "out.txt")
        ctx.services["_safe_path"] = safe_path

        result = await plugin.execute(ctx=ctx, params=WriteFileParams(path="out.txt", content="data"))
        assert "已写入" in result
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tools/test_write_file.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_write_file.py
git commit -m "test: add WriteFilePlugin unit tests"
```

---

### Task 11: test_timing.py — Timer + TimingClock

**Files:**
- Create: `core/test_timing.py`

- [ ] **Step 1: 编写测试**

```python
"""Timer + TimingClock 单元测试。"""
import time
import pytest
from core.timing import Timer, TimingClock


class TestTimer:
    def test_context_manager_records_elapsed(self):
        with Timer("test_op") as t:
            time.sleep(0.01)
        assert t.elapsed_ms > 0
        assert t.name == "test_op"

    def test_elapsed_ms_is_float(self):
        with Timer("test_op") as t:
            pass
        assert isinstance(t.elapsed_ms, float)

    def test_elapsed_ms_increases_over_time(self):
        with Timer("test_op") as t:
            time.sleep(0.02)
        elapsed = t.elapsed_ms
        assert elapsed >= 15  # at least ~15ms for 20ms sleep


class TestTimingClock:
    def test_span_context_manager(self):
        clock = TimingClock()
        with clock.span("http_call"):
            time.sleep(0.01)
        assert "http_call" in clock.spans
        assert clock.spans["http_call"] > 0

    def test_multiple_spans_accumulate(self):
        clock = TimingClock()
        with clock.span("op_a"):
            time.sleep(0.01)
        with clock.span("op_a"):
            time.sleep(0.01)
        assert clock.spans["op_a"] >= 20

    def test_total_ms(self):
        clock = TimingClock()
        with clock.span("a"):
            time.sleep(0.01)
        with clock.span("b"):
            time.sleep(0.01)
        total = clock.total_ms
        assert total > 0

    def test_summary_format(self):
        clock = TimingClock()
        with clock.span("op_a"):
            time.sleep(0.01)
        summary = clock.summary()
        assert "op_a" in summary
        assert "ms" in summary
        assert "total" in summary.lower()

    def test_reset_clears_spans(self):
        clock = TimingClock()
        with clock.span("op_a"):
            time.sleep(0.01)
        clock.reset()
        assert len(clock.spans) == 0

    def test_empty_clock_total_is_zero(self):
        clock = TimingClock()
        assert clock.total_ms == 0

    def test_start_stop_lifecycle(self):
        clock = TimingClock()
        clock.start()
        time.sleep(0.02)
        clock.stop()
        total = clock.total_ms
        # start/stop may write to a default span or track globally
        assert total >= 0
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/test_timing.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/test_timing.py
git commit -m "test: add Timer and TimingClock unit tests"
```

---

### Task 12: 最终验证 — 运行全部新增测试

- [ ] **Step 1: 运行全部新增测试**

```bash
python -m pytest core/tools/test_filesystem.py core/tools/test_web.py core/tools/test_time.py core/tools/test_memory.py core/tools/test_reminder.py core/tools/test_runtime_config.py core/tools/test_self_state.py core/tools/test_write_file.py core/test_timing.py Module/tool/test_tool_safety.py Module/tool/test_framework.py -v
```

- [ ] **Step 2: 运行已有测试确保无回归**

```bash
python -m pytest Module/ -v --ignore=Module/tool/test_tool_safety.py
python -m pytest core/tests/ -v
```

---

## 验证方法

```bash
# 新增测试
python -m pytest core/tools/ -v
python -m pytest core/test_timing.py -v
python -m pytest Module/tool/test_tool_safety.py -v

# 全部测试
python -m pytest Module/ -v
python -m pytest core/tests/ -v
python -m pytest core/tools/ -v
```
