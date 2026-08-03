# Plugin Directory Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 core/tools/ 下平铺的 8 个 .py 插件文件重构为 7 个独立目录，每个目录包含 plugin.py、config.yaml、README.md、__init__.py；扩展 ToolManager.load_all() 从各插件的 config.yaml 加载 SafetyProfile 覆盖。

**Architecture:** 插件代码原地搬家（不改 execute 逻辑），ToolManager.load_all() 新增 `yaml.safe_load` 步骤读取插件所在目录的 config.yaml，用同名 key 覆盖 instance.safety 的字段值。每个插件目录的 __init__.py 重导出 plugin class。

**Tech Stack:** Python 3.13, PyYAML (yaml.safe_load), existing Module/tool 体系。

---

## File Structure

| 新文件 | 职责 |
|--------|------|
| `core/tools/filesystem/plugin.py` | ReadFilePlugin + ListDirPlugin + WriteFilePlugin（合并自 filesystem.py + write_file.py） |
| `core/tools/filesystem/config.yaml` | read_dirs, write_dir, max_file_bytes, allow_symlinks, max_dir_depth |
| `core/tools/filesystem/__init__.py` | 重导出三个 plugin class |
| `core/tools/filesystem/README.md` | 文件系统插件说明 |
| `core/tools/web/plugin.py` | WebSearchPlugin + WebFetchPlugin + _TextExtractor + _extract_text（搬自 web.py） |
| `core/tools/web/config.yaml` | block_private_ips, max_redirects, max_response_bytes, request_timeout, max_concurrency |
| `core/tools/web/__init__.py` | 重导出两个 plugin class |
| `core/tools/web/README.md` | 网络插件说明 |
| `core/tools/memory/plugin.py` | MemorySearchPlugin + MemorySavePlugin（搬自 memory.py） |
| `core/tools/memory/config.yaml` | database_allowed, database_readonly, max_result_tokens |
| `core/tools/memory/__init__.py` | 重导出两个 plugin class |
| `core/tools/memory/README.md` | 记忆插件说明 |
| `core/tools/time/plugin.py` | TimePlugin（搬自 time.py） |
| `core/tools/time/config.yaml` | 空 |
| `core/tools/time/__init__.py` | 重导出 TimePlugin |
| `core/tools/time/README.md` | 时间插件说明 |
| `core/tools/reminder/plugin.py` | SetReminderPlugin（搬自 reminder.py） |
| `core/tools/reminder/config.yaml` | 空 |
| `core/tools/reminder/__init__.py` | 重导出 SetReminderPlugin |
| `core/tools/reminder/README.md` | 提醒插件说明 |
| `core/tools/runtime_config/plugin.py` | RuntimeConfigPlugin（搬自 runtime_config.py） |
| `core/tools/runtime_config/config.yaml` | 空 |
| `core/tools/runtime_config/__init__.py` | 重导出 RuntimeConfigPlugin |
| `core/tools/runtime_config/README.md` | 运行时配置插件说明 |
| `core/tools/self_state/plugin.py` | SelfStatePlugin（搬自 self_state.py） |
| `core/tools/self_state/config.yaml` | 空 |
| `core/tools/self_state/__init__.py` | 重导出 SelfStatePlugin |
| `core/tools/self_state/README.md` | 自身状态插件说明 |

| 修改文件 | 改动 |
|----------|------|
| `Module/tool/manager.py` | `load_all()` 新增 yaml.safe_load 步骤 |
| `core/tools/__init__.py` | 更新导入路径（8 条 import） |
| `core/tests/test_phase3.py` | 更新导入路径 + read_dirs 断言值 |

| 删除文件 |
|----------|
| `core/tools/filesystem.py` |
| `core/tools/write_file.py` |
| `core/tools/web.py` |
| `core/tools/memory.py` |
| `core/tools/time.py` |
| `core/tools/reminder.py` |
| `core/tools/runtime_config.py` |
| `core/tools/self_state.py` |
| `core/tools/test_filesystem.py` |
| `core/tools/test_write_file.py` |
| `core/tools/test_web.py` |
| `core/tools/test_memory.py` |
| `core/tools/test_time.py` |
| `core/tools/test_reminder.py` |
| `core/tools/test_runtime_config.py` |
| `core/tools/test_self_state.py` |

---

### Task 1: 创建 filesystem 插件目录（合并 filesystem.py + write_file.py）

**Files:**
- Create: `core/tools/filesystem/__init__.py`
- Create: `core/tools/filesystem/plugin.py`
- Create: `core/tools/filesystem/config.yaml`
- Create: `core/tools/filesystem/README.md`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p /home/yomu/Agent/core/tools/filesystem
```

- [ ] **Step 2: 写 plugin.py（合并 filesystem.py + write_file.py）**

```python
"""文件系统工具插件 — 读取文件、列出目录、写入文件。"""

import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024*1024):.1f}M"
    else:
        return f"{size / (1024*1024*1024):.1f}G"


# ============================================================================
# read_file
# ============================================================================

class ReadFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对或绝对）")
    start_line: int = Field(default=0, ge=0, description="起始行号（0-indexed）")
    max_lines: int = Field(default=200, ge=1, le=500, description="最大读取行数")


