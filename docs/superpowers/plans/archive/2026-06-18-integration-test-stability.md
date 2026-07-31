# Integration Test Stability Improvement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unstable `WebSearchPlugin` with deterministic `MockWebSearchPlugin` in integration tests, add shared test helpers, and eliminate duplicated `.env`-loading boilerplate.

**Architecture:** New `MockWebSearchPlugin` (deterministic test double) at `core/tools/web/mock_plugin.py`; new `conftest.py` for pytest-level `.env` loading; new `Module/task_system/tests/helpers/` package providing `build_test_task_system()`, `get_tools_from_manager()`, and `mock_replan_response()`.

**Tech Stack:** Python 3.13, pytest (async), ToolPlugin base class, unittest.mock.patch

## Global Constraints

- Do NOT modify TaskSystem / Planner / Replanner / Evaluator / Orchestrator / SubAgent core logic
- Do NOT add new fields to `ConstraintConfig` / `TaskConstraints` / `SafetyProfile`
- Do NOT touch unit tests (`tests/unit/`)
- Do NOT modify `core/tools/web/plugin.py`
- Do NOT add mock plugins to `core/tools/__init__.py` `ALL_PLUGINS`
- Do NOT modify `test_planner.py` (it uses pure-data `Tool` dataclass, not Plugin→ToolManager path)
- All code comments in Chinese, following project convention

---

### Task 1: Create MockWebSearchPlugin

**Files:**
- Create: `core/tools/web/mock_plugin.py`
- Modify: `core/tools/web/__init__.py`

**Interfaces:**
- Produces: `MockWebSearchPlugin` class — `name="web_search"`, `param_model=WebSearchParams`, `capabilities={NETWORK, WEB_SEARCH}`, `safety=SafetyProfile(...)`, `async execute(ctx, emit, params) -> str`

- [ ] **Step 1: Write MockWebSearchPlugin class**

Create `core/tools/web/mock_plugin.py`:

```python
"""确定性 web_search mock —— 集成测试用，返回固定结果，不发起网络请求。"""

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile
from .plugin import WebSearchParams


class MockWebSearchPlugin(ToolPlugin):
    """Mock web_search —— 返回预设的搜索结果，不发起网络请求。

    与真实 WebSearchPlugin 保持相同的 name、param_model、capabilities、safety，
    通过 ToolManager.load_all([TimePlugin, MockWebSearchPlugin], ctx) 无缝替换。
    真实 WebSearchPlugin 不被加载。
    """

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
        """返回基于 query 关键词的预设搜索结果，不发起网络请求。"""
        query = params.query
        limit = min(params.limit, 10)

        # 根据 query 关键词返回不同领域的固定结果
        results = _pick_results(query)
        return _format_results(results[:limit])


def _pick_results(query: str) -> list[dict[str, str]]:
    """根据 query 关键词选择预设结果集。"""
    q = query.lower()

    if any(kw in q for kw in ("天气", "weather", "气温")):
        return _WEATHER_RESULTS
    if any(kw in q for kw in ("新闻", "news", "头条")):
        return _NEWS_RESULTS
    if any(kw in q for kw in ("代码", "code", "编程", "python", "rust", "golang")):
        return _CODE_RESULTS
    if any(kw in q for kw in ("电影", "movie", "音乐", "music")):
        return _ENTERTAINMENT_RESULTS
    return _GENERAL_RESULTS


def _format_results(results: list[dict[str, str]]) -> str:
    """格式化为与 _tavily_search() 一致的输出格式。"""
    if not results:
        return "未找到相关结果。"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        content = r.get("content", "")
        lines.append(f"{i}. {title}\n   {url}\n   {content}")
    return "\n\n".join(lines)


# ── 预设结果集 ──────────────────────────────────────────────────────────────────

_WEATHER_RESULTS = [
    {
        "title": "北京天气预报一周 — 中国天气网",
        "url": "https://weather.example.com/beijing",
        "content": "北京今日晴转多云，气温 22°C ~ 32°C，东南风 3-4 级，空气质量良。未来三天以晴为主，气温稳定。",
    },
    {
        "title": "上海天气预报 — 中央气象台",
        "url": "https://weather.example.com/shanghai",
        "content": "上海今日阴有阵雨，气温 25°C ~ 30°C，东风 2-3 级，相对湿度 75%。",
    },
    {
        "title": "全国降水量预报图 — 国家气象局",
        "url": "https://weather.example.com/rainfall",
        "content": "未来 24 小时全国降水主要集中在华南和华东地区，华北晴好天气持续。",
    },
]

_NEWS_RESULTS = [
    {
        "title": "科技巨头发布新一代 AI 芯片 — 科技日报",
        "url": "https://news.example.com/ai-chip-2026",
        "content": "该芯片采用 3nm 制程，算力较上一代提升 3 倍，功耗降低 40%，预计下半年量产。",
    },
    {
        "title": "量子计算取得新突破 — 自然科学杂志",
        "url": "https://news.example.com/quantum-2026",
        "content": "研究团队成功实现 1000+ 量子比特的稳定纠缠，纠错率达到实用化门槛。",
    },
    {
        "title": "全球可再生能源装机创新高 — 能源周刊",
        "url": "https://news.example.com/renewable-energy",
        "content": "2026 年上半年全球光伏和风电新增装机突破 500GW，中国占比超 40%。",
    },
]

_CODE_RESULTS = [
    {
        "title": "Python 3.13 新特性详解 — Python 官方文档",
        "url": "https://docs.python.org/3.13/whatsnew/",
        "content": "Python 3.13 引入改进的 asyncio 性能、新的类型注解语法、更好的异常信息等特性。",
    },
    {
        "title": "Rust 异步编程最佳实践 — Rust 社区博客",
        "url": "https://blog.rust-lang.org/async-best-practices",
        "content": "使用 tokio 进行异步编程时应注意避免 blocking 操作、合理使用 select! 宏、正确管理取消安全。",
    },
    {
        "title": "如何设计可测试的分布式系统 — 架构博客",
        "url": "https://example.com/distributed-testing",
        "content": "关键原则：外部依赖可替换、状态可注入、超时可配置、日志结构化。使用 test double 替代真实服务。",
    },
]

_ENTERTAINMENT_RESULTS = [
    {
        "title": "热映电影票房排行榜 — 豆瓣电影",
        "url": "https://movie.example.com/boxoffice",
        "content": "本周票房前三：《星际探索 2》（3.2 亿）、《重返地球》（1.8 亿）、《盛夏光年》（1.2 亿）。",
    },
    {
        "title": "2026 年度音乐榜单发布 — QQ 音乐",
        "url": "https://music.example.com/annual-2026",
        "content": "年度最受欢迎歌手包括周杰伦、Taylor Swift、BTS，流媒体播放量超百亿次。",
    },
]

_GENERAL_RESULTS = [
    {
        "title": "维基百科 — 自由的百科全书",
        "url": "https://zh.wikipedia.org/wiki/Main_Page",
        "content": "维基百科是一个多语言的网络百科全书，由全球志愿者共同维护，现有超过 140 万篇中文条目。",
    },
    {
        "title": "如何高效获取信息 — 研究方法论",
        "url": "https://example.com/research-methods",
        "content": "信息获取应遵循交叉验证原则：使用多个独立来源互相印证，优先选择权威和一手来源。",
    },
    {
        "title": "搜索引擎使用技巧 — 数字素养指南",
        "url": "https://example.com/search-tips",
        "content": "使用精确关键词、引号匹配、site: 限定域名、filetype: 限定文件类型等技巧可显著提高搜索效率。",
    },
    {
        "title": "信息检索技术综述 — 学术论文",
        "url": "https://example.com/ir-survey",
        "content": "现代信息检索系统结合倒排索引、BM25 排序、语义向量检索等技术，提供高效准确的结果。",
    },
    {
        "title": "知识图谱在搜索中的应用 — 技术博客",
        "url": "https://example.com/knowledge-graph-search",
        "content": "知识图谱通过实体链接、关系推理提升搜索结果的相关性和完整性，Google、Bing 已广泛应用。",
    },
]
```

- [ ] **Step 2: Update `core/tools/web/__init__.py` to export MockWebSearchPlugin**

```python
"""网络工具插件 — 搜索和网页抓取。"""

from .plugin import WebSearchPlugin, WebFetchPlugin
from .mock_plugin import MockWebSearchPlugin

__all__ = ["WebSearchPlugin", "WebFetchPlugin", "MockWebSearchPlugin"]
```

- [ ] **Step 3: Verify MockWebSearchPlugin can be imported and instantiated**

Run: `python -c "from core.tools.web.mock_plugin import MockWebSearchPlugin; m = MockWebSearchPlugin(); print(f'name={m.name}, param_model={m.param_model.__name__}, caps={m.capabilities}')"`