class ReadFilePlugin(ToolPlugin):
    name = "read_file"
    description = "读取指定文件的内容（仅文本文件）"
    param_model = ReadFileParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["/home/yomu/data/sandbox"],
        max_file_bytes=10 * 1024 * 1024,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: ReadFileParams,
    ) -> str:
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        if not resolved.exists():
            return f"路径不存在: {path}"
        if not resolved.is_file():
            return f"不是文件: {path}"

        try:
            fsize = resolved.stat().st_size
            if fsize > self.safety.max_file_bytes:
                return f"文件过大 ({fsize} bytes)，最大允许 {self.safety.max_file_bytes} bytes"
        except OSError as e:
            return f"无法读取文件信息: {e}"

        try:
            with open(resolved, "r", encoding="utf-8") as f:
                probe = f.read(1024)
                f.seek(0)

                result_lines: list[str] = []
                i = 0
                for i, line in enumerate(f):
                    if i < params.start_line:
                        continue
                    if len(result_lines) >= params.max_lines:
                        break
                    result_lines.append(f"{i+1:4d} | {line.rstrip()}")
                total = i + 1
        except UnicodeDecodeError:
            return f"无法读取（可能是二进制文件）: {path}"
        except Exception as e:
            return f"读取失败: {e}"

        output = "\n".join(result_lines)
        if not output.strip():
            output = "（文件为空）"
            return output

        last_shown = params.start_line + len(result_lines)
        if last_shown < total:
            output += f"\n\n…[显示 {params.start_line+1}-{last_shown} 行，共 {total} 行]"

        return output


# ============================================================================
# list_directory
# ============================================================================

class ListDirParams(BaseModel):
    path: str = Field(default=".", description="目录路径")
    depth: int = Field(default=1, ge=1, le=3, description="遍历深度，1-3")


class ListDirPlugin(ToolPlugin):
    name = "list_directory"
    description = "列出目录中的文件和子目录"
    param_model = ListDirParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["/home/yomu/data/sandbox"],
        max_dir_depth=3,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: ListDirParams,
    ) -> str:
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        if not resolved.exists():
            return f"路径不存在: {path}"
        if not resolved.is_dir():
            return f"不是目录: {path}"

        depth = max(1, min(params.depth, self.safety.max_dir_depth))
        visited: set[int] = set()

        def _walk(p: Path, d: int) -> list[str]:
            lines: list[str] = []
            inode = p.stat().st_ino if not p.is_symlink() else None
            if inode and inode in visited:
                return [f"{'  ' * (depth - d)}[循环] {p}"]
            if inode:
                visited.add(inode)

            try:
                entries = sorted(
                    p.iterdir(),
                    key=lambda x: (not x.is_dir(), x.name.lower()),
                )
            except PermissionError:
                return [f"{'  ' * (depth - d)}[权限不足] {p}"]

            prefix = "  " * (depth - d)
            for entry in entries:
                try:
                    st = entry.stat()
                except OSError:
                    lines.append(f"{prefix}{entry.name}/ (?)")
                    continue

                mtime = time.strftime(
                    "%m-%d %H:%M", time.localtime(st.st_mtime),
                )
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/ ({mtime})")
                    if d > 1:
                        lines.extend(_walk(entry, d - 1))
                else:
                    size = _format_size(st.st_size)
                    lines.append(f"{prefix}{entry.name}  {size}  {mtime}")
            return lines

        lines = [f"目录: {resolved}\n"]
        lines.extend(_walk(resolved, depth))
        output = "\n".join(lines)
        if len(output) > 6000:
            output = output[:6000] + "\n\n…[输出已截断]"
        return output


# ============================================================================
# write_file
# ============================================================================

class WriteFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对路径，自动限定在 sandbox 目录下）")
    content: str = Field(..., description="要写入的内容")


class WriteFilePlugin(ToolPlugin):
    name = "write_file"
    description = "在安全目录下写入文件（需用户确认）"
    param_model = WriteFileParams
    capabilities = {Capability.FILESYSTEM_WRITE}
    safety = SafetyProfile(
        filesystem_allowed=True,
        write_dir="data/sandbox/{session_id}",
        max_file_bytes=10 * 1024 * 1024,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: WriteFileParams,
    ) -> str:
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        session_id = ctx.session_id or "default"
        safe_sid = session_id.replace("/", "_").replace("\\", "_")
        write_dir = Path(
            self.safety.write_dir.replace("{session_id}", safe_sid)
        ).resolve()

        if not str(resolved).startswith(str(write_dir)):
            return f"拒绝写入: 路径 '{params.path}' 越过了 sandbox 边界"

        resolved.parent.mkdir(parents=True, exist_ok=True)

        try:
            resolved.write_text(params.content, encoding="utf-8")
            size = resolved.stat().st_size
            return f"已写入 {resolved.name} ({size} bytes)"
        except Exception as e:
            return f"写入失败: {e}"
```

- [ ] **Step 3: 写 config.yaml**

```yaml
# 文件系统沙箱配置
read_dirs:
  - /home/yomu/data/sandbox
write_dir: data/sandbox/{session_id}
max_file_bytes: 10485760
allow_symlinks: false
max_dir_depth: 3
```

- [ ] **Step 4: 写 __init__.py**

```python
"""文件系统工具插件 — 读文件、列目录、写文件。"""

from .plugin import ReadFilePlugin, ListDirPlugin, WriteFilePlugin

__all__ = ["ReadFilePlugin", "ListDirPlugin", "WriteFilePlugin"]
```

- [ ] **Step 5: 写 README.md**

```markdown
# 文件系统工具插件

提供 Agent 访问本地文件系统的能力。

## 插件

- `read_file` — 读取文本文件内容（仅 UTF-8 文本）
- `list_directory` — 列出目录内容（最多 3 层深度）
- `write_file` — 在 sandbox 目录下写入文件（需用户确认）

## 配置 (config.yaml)

- `read_dirs` — 允许读取的目录白名单（绝对路径或相对于 CWD 的路径）
- `write_dir` — 写入目录模板，`{session_id}` 会替换为当前会话 ID
- `max_file_bytes` — 最大允许读取/写入的文件大小（默认 10MB）
- `allow_symlinks` — 是否允许跟随符号链接
- `max_dir_depth` — 最大目录遍历深度
```

- [ ] **Step 6: 验证**

```bash
python -c "from core.tools.filesystem.plugin import ReadFilePlugin; p = ReadFilePlugin(); print(p.name, p.safety.read_dirs)"
```
Expected: `read_file ['/home/yomu/data/sandbox']`

---

### Task 2: 创建 web 插件目录

**Files:**
- Create: `core/tools/web/__init__.py`
- Create: `core/tools/web/plugin.py`
- Create: `core/tools/web/config.yaml`
- Create: `core/tools/web/README.md`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p /home/yomu/Agent/core/tools/web
```

- [ ] **Step 2: 写 plugin.py（复制 web.py 全部内容，无改动）**

```python
"""网络工具插件 — 网页搜索和抓取。"""

import asyncio
import re
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, Field

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


# ============================================================================
# 文本提取器
# ============================================================================

class _TextExtractor(HTMLParser):
    """提取 HTML 中的可见文本，跳过 script/style/不可见元素。"""

    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "head", "meta", "link"}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False
        if tag in ("br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self.text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.text.append(text)


def _extract_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    raw = " ".join(parser.text)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    return raw.strip()


# ============================================================================
# web_search — DuckDuckGo (默认) + Brave API (可选)
# ============================================================================

class WebSearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=10, description="结果数量，最大10")


class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网，返回相关网页的标题、URL 和摘要"
    param_model = WebSearchParams
    capabilities = {Capability.NETWORK, Capability.WEB_SEARCH}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_concurrency=3,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: WebSearchParams,
    ) -> str:
        backend = ctx.get("web_search", "")
        if backend == "brave":
            brave_key = ctx.get("brave_api_key")
            if brave_key:
                return await _brave_search(params.query, params.limit, brave_key)

        return await _ddg_search(params.query, params.limit)


async def _ddg_search(query: str, limit: int) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    try:
        with DDGS() as ddgs:
            results = await asyncio.to_thread(
                lambda: list(ddgs.text(query, max_results=limit))
            )
    except Exception as e:
        return f"搜索失败: {e}"

    if not results:
        return f"未找到与 '{query}' 相关的结果。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        href = r.get("href", "")
        body = r.get("body", "")
        lines.append(f"{i}. {title}\n   {href}\n   {body}")
    return "\n\n".join(lines)


async def _brave_search(query: str, limit: int, api_key: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return f"Brave 搜索失败: {e}"

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return f"未找到与 '{query}' 相关的结果。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        desc = r.get("description", "")
        lines.append(f"{i}. {title}\n   {url}\n   {desc}")
    return "\n\n".join(lines)


# ============================================================================
# web_fetch — 抓取网页纯文本
# ============================================================================

class WebFetchParams(BaseModel):
    url: str = Field(..., description="要抓取的网页 URL")


class WebFetchPlugin(ToolPlugin):
    name = "web_fetch"
    description = "抓取并解析指定网页的文本内容"
    param_model = WebFetchParams
    capabilities = {Capability.NETWORK}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_redirects=5,
        max_response_bytes=5 * 1024 * 1024,
        request_timeout=30.0,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: WebFetchParams,
    ) -> str:
        url = params.url
        if not url.startswith(("http://", "https://")):
            return f"无效的 URL: {url}"

        try:
            async with httpx.AsyncClient(
                timeout=self.safety.request_timeout,
                follow_redirects=True,
                max_redirects=self.safety.max_redirects,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Elysia-Agent/1.0)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return f"不支持的内容类型: {content_type}"

                text = _extract_text(resp.text)
                if not text:
                    return "（页面无文本内容）"
                return text
        except httpx.HTTPStatusError as e:
            return f"HTTP 错误: {e.response.status_code}"
        except Exception as e:
            return f"抓取失败: {e}"
```

- [ ] **Step 3: 写 config.yaml**

```yaml
# 网络工具配置
block_private_ips: true
max_redirects: 5
max_response_bytes: 5242880
request_timeout: 30.0
max_concurrency: 3
```