Expected output: `name=web_search, param_model=WebSearchParams, caps={...}`

- [ ] **Step 4: Verify MockWebSearchPlugin.execute() returns stable output**

Run: `python -c "
import asyncio
from core.tools.web.mock_plugin import MockWebSearchPlugin
from Module.tool.protocol import PluginContext
m = MockWebSearchPlugin()
ctx = PluginContext(session_id='test')
r1 = asyncio.run(m.execute(ctx=ctx, params=m.param_model(query='天气')))
r2 = asyncio.run(m.execute(ctx=ctx, params=m.param_model(query='天气')))
print('Same output:', r1 == r2)
print('Contains weather:', '天气' in r1)
"`

Expected: `Same output: True` and `Contains weather: True`

- [ ] **Step 5: Commit**

```bash
git add core/tools/web/mock_plugin.py core/tools/web/__init__.py
git commit -m "feat: add MockWebSearchPlugin — deterministic test double for integration tests"
```

---

### Task 2: Create conftest.py for .env loading

**Files:**
- Create: `Module/task_system/tests/integration/conftest.py`

**Interfaces:**
- Produces: pytest session-level `.env` loading — sets `DEEPSEEK_API_KEY` and other env vars before any test runs

- [ ] **Step 1: Write conftest.py**

Create `Module/task_system/tests/integration/conftest.py`:

```python
"""pytest conftest — 集成测试模块级 .env 加载。

在测试收集阶段自动加载项目根目录的 .env 文件。
各测试文件中分散的 .env 加载代码可逐步删除。
"""

from pathlib import Path

# ── 加载 .env ────────────────────────────────────────────────────────────────────

_env_file = Path(__file__).resolve().parents[5] / ".env"
# 路径：conftest.py → integration/ → tests/ → task_system/ → Module/ → Agent/ → .env
# parents[0] = conftest.py 所在目录 (integration/)
# parents[5] = Agent/ (repo root)

if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _value = _line.partition("=")
                _key = _key.strip()
                _value = _value.strip().strip('"').strip("'")
                import os
                if _key not in os.environ:
                    os.environ[_key] = _value
```

Note: Uses manual `.env` parsing (not `python-dotenv`) to avoid adding a dependency. The project's existing test files already use this pattern — `conftest.py` simply consolidates it.

- [ ] **Step 2: Verify conftest.py is loaded by pytest**

Run: `python -m pytest Module/task_system/tests/integration/ --collect-only -q 2>&1 | head -5`