- [ ] **Step 4: 写 __init__.py**

```python
"""网络工具插件 — 搜索和网页抓取。"""

from .plugin import WebSearchPlugin, WebFetchPlugin

__all__ = ["WebSearchPlugin", "WebFetchPlugin"]
```

- [ ] **Step 5: 写 README.md**

```markdown
# 网络工具插件

提供 Agent 访问互联网的能力。

## 插件

- `web_search` — DuckDuckGo / Brave 搜索
- `web_fetch` — 抓取网页纯文本内容

## 配置 (config.yaml)

- `block_private_ips` — 是否阻止访问内网 IP
- `max_redirects` — HTTP 重定向最大次数
- `max_response_bytes` — 最大响应大小
- `request_timeout` — 请求超时时间（秒）
- `max_concurrency` — 最大并发请求数
```

- [ ] **Step 6: 验证**

```bash
python -c "from core.tools.web.plugin import WebSearchPlugin; print(WebSearchPlugin().name)"
```
Expected: `web_search`

---

### Task 3: 创建 memory 插件目录

**Files:**
- Create: `core/tools/memory/__init__.py`
- Create: `core/tools/memory/plugin.py`
- Create: `core/tools/memory/config.yaml`
- Create: `core/tools/memory/README.md`

- [ ] **Step 1: 创建目录并写文件**

```bash
mkdir -p /home/yomu/Agent/core/tools/memory
```

- [ ] **Step 2: 写 plugin.py（复制 memory.py 全部内容，无改动）**

```python
"""记忆工具插件 — 搜索和保存长期记忆。"""

import logging
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile

logger = logging.getLogger(__name__)


class SearchMemoryParams(BaseModel):
    query: str = Field(..., description="搜索关键词或语义描述")
    limit: int = Field(default=5, ge=1, le=20, description="返回结果数量，最大20")


class MemorySearchPlugin(ToolPlugin):
    name = "search_memory"
    description = "在长期记忆中搜索相关信息，比如计划、偏好、历史对话等"
    param_model = SearchMemoryParams
    capabilities = {Capability.DATABASE_READ}
    safety = SafetyProfile(database_allowed=True, database_readonly=True)

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: SearchMemoryParams,
    ) -> str:
        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        try:
            results = await memory.search(
                query_text=params.query, top_k=min(params.limit, 20),
            )
        except Exception as e:
            logger.warning("search_memory 失败: %s", e)
            return f"搜索记忆时出错: {e}"

        if not results:
            return "未找到相关记忆。"

        lines = []
        for r in results:
            entry_id = getattr(r, "id", "?")
            entry_type = getattr(r, "memory_type", "unknown")
            poignancy = getattr(r, "poignancy", 0)
            text = getattr(r, "text", str(r))
            lines.append(
                f"[ID:{entry_id}] (类型:{entry_type}, 重要性:{poignancy}) {text}"
            )
        return "\n".join(lines)


class SaveMemoryParams(BaseModel):
    content: str = Field(..., description="要保存的记忆内容")
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), plan(计划), preference(偏好), note(笔记)",
    )


class MemorySavePlugin(ToolPlugin):
    name = "save_memory"
    description = "将重要信息保存到长期记忆库以供未来检索"
    param_model = SaveMemoryParams
    capabilities = {Capability.DATABASE_WRITE}
    safety = SafetyProfile(
        database_allowed=True,
        database_readonly=False,
        requires_human_approval=True,
    )

    _VALID_TYPES = {"fact", "plan", "preference", "note"}

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: SaveMemoryParams,
    ) -> str:
        if params.type not in self._VALID_TYPES:
            return f"无效的记忆类型 '{params.type}'，允许: {', '.join(sorted(self._VALID_TYPES))}"

        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        content = params.content
        try:
            await memory.insert(content, type=params.type)
        except Exception as e:
            logger.warning("save_memory 失败: %s", e)
            return f"保存记忆时出错: {e}"

        return f"已保存 {params.type} 类型记忆: {content[:200]}"
```

- [ ] **Step 3: 写 config.yaml**

```yaml
# 记忆工具配置
database_allowed: true
database_readonly: true
max_result_tokens: 2000
```

> 注意: `database_readonly: true` 是全局默认值，对 MemorySavePlugin 会被其代码中的 `database_readonly=False` 覆盖。config.yaml 值按字段逐项覆盖，未列出的字段保留类定义值。

- [ ] **Step 4: 写 __init__.py**

```python
"""记忆工具插件 — 搜索和保存长期记忆。"""

from .plugin import MemorySearchPlugin, MemorySavePlugin

__all__ = ["MemorySearchPlugin", "MemorySavePlugin"]
```

- [ ] **Step 5: 写 README.md**

```markdown
# 记忆工具插件

提供 Agent 长期记忆能力。

## 插件

- `search_memory` — 语义搜索长期记忆
- `save_memory` — 保存信息到长期记忆（需用户确认）

## 配置 (config.yaml)

- `database_allowed` — 是否允许数据库访问
- `database_readonly` — 数据库操作是否只读
- `max_result_tokens` — 最大返回 token 数
```

---

### Task 4: 创建 time 插件目录

**Files:**
- Create: `core/tools/time/__init__.py`
- Create: `core/tools/time/plugin.py`
- Create: `core/tools/time/config.yaml`
- Create: `core/tools/time/README.md`

- [ ] **Step 1: 创建目录并写文件**

```bash
mkdir -p /home/yomu/Agent/core/tools/time
```

- [ ] **Step 2: 写 plugin.py（复制 time.py 全部内容，无改动）**

```python
"""时间工具插件 — 获取当前时间（支持时区和格式）。"""

from datetime import datetime, timezone, timedelta

try:
    import zoneinfo as _zoneinfo
    HAS_ZONEINFO = True
except ImportError:
    HAS_ZONEINFO = False

from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.safety import SafetyProfile


class TimeParams(BaseModel):
    timezone: str = Field(
        default="UTC",
        description="时区，如 'UTC', 'Asia/Shanghai', 'US/Eastern'，或 '+8' 表示 UTC+8",
    )
    format: str = Field(
        default="iso",
        description="输出格式: 'iso' (ISO 8601) 或 'human' (人类可读)",
    )


class TimePlugin(ToolPlugin):
    name = "get_time"
    description = "获取当前日期和时间，支持指定时区和格式"
    param_model = TimeParams
    capabilities: set = set()
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: TimeParams,
    ) -> str:
        tz_str = params.timezone.strip()
        tz: timezone | None = None
        if tz_str.startswith(("+", "-")):
            try:
                hours = int(tz_str)
                tz = timezone(timedelta(hours=hours))
            except ValueError:
                return f"无效的时区偏移: {tz_str}，请使用如 '+8' 或 'UTC'"
        elif tz_str == "UTC":
            tz = timezone.utc
        else:
            if not HAS_ZONEINFO:
                return "时区功能不可用（缺少 zoneinfo 模块）"
            try:
                tz = _zoneinfo.ZoneInfo(tz_str)
            except Exception:
                return f"无效的时区名称: '{tz_str}'，请使用 'UTC'、'Asia/Shanghai' 或 '+8'"

        now = datetime.now(tz)

        if params.format == "human":
            return now.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            return now.isoformat()
```

- [ ] **Step 3: 写 config.yaml**

```yaml
# 时间插件无特殊安全配置
```

- [ ] **Step 4: 写 __init__.py**

```python
"""时间工具插件。"""

from .plugin import TimePlugin

__all__ = ["TimePlugin"]
```

- [ ] **Step 5: 写 README.md**

```markdown
# 时间工具插件

获取当前日期和时间，支持多时区和格式。

## 插件

- `get_time` — 获取当前时间

## 配置

无需特殊配置，使用 SafetyProfile 默认值。
```

---

### Task 5: 创建 reminder、runtime_config、self_state 插件目录

**Files:**
- Create: `core/tools/reminder/plugin.py`
- Create: `core/tools/reminder/config.yaml`
- Create: `core/tools/reminder/__init__.py`
- Create: `core/tools/reminder/README.md`
- Create: `core/tools/runtime_config/plugin.py`
- Create: `core/tools/runtime_config/config.yaml`
- Create: `core/tools/runtime_config/__init__.py`
- Create: `core/tools/runtime_config/README.md`
- Create: `core/tools/self_state/plugin.py`
- Create: `core/tools/self_state/config.yaml`
- Create: `core/tools/self_state/__init__.py`
- Create: `core/tools/self_state/README.md`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p /home/yomu/Agent/core/tools/reminder
mkdir -p /home/yomu/Agent/core/tools/runtime_config
mkdir -p /home/yomu/Agent/core/tools/self_state
```

- [ ] **Step 2: 写 reminder/plugin.py（复制 reminder.py 全部内容）**

```python
"""提醒工具插件 — 设置定时提醒。"""

from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


class ReminderParams(BaseModel):
    message: str = Field(..., description="提醒内容")
    delay_seconds: int = Field(
        ..., ge=1, le=86400,
        description="延迟秒数，最大86400（24小时）",
    )


class SetReminderPlugin(ToolPlugin):
    name = "set_reminder"
    description = "设置定时提醒，在指定秒数后通知用户"
    param_model = ReminderParams
    capabilities = {Capability.SCHEDULE}
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: ReminderParams,
    ) -> str:
        scheduler = ctx.get("scheduler")
        if scheduler is None:
            return "提醒功能当前不可用（调度器未加载）。"

        async def _remind():
            import asyncio
            await asyncio.sleep(params.delay_seconds)
            return f"⏰ 提醒: {params.message}"

        try:
            tid = await scheduler.dispatch_background(_remind())
            return f"已设置提醒，{params.delay_seconds} 秒后通知: {params.message}"
        except Exception as e:
            return f"设置提醒失败: {e}"
```

- [ ] **Step 3: 写 runtime_config/plugin.py（复制 runtime_config.py 全部内容）**

```python
"""运行时配置工具插件 — 查看 agent 配置和状态。"""

import asyncio
import os
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class RuntimeConfigParams(BaseModel):
    query: str | None = Field(
        default=None,
        description="可选：查询特定项，如 'model', 'tts', 'memory'。留空返回全部。",
    )