Expected: Tests are collected without error. (Verifies conftest.py doesn't break test discovery.)

- [ ] **Step 3: Temporarily verify env loading works**

Add a quick test by running a simple python script from the conftest path:

Run: `python -c "
import os, sys
sys.path.insert(0, 'Module/task_system/tests/integration')
# Import triggers conftest (which runs at module load in pytest, not via import)
# Manual verification:
from pathlib import Path
_env_file = Path('Module/task_system/tests/integration/conftest.py').resolve().parents[5] / '.env'
print(f'.env path: {_env_file}')
print(f'exists: {_env_file.exists()}')
"`

Expected: Path resolves correctly to repo root `.env`.

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/integration/conftest.py
git commit -m "test: add conftest.py for integration test .env loading"
```

---

### Task 3: Create shared test helpers module

**Files:**
- Create: `Module/task_system/tests/helpers/__init__.py`

**Interfaces:**
- Produces:
  - `MOCK_PLUGIN_MAP: dict[str, type[ToolPlugin]]` — `{"web_search": MockWebSearchPlugin}`
  - `async build_test_task_system(plugins=None, use_mocks=True, wrap_llm=False) -> TaskSystem`
  - `get_tools_from_manager(tm: ToolManager) -> list[Tool]`
  - `mock_replan_response(response: DAGPatch | Plan)` -> `unittest.mock._patch`
- Consumes: `MockWebSearchPlugin` from Task 1, `TaskSystem`, `ToolManager`, `AgentInfra`, `ConstraintConfig`, `PluginContext`

- [ ] **Step 1: Write the helpers module**

Create `Module/task_system/tests/helpers/__init__.py`:

```python
"""集成测试辅助工具 —— TaskSystem 工厂、工具提取、mock 工具函数。

用法:
    from Module.task_system.tests.helpers import (
        build_test_task_system,
        get_tools_from_manager,
        mock_replan_response,
        MOCK_PLUGIN_MAP,
    )

    # 创建带 mock 工具的 TaskSystem（默认）
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],  # WebSearchPlugin → MockWebSearchPlugin
    )

    # 从 TaskSystem 提取 Tool 列表给 DelegationPreparer
    tools = get_tools_from_manager(ts.infra.tool_registry)

    # Mock Replanner 返回固定 DAGPatch（跳过 LLM 调用）
    with mock_replan_response(my_patch):
        ...
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

from openai import AsyncOpenAI

from Module.task_system.infra import AgentInfra
from Module.task_system.schema import (
    ConstraintConfig,
    DAGPatch,
    Plan,
    Tool,
)
from Module.task_system.task_system import TaskSystem
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext, ToolPlugin
from core.tools.time.plugin import TimePlugin
from core.tools.web.mock_plugin import MockWebSearchPlugin


# ── Mock 插件映射 ────────────────────────────────────────────────────────────────

MOCK_PLUGIN_MAP: dict[str, type[ToolPlugin]] = {
    "web_search": MockWebSearchPlugin,
}
"""工具名 → mock 插件类映射。后续新增不稳定工具的 mock 时往这里加条目。"""


# ── TaskSystem 工厂 ──────────────────────────────────────────────────────────────

async def build_test_task_system(
    plugins: list[type[ToolPlugin]] | None = None,
    use_mocks: bool = True,
    wrap_llm: bool = False,
) -> TaskSystem:
    """创建并启动 TaskSystem（真实 LLM + 选定工具）。

    Args:
        plugins: 要加载的插件类列表。默认 None → [TimePlugin]（纯确定性工具）。
                 e2e 测试传 [TimePlugin, WebSearchPlugin]。
        use_mocks: True → 将 plugins 中的不稳定工具替换为 MOCK_PLUGIN_MAP 中的 mock。
                   例如 [TimePlugin, WebSearchPlugin] → [TimePlugin, MockWebSearchPlugin]。
                   False → 原样使用 plugins。
        wrap_llm: True → 包装 LLM client 以记录调用（e2e 测试日志用）。

    Returns:
        已启动的 TaskSystem 实例。

    Raises:
        RuntimeError: DEEPSEEK_API_KEY 未设置。
    """
    if plugins is None:
        plugins = [TimePlugin]

    # 替换不稳定工具为 mock
    resolved: list[type[ToolPlugin]] = []
    for cls in plugins:
        # 获取类级别 name（实例化前的类属性）
        plugin_name = getattr(cls, "name", "")
        if use_mocks and plugin_name in MOCK_PLUGIN_MAP:
            resolved.append(MOCK_PLUGIN_MAP[plugin_name])
        else:
            resolved.append(cls)

    # .env 兜底加载（conftest.py 已覆盖，此处为安全网）
    _ensure_env()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置，无法创建 AsyncOpenAI")

    llm = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    if wrap_llm:
        llm = _wrap_llm_client(llm)

    tm = ToolManager()
    ctx = PluginContext(session_id="integration-test")
    await tm.load_all(resolved, ctx)

    infra = AgentInfra(
        llm=llm,
        milvus=None,
        http=None,
        tool_registry=tm,
        constraints=ConstraintConfig(),
    )

    ts = TaskSystem(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=2)
    await ts.start()
    return ts


# ── 工具提取 ─────────────────────────────────────────────────────────────────────

def get_tools_from_manager(tm: ToolManager) -> list[Tool]:
    """从 ToolManager 提取 TaskSystem 格式的 Tool 列表。

    供 DelegationPreparer.prepare() 使用。

    已知局限：访问 tm._plugins（私有属性）。后续建议给 ToolManager
    增加公共方法 list_tools()，届时此函数改为调用公共 API。
    """
    tools: list[Tool] = []
    for name, plugin in tm._plugins.items():
        caps = getattr(plugin, "capabilities", [])
        if isinstance(caps, str):
            caps = [caps]
        desc = (
            getattr(plugin, "description", "")
            or getattr(plugin, "tool_description", "")
            or ""
        )
        tools.append(Tool(
            name=name,
            description=desc,
            capabilities=list(caps) if caps else [],
        ))
    return tools


# ── Replanner mock ───────────────────────────────────────────────────────────────

def mock_replan_response(response: DAGPatch | Plan):
    """返回一个 patch 上下文管理器，mock Replanner.replan() 返回固定结果。

    用法（在测试函数体内，无需 pytest monkeypatch fixture）:

        patch_obj = DAGPatch(reason="test", trigger_nodes=["node_1"])
        with mock_replan_response(patch_obj):
            # 提交任务，Replanner.replan() 会被 mock 返回 patch_obj
            ...

    Args:
        response: 预设的 DAGPatch 或 Plan，Replanner.replan() 将直接返回此对象。

    Returns:
        unittest.mock.patch 上下文管理器。
    """
    async def _mock_replan(self, *args: Any, **kwargs: Any) -> DAGPatch | Plan:
        return response

    return patch(
        "Module.task_system.core.planner.replanner.Replanner.replan",
        side_effect=_mock_replan,
    )


# ── 内部 helper ──────────────────────────────────────────────────────────────────

def _ensure_env() -> None:
    """兜底 .env 加载（conftest.py 已覆盖，此处防止直接导入 helpers 时不加载）。"""
    from pathlib import Path
    _env_file = Path(__file__).resolve().parents[5] / ".env"
    if _env_file.exists():
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _, _value = _line.partition("=")
                    _key = _key.strip()
                    _value = _value.strip().strip('"').strip("'")
                    if _key not in os.environ:
                        os.environ[_key] = _value


def _wrap_llm_client(llm: AsyncOpenAI) -> AsyncOpenAI:
    """包装 AsyncOpenAI，拦截 chat.completions.create() 以记录 LLM 调用。

    这是一个轻量包装——仅打印调用摘要。完整版本（含 prompt/response 打印）
    保留在 test_e2e_mvp.py 中，因为它依赖大量 e2e 专属的日志格式化函数。
    """
    import time
    original_create = llm.chat.completions.create

    async def _wrapped_create(**kwargs: Any) -> Any:
        t0 = time.monotonic()
        result = await original_create(**kwargs)
        elapsed = time.monotonic() - t0
        model = getattr(result, "model", "?")
        usage = getattr(result, "usage", None)
        tokens = f"in={usage.prompt_tokens}/out={usage.completion_tokens}" if usage else "?"
        print(f"  [LLM] {model} {elapsed:.1f}s tokens=({tokens})")
        return result

    llm.chat.completions.create = _wrapped_create  # type: ignore[method-assign]
    return llm
```

- [ ] **Step 2: Verify helpers import correctly**

Run: `python -c "from Module.task_system.tests.helpers import build_test_task_system, get_tools_from_manager, mock_replan_response, MOCK_PLUGIN_MAP; print('imports OK'); print('MOCK_PLUGIN_MAP:', MOCK_PLUGIN_MAP)"`

Expected: `imports OK` and `MOCK_PLUGIN_MAP: {'web_search': <class '...MockWebSearchPlugin'>}`

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/tests/helpers/__init__.py
git commit -m "test: add shared helpers — build_test_task_system, get_tools_from_manager, mock_replan_response"
```

---

### Task 4: Update test_e2e_mvp.py to use helpers

**Files:**
- Modify: `Module/task_system/tests/integration/test_e2e_mvp.py`

**Interfaces:**
- Consumes: `build_test_task_system` (Task 3), `get_tools_from_manager` (Task 3)
- Produces: No new interface — same test functions, internal refactor only

- [ ] **Step 1: Remove .env loading code (lines 32-42)**

Delete lines 32-42:
```python
# DELETE this block:
_env_path = Path(__file__).resolve().parents[4] / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _value = _line.partition("=")
                _key = _key.strip()
                _value = _value.strip().strip('"').strip("'")
                if _key not in os.environ:
                    os.environ[_key] = _value
```

Also remove unused imports that were only needed for .env loading:
- `os` (check if still used elsewhere — it is, in `os.getenv("DEEPSEEK_API_KEY")` in `_build_task_system`, but that function will be removed)
- `Path` from `pathlib` (check if still used — will be removed with `_build_task_system`)

- [ ] **Step 2: Add helpers import**

Add after the existing import block (around line 59):

```python
from Module.task_system.tests.helpers import (
    build_test_task_system,
    get_tools_from_manager,
)
```

- [ ] **Step 3: Remove `_build_task_system()` function (lines 342-376)**

Delete the entire `_build_task_system()` function.

- [ ] **Step 4: Remove `_get_tools_from_manager()` function (lines 379-396)**

Delete the entire `_get_tools_from_manager()` function.

- [ ] **Step 5: Remove now-unused imports**

Check and remove unused imports after the above deletions:
- `TimePlugin` (was only used in `_build_task_system`)
- `WebSearchPlugin` (was only used in `_build_task_system`)
- `ToolManager` (was only used in `_build_task_system` and `_get_tools_from_manager`)
- `PluginContext` (was only used in `_build_task_system`)
- `AgentInfra` (was only used in `_build_task_system`)
- `ConstraintConfig` (was only used in `_build_task_system` and `_run_task`)
  - Note: `ConstraintConfig` is still used in `_run_task` via `ConstraintConfig()`, keep it

Let me verify what's still needed:

```bash
# After deletions, check: grep for TimePlugin, WebSearchPlugin, ToolManager, PluginContext in test_e2e_mvp.py
```

- [ ] **Step 6: Replace `_build_task_system()` calls with `build_test_task_system()`**

Find all calls to `_build_task_system()` and replace with:

```python
ts = await build_test_task_system(
    plugins=[TimePlugin, WebSearchPlugin],
    use_mocks=True,
    wrap_llm=True,
)
```

But wait — `wrap_llm=True` needs the e2e-specific `_wrap_llm_client` function which does detailed prompt/response logging. The helpers' `_wrap_llm_client` is a lightweight version. The e2e tests need the full version.

**Design decision**: Keep the e2e-specific `_wrap_llm_client` (with its `_detect_component` and detailed printing) in `test_e2e_mvp.py`. Pass `wrap_llm=False` to `build_test_task_system()`, then wrap manually. OR: modify `build_test_task_system()` to accept an optional `wrap_fn` parameter.

**Simpler approach**: Don't use `wrap_llm=True` in e2e tests. Instead, call `build_test_task_system(wrap_llm=False)`, then wrap the LLM client with the e2e-specific `_wrap_llm_client` after creation. But `build_test_task_system` hides the LLM client inside...

**Best approach**: Add an optional `llm_wrapper: Callable[[AsyncOpenAI], AsyncOpenAI] | None = None` parameter to `build_test_task_system()`. If provided, it wraps the LLM client. The e2e tests pass `llm_wrapper=_wrap_llm_client`.

Wait, that's scope creep on Task 3. Let me instead just handle this in the test file: the e2e tests can continue to use their own inline `_build_task_system` pattern... No, the whole point is to eliminate it.

Let me add the `llm_wrapper` param to `build_test_task_system` as part of Task 3's interface. Actually, I already defined the interface in Task 3. Let me update the approach:

In test_e2e_mvp.py, use:
```python
ts = await build_test_task_system(
    plugins=[TimePlugin, WebSearchPlugin],
    use_mocks=True,
    wrap_llm=False,  # don't use helpers' light wrapper
)
# Then manually wrap the LLM client with e2e's detailed wrapper:
ts.infra.llm = _wrap_llm_client(ts.infra.llm)
```

This works because `build_test_task_system` returns a fully constructed TaskSystem, and `ts.infra.llm` is the AsyncOpenAI client. The e2e's `_wrap_llm_client` replaces `chat.completions.create` with the detailed logging version.

Actually wait, this will still work because `_wrap_llm_client` is already defined in the e2e test file. We just need to apply it after getting the TaskSystem.

- [ ] **Step 6 (revised): Replace `_build_task_system()` calls**

Each test that previously did:
```python
ts = await _build_task_system()
```

Now does:
```python
ts = await build_test_task_system(
    plugins=[TimePlugin, WebSearchPlugin],
    use_mocks=True,
    wrap_llm=False,
)
# Wrap with e2e-specific detailed LLM logger
ts.infra.llm = _wrap_llm_client(ts.infra.llm, label="LLM")
```

And each call to `_get_tools_from_manager(tm)` becomes `get_tools_from_manager(tm)`.

- [ ] **Step 7: Add missing imports for types that were previously transitively imported**

Check that `TimePlugin` and `WebSearchPlugin` imports are still present (they need to be passed to `build_test_task_system`):

```python
from core.tools.time.plugin import TimePlugin
from core.tools.web.plugin import WebSearchPlugin  # keep for passing to build_test_task_system(plugins=[..., WebSearchPlugin])
```

Note: `WebSearchPlugin` is passed to `build_test_task_system()` but will be replaced by `MockWebSearchPlugin` via `MOCK_PLUGIN_MAP` when `use_mocks=True`. The import is still needed so the class reference can be passed.

- [ ] **Step 8: Run test_e2e_mvp.py to verify**

Run: `python -m pytest Module/task_system/tests/integration/test_e2e_mvp.py -v --timeout=300 2>&1 | tail -20`

Expected: Tests are collected and start running (may skip if DEEPSEEK_API_KEY not set, or pass/fail normally).

- [ ] **Step 9: Commit**

```bash
git add Module/task_system/tests/integration/test_e2e_mvp.py
git commit -m "test: refactor test_e2e_mvp.py to use shared helpers"
```

---

### Task 5: Update test_task_system_lifecycle.py to use helpers

**Files:**
- Modify: `Module/task_system/tests/integration/test_task_system_lifecycle.py`

**Interfaces:**
- Consumes: `build_test_task_system` (Task 3), `get_tools_from_manager` (Task 3)

- [ ] **Step 1: Remove .env loading code (lines 24-34)**

Delete lines 24-34 (the `_env_path = ...` block).

- [ ] **Step 2: Add helpers import**

Add after the import block:
```python
from Module.task_system.tests.helpers import build_test_task_system, get_tools_from_manager
```

- [ ] **Step 3: Remove `_build_task_system()` function (lines 61-79)**

Delete the entire `_build_task_system()` function.

- [ ] **Step 4: Remove `_tools_from_manager()` function (lines 82-91)**

Delete the entire `_tools_from_manager()` function.

- [ ] **Step 5: Remove now-unused imports**

Remove imports that are no longer needed:
- `os` (check if used elsewhere — used in `_build_task_system` only)
- `AsyncOpenAI` (used only in `_build_task_system`)
- `AgentInfra` (used only in `_build_task_system`)
- `ConstraintConfig` (used only in `_build_task_system`? Actually check — it's also used at line 362 in `test_goal_failed_triggers_replan` for `max_failure_count=2`)
  - Keep `ConstraintConfig`
- `ToolManager` (used only in `_build_task_system`)
- `PluginContext` (used only in `_build_task_system`)
- `TimePlugin` (used only in `_build_task_system`)

Wait — `TimePlugin` is still needed since we pass it to `build_test_task_system()`.

- [ ] **Step 6: Replace `_build_task_system()` calls**

Find all calls to `ts = await _build_task_system()` and replace with:

```python
ts = await build_test_task_system(plugins=[TimePlugin])
```

And `_tools_from_manager(ts.infra.tool_registry)` → `get_tools_from_manager(ts.infra.tool_registry)`

- [ ] **Step 7: Verify imports are clean**

Run: `python -c "import py_compile; py_compile.compile('Module/task_system/tests/integration/test_task_system_lifecycle.py', doraise=True); print('syntax OK')"`

Expected: `syntax OK`

- [ ] **Step 8: Commit**

```bash
git add Module/task_system/tests/integration/test_task_system_lifecycle.py
git commit -m "test: refactor test_task_system_lifecycle.py to use shared helpers"
```

---

### Task 6: Update test_executor.py to use helpers

**Files:**
- Modify: `Module/task_system/tests/integration/test_executor.py`

**Interfaces:**
- Consumes: `build_test_task_system` (Task 3)

- [ ] **Step 1: Remove .env loading code (lines 13-23)**

Delete lines 13-23 (the `_env_path = ...` block).

- [ ] **Step 2: Update `_build_integration_executor()` to use helpers for ToolManager creation**

Replace the `_build_integration_executor()` function body that manually creates `ToolManager` + `PluginContext` + `load_all` with helpers. The current function:

```python
async def _build_integration_executor():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    llm = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    tm = ToolManager()
    ctx = PluginContext(session_id="executor-test")
    await tm.load_all([TimePlugin], ctx)
    ...
```

Becomes:

```python
from Module.task_system.tests.helpers import build_test_task_system

async def _build_integration_executor():
    """构建集成测试 Executor——真实 LLM + 加载 TimePlugin 的 ToolManager。

    需要 DEEPSEEK_API_KEY 环境变量。如果未设置，返回 None。
    """
    import os
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    # 使用 helpers 工厂创建 ToolManager（只取 ToolManager，不需要完整 TaskSystem）
    from Module.tool.manager import ToolManager
    from Module.tool.protocol import PluginContext

    tm = ToolManager()
    ctx = PluginContext(session_id="executor-test")
    await tm.load_all([TimePlugin], ctx)
    ...
```

Wait — the Executor tests don't need a full TaskSystem. They only need `ToolManager` + LLM. `build_test_task_system()` creates a full TaskSystem which is overkill.

**Better approach**: `test_executor.py` only needs `.env` loading removed. Its `_build_integration_executor()` already only creates the minimal components it needs. Just remove the .env loading code (conftest handles it) and remove unused imports (`Path`, may `os`).

- [ ] **Step 1 (revised): Remove .env loading code only**

Delete lines 13-23. Keep `_build_integration_executor()` as-is — it doesn't create a TaskSystem and doesn't use WebSearchPlugin.

- [ ] **Step 2: Remove now-unused imports**

Remove `from pathlib import Path` (only used for .env loading).
Check if `os` is still needed — it's used in `os.getenv("DEEPSEEK_API_KEY")` in `_build_integration_executor()`, so keep it.

- [ ] **Step 3: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('Module/task_system/tests/integration/test_executor.py', doraise=True); print('syntax OK')"`

Expected: `syntax OK`

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/integration/test_executor.py
git commit -m "test: remove .env loading from test_executor.py (handled by conftest)"
```

---

### Task 7: Update test_delegation_preparer.py to use helpers

**Files:**
- Modify: `Module/task_system/tests/integration/test_delegation_preparer.py`

**Interfaces:**
- Consumes: conftest.py .env loading (Task 2)

- [ ] **Step 1: Remove .env loading code (lines 10-21)**

Delete lines 10-21 (the `_env_path = ...` block).

- [ ] **Step 2: Remove now-unused imports**

Remove `import os` (only used in .env loading block — verify by checking rest of file).
Remove `from pathlib import Path` (only used in .env loading block).

- [ ] **Step 3: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('Module/task_system/tests/integration/test_delegation_preparer.py', doraise=True); print('syntax OK')"`

Expected: `syntax OK`

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/integration/test_delegation_preparer.py
git commit -m "test: remove .env loading from test_delegation_preparer.py (handled by conftest)"
```

---

### Task 8: Integration verification — run full test suite

**Files:**
- None (verification only)

- [ ] **Step 1: Run all integration tests**

```bash
python -m pytest Module/task_system/tests/integration/ -v --timeout=300 2>&1
```

Expected: All tests pass or skip (if DEEPSEEK_API_KEY not set). No import errors, no missing symbols.

- [ ] **Step 2: Verify no .env loading code remains in test files**

```bash
grep -n "_env_path = Path" Module/task_system/tests/integration/test_*.py
```

Expected: No matches (test_planner.py may still have it since it's excluded from this change). Actually test_planner.py WILL still have it — that's expected per the spec.

Expected output should show only `test_planner.py` has the .env loading code.

- [ ] **Step 3: Verify helpers import works standalone**

```bash
python -c "
from Module.task_system.tests.helpers import (
    build_test_task_system,
    get_tools_from_manager,
    mock_replan_response,
    MOCK_PLUGIN_MAP,
)
print('All imports OK')
print('MOCK_PLUGIN_MAP:', {k: v.__name__ for k, v in MOCK_PLUGIN_MAP.items()})
"
```

Expected: `All imports OK` and `MOCK_PLUGIN_MAP: {'web_search': 'MockWebSearchPlugin'}`

- [ ] **Step 4: Quick smoke test — verify build_test_task_system() creates working TaskSystem**

```bash
python -c "
import asyncio
from Module.task_system.tests.helpers import build_test_task_system

async def main():
    ts = await build_test_task_system(plugins=None)  # default [TimePlugin]
    print(f'TaskSystem started: {ts.is_running}')
    print(f'Tools loaded: {list(ts.infra.tool_registry._plugins.keys())}')
    await ts.stop()
    print('TaskSystem stopped OK')

asyncio.run(main())
"
```

Expected: TaskSystem starts and stops cleanly. Tools loaded includes `get_time`.

- [ ] **Step 5: Commit verification results**

If all checks pass, no commit needed (verification task). If any fix was needed, commit the fix.

---

## Verification Summary

After all tasks complete, run:

```bash
# Full integration test suite
python -m pytest Module/task_system/tests/integration/ -v --timeout=300

# Verify no .env loading code in non-planner test files
grep -n "_env_path = Path" Module/task_system/tests/integration/test_e2e_mvp.py \
  Module/task_system/tests/integration/test_task_system_lifecycle.py \
  Module/task_system/tests/integration/test_executor.py \
  Module/task_system/tests/integration/test_delegation_preparer.py
# Expected: no matches

# Verify helpers import
python -c "from Module.task_system.tests.helpers import build_test_task_system, get_tools_from_manager, mock_replan_response, MOCK_PLUGIN_MAP; print('OK')"
```