class RuntimeConfigPlugin(ToolPlugin):
    name = "get_runtime_config"
    description = "查看当前运行时配置：模型名、TTS/STT 状态、内存使用、已加载工具"
    param_model = RuntimeConfigParams
    capabilities = {Capability.PROCESS}
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: RuntimeConfigParams,
    ) -> str:
        config = ctx.get("config")
        tools = ctx.get("_tool_names", [])
        lines = []

        if config is not None:
            llm_cfg = getattr(config, "llm", None)
            tts_cfg = getattr(config, "tts", None)
            stt_cfg = getattr(config, "stt", None)
            server_cfg = getattr(config, "server", None)

            if llm_cfg:
                lines.append(f"LLM 模型: {getattr(llm_cfg, 'model', 'unknown')}")
            if tts_cfg:
                lines.append(
                    f"TTS: {'启用' if getattr(tts_cfg, 'enabled', False) else '禁用'}"
                )
            if stt_cfg:
                lines.append(
                    f"STT: {'启用' if getattr(stt_cfg, 'enabled', False) else '禁用'}"
                )
            if server_cfg:
                lines.append(
                    f"服务: {getattr(server_cfg, 'host', '?')}:{getattr(server_cfg, 'port', '?')}"
                )

        lines.append(
            f"已加载工具 ({len(tools)}): {', '.join(tools[:20])}"
        )

        if HAS_PSUTIL:
            def _get_memory():
                proc = psutil.Process()
                mem = proc.memory_info()
                return mem, proc.num_threads()
            mem, threads = await asyncio.to_thread(_get_memory)
            lines.append(
                f"进程内存: RSS={mem.rss / 1024 / 1024:.1f}MB, "
                f"VMS={mem.vms / 1024 / 1024:.1f}MB, 线程数={threads}"
            )

        return "\n".join(lines)
```

- [ ] **Step 4: 写 self_state/plugin.py（复制 self_state.py 全部内容）**

```python
"""自我感知工具插件 — agent 查看自身状态。"""

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.safety import SafetyProfile


class SelfStatePlugin(ToolPlugin):
    name = "get_self_state"
    description = "查看 agent 的当前运行状态：FSM 状态、会话信息、工具使用情况"
    param_model = None
    capabilities: set = set()
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params=None,
    ) -> str:
        fsm_state = ctx.get("_fsm_state", "unknown")
        session_count = ctx.get("_session_msg_count", 0)
        tool_count = ctx.get("_tool_count", 0)
        trace_id = ctx.trace_id or "N/A"
        agent_id = ctx.agent_id or "default"

        lines = [
            f"Agent ID: {agent_id}",
            f"当前 Trace ID: {trace_id}",
            f"FSM 状态: {fsm_state}",
            f"会话消息数: {session_count}",
            f"已加载工具数: {tool_count}",
        ]
        return "\n".join(lines)
```

- [ ] **Step 5: 写这三个目录的 config.yaml、__init__.py、README.md**

reminder/config.yaml:
```yaml
# 提醒插件无特殊安全配置
```
reminder/__init__.py:
```python
"""提醒工具插件。"""
from .plugin import SetReminderPlugin
__all__ = ["SetReminderPlugin"]
```
reminder/README.md:
```markdown
# 提醒工具插件
设置定时提醒。

## 插件
- `set_reminder` — 在指定秒数后通知用户

## 配置
无需特殊配置。
```

runtime_config/config.yaml:
```yaml
# 运行时配置插件无特殊安全配置
```
runtime_config/__init__.py:
```python
"""运行时配置工具插件。"""
from .plugin import RuntimeConfigPlugin
__all__ = ["RuntimeConfigPlugin"]
```
runtime_config/README.md:
```markdown
# 运行时配置工具插件
查看 Agent 运行时配置和状态。

## 插件
- `get_runtime_config` — 查看模型、TTS/STT 状态、内存占用

## 配置
无需特殊配置。
```

self_state/config.yaml:
```yaml
# 自身状态插件无特殊安全配置
```
self_state/__init__.py:
```python
"""自身状态工具插件。"""
from .plugin import SelfStatePlugin
__all__ = ["SelfStatePlugin"]
```
self_state/README.md:
```markdown
# 自身状态工具插件
查看 Agent 当前运行状态。

## 插件
- `get_self_state` — 查看 FSM 状态、会话信息

## 配置
无需特殊配置。
```

---

### Task 6: 扩展 ToolManager.load_all() 加载 config.yaml

**Files:**
- Modify: `Module/tool/manager.py`

- [ ] **Step 1: 在 manager.py 文件顶部添加 import**

在现有 import 区域后添加:
```python
import inspect
import yaml
from pathlib import Path
```

- [ ] **Step 2: 修改 load_all() 方法**

在 `instance = cls()` 之后、`await instance.setup(ctx)` 之前插入配置加载逻辑。修改后的 `load_all()` 方法:

```python
    async def load_all(
        self,
        plugin_classes: list[type[ToolPlugin]],
        ctx: PluginContext,
    ) -> None:
        """加载并检测所有插件。setup() 失败则跳过并 log warning。"""
        self._plugin_context = ctx
        for cls in plugin_classes:
            instance = cls()
            try:
                # 从插件所在目录加载 config.yaml，覆盖 SafetyProfile 字段
                module_file = inspect.getfile(cls)
                plugin_dir = Path(module_file).parent
                config_file = plugin_dir / "config.yaml"
                if config_file.is_file():
                    try:
                        raw = yaml.safe_load(config_file.read_text("utf-8"))
                        if isinstance(raw, dict):
                            for key, value in raw.items():
                                if hasattr(instance.safety, key):
                                    setattr(instance.safety, key, value)
                    except (yaml.YAMLError, OSError) as e:
                        logger.warning(
                            "工具 '%s' config.yaml 加载失败: %s", cls.__name__, e,
                        )
                await instance.setup(ctx)
                self._plugins[instance.name] = instance
                self._plugin_classes[instance.name] = cls
                self._plugin_modules[instance.name] = cls.__module__

                if instance.safety and instance.safety.max_concurrency > 0:
                    self._semaphores[instance.name] = asyncio.Semaphore(
                        instance.safety.max_concurrency,
                    )

                logger.info("工具 '%s' 已加载", instance.name)
            except Exception as e:
                logger.warning(
                    "工具 '%s' setup() 失败，跳过: %s", instance.name, e,
                )
```

注意: `import inspect` 和 `import yaml` 需要加到文件顶部。`yaml` 使用标准库的 `PyYAML`（已在项目依赖中）。

- [ ] **Step 3: 验证**

```bash
python -c "
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext
from core.tools.filesystem.plugin import ReadFilePlugin

mgr = ToolManager()
# 模拟 ctx（不会真正 setup，只测试 load_all 的配置加载部分）
import asyncio
async def test():
    ctx = PluginContext(session_id='test')
    await mgr.load_all([ReadFilePlugin], ctx)
    p = mgr._plugins.get('read_file')
    if p:
        print('loaded:', p.name, p.safety.read_dirs)
asyncio.run(test())
"
```

---

### Task 7: 更新 ALL_PLUGINS 导入路径

**Files:**
- Modify: `core/tools/__init__.py`

- [ ] **Step 1: 替换所有 import**

将 `core/tools/__init__.py` 全部替换为:

```python
"""Elysia 工具插件集合。"""

from .filesystem.plugin import ReadFilePlugin, ListDirPlugin, WriteFilePlugin
from .web.plugin import WebSearchPlugin, WebFetchPlugin
from .memory.plugin import MemorySearchPlugin, MemorySavePlugin
from .time.plugin import TimePlugin
from .reminder.plugin import SetReminderPlugin
from .runtime_config.plugin import RuntimeConfigPlugin
from .self_state.plugin import SelfStatePlugin

# 所有插件类（按顺序加载）
ALL_PLUGINS: list[type] = [
    MemorySearchPlugin,
    MemorySavePlugin,
    TimePlugin,
    WebSearchPlugin,
    WebFetchPlugin,
    ReadFilePlugin,
    ListDirPlugin,
    WriteFilePlugin,
    SetReminderPlugin,
    RuntimeConfigPlugin,
    SelfStatePlugin,
]
```

- [ ] **Step 2: 验证导入**

```bash
python -c "from core.tools import ALL_PLUGINS; print([p.name for p in ALL_PLUGINS])"
```
Expected: `['memory_search', 'save_memory', 'get_time', 'web_search', 'web_fetch', 'read_file', 'list_directory', 'write_file', 'set_reminder', 'get_runtime_config', 'get_self_state']`

---

### Task 8: 迁移测试文件

**Files:**
- Move: `core/tools/test_filesystem.py` → `core/tools/filesystem/test_filesystem.py`
- Move: `core/tools/test_write_file.py` → `core/tools/filesystem/test_write_file.py`
- Move: `core/tools/test_web.py` → `core/tools/web/test_web.py`
- Move: `core/tools/test_memory.py` → `core/tools/memory/test_memory.py`
- Move: `core/tools/test_time.py` → `core/tools/time/test_time.py`
- Move: `core/tools/test_reminder.py` → `core/tools/reminder/test_reminder.py`
- Move: `core/tools/test_runtime_config.py` → `core/tools/runtime_config/test_runtime_config.py`
- Move: `core/tools/test_self_state.py` → `core/tools/self_state/test_self_state.py`

- [ ] **Step 1: 移动文件并更新导入路径**

```bash
for d in filesystem web memory time reminder runtime_config self_state; do
    test_file="test_${d}.py"
    cp "/home/yomu/Agent/core/tools/${test_file}" "/home/yomu/Agent/core/tools/${d}/${test_file}"
done
# write_file 的测试需要单独处理
cp /home/yomu/Agent/core/tools/test_write_file.py /home/yomu/Agent/core/tools/filesystem/test_write_file.py
```

- [ ] **Step 2: 更新每个测试文件的 import**

更新 import 路径从 `from core.tools.<module> import` 到 `from core.tools.<module>.plugin import`:

**filesystem/test_filesystem.py** 第 7 行:
```python
from core.tools.filesystem.plugin import (
    ReadFilePlugin, ReadFileParams,
    ListDirPlugin, ListDirParams,
    _format_size,
)
```

**filesystem/test_write_file.py** 第 8 行:
```python
from core.tools.filesystem.plugin import WriteFilePlugin, WriteFileParams
```

**web/test_web.py** 将所有 `from core.tools.web import` 改为 `from core.tools.web.plugin import`:
```python
from core.tools.web.plugin import _extract_text
from core.tools.web.plugin import WebSearchPlugin, WebSearchParams
from core.tools.web.plugin import WebFetchPlugin, WebFetchParams
```

**memory/test_memory.py** 第 7 行:
```python
from core.tools.memory.plugin import (
    MemorySearchPlugin, SearchMemoryParams,
    MemorySavePlugin, SaveMemoryParams,
)
```

**time/test_time.py** 第 7 行:
```python
from core.tools.time.plugin import TimePlugin, TimeParams
```

**reminder/test_reminder.py** 第 7 行:
```python
from core.tools.reminder.plugin import SetReminderPlugin, ReminderParams
```

**runtime_config/test_runtime_config.py** 第 6 行:
```python
from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams
```

**self_state/test_self_state.py** 第 5 行:
```python
from core.tools.self_state.plugin import SelfStatePlugin
```

- [ ] **Step 3: 更新 core/tests/test_phase3.py 的 import**

将 `test_phase3.py` 第 1542 行:
```python
    from core.tools.filesystem.plugin import ReadFilePlugin
    p = ReadFilePlugin()
    assert p.safety.filesystem_allowed is True
    assert p.safety.read_dirs == ["/home/yomu/data/sandbox"]
    assert p.safety.allow_symlinks is False
    assert p.safety.max_file_bytes == 10 * 1024 * 1024
```

---

### Task 9: 删除旧文件

**Files:**
- Delete: 8 个旧 plugin.py + 8 个旧 test 文件

- [ ] **Step 1: 删除旧文件**

```bash
rm /home/yomu/Agent/core/tools/filesystem.py
rm /home/yomu/Agent/core/tools/write_file.py
rm /home/yomu/Agent/core/tools/web.py
rm /home/yomu/Agent/core/tools/memory.py
rm /home/yomu/Agent/core/tools/time.py
rm /home/yomu/Agent/core/tools/reminder.py
rm /home/yomu/Agent/core/tools/runtime_config.py
rm /home/yomu/Agent/core/tools/self_state.py
rm /home/yomu/Agent/core/tools/test_filesystem.py
rm /home/yomu/Agent/core/tools/test_write_file.py
rm /home/yomu/Agent/core/tools/test_web.py
rm /home/yomu/Agent/core/tools/test_memory.py
rm /home/yomu/Agent/core/tools/test_time.py
rm /home/yomu/Agent/core/tools/test_reminder.py
rm /home/yomu/Agent/core/tools/test_runtime_config.py
rm /home/yomu/Agent/core/tools/test_self_state.py
```

---

### Task 10: 运行全部测试验证

- [ ] **Step 1: 运行 tool 相关单元测试**

```bash
python -m pytest core/tools/ -v
```
Expected: ALL tests pass (no import errors, no broken tests)

- [ ] **Step 2: 运行 module 层测试**

```bash
python -m pytest Module/tool/ -v
```
Expected: ALL tests pass

- [ ] **Step 3: 运行所有核心测试**

```bash
python -m pytest core/tests/ -v
```
Expected: ALL tests pass

- [ ] **Step 4: 运行全部测试**

```bash
python -m pytest Module/ core/ -v
```
Expected: ALL tests pass

---

### Task 11: 提交

- [ ] **Step 1: 检查 git status**

```bash
git status
```

- [ ] **Step 2: 分两次提交**

首次提交 — 新目录和文件:
```bash
git add core/tools/filesystem/
git add core/tools/web/
git add core/tools/memory/
git add core/tools/time/
git add core/tools/reminder/
git add core/tools/runtime_config/
git add core/tools/self_state/
git add Module/tool/manager.py
git add core/tools/__init__.py
git add core/tests/test_phase3.py
git commit -m "refactor: move tool plugins to per-directory layout with config.yaml"
```

二次提交 — 删除旧文件:
```bash
git rm core/tools/filesystem.py
git rm core/tools/write_file.py
git rm core/tools/web.py
git rm core/tools/memory.py
git rm core/tools/time.py
git rm core/tools/reminder.py
git rm core/tools/runtime_config.py
git rm core/tools/self_state.py
git rm core/tools/test_filesystem.py
git rm core/tools/test_write_file.py
git rm core/tools/test_web.py
git rm core/tools/test_memory.py
git rm core/tools/test_time.py
git rm core/tools/test_reminder.py
git rm core/tools/test_runtime_config.py
git rm core/tools/test_self_state.py
git commit -m "chore: remove old flat plugin files (migrated to per-directory layout)"
```

---

## Verification Checklist

1. `python -m pytest core/tools/ -v` 全部通过
2. `python -m pytest Module/ core/ -v` 全部通过
3. 修改 `core/tools/filesystem/config.yaml` 的 `read_dirs`，重启服务后生效
4. 通过 Web 界面让 Agent 访问 `/home/yomu/data/sandbox` 下的文件，确认能正常读取
