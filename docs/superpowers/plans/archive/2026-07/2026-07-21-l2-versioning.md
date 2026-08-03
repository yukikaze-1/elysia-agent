# L2 Self 版本化 — LLM 编辑模式 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 L3→L2 压缩升级为 LLM 编辑模式 + 版本历史永久存储到独立 MongoDB 集合。

**Architecture:** 新增 `CompressionTrigger` StrEnum + `CharacterVersionRepository`（MongoDB `character_l2_versions` 集合）。重写 `PersonalityCompression.j2` 让 LLM 以"编辑者"角色基于当前 L2 进行增量编辑，输出结构化 `<new_self>` + `<changes>`。`_compress()` 接受 `trigger` 参数区分常规/Flush/手动。debug 面板可浏览全版本历史并回滚。

**Tech Stack:** Python 3.11+, Jinja2, MongoDB (motor), IsoTimestamp, StrEnum

## Global Constraints

- `CharacterState.self_l2: str` 不变，历史在独立集合
- `_should_compress()` 触发条件不变（5 条 or 7 天）
- `VersionEntry` 使用 TypedDict（非 Pydantic BaseModel）
- `CompressionTrigger` 使用 StrEnum
- 版本号从 1 开始递增，永不重置
- `version_repo` 为 None 时降级：照常更新 `self_l2`，不写历史，打 WARNING
- Spec: `docs/superpowers/specs/2026-07-21-l2-versioning-design.md`

---

### Task 1: Schema types — CompressionTrigger + VersionEntry

**Files:**
- Create: `core/schema/compression.py`

**Interfaces:**
- Produces: `CompressionTrigger(StrEnum)` — enum 值: `"compression"`, `"flush"`, `"manual"`
- Produces: `VersionEntry(TypedDict)` — keys: `session_id: str`, `version: int`, `text: str`, `timestamp: str`, `trigger: str`, `change_summary: list[str]`

- [ ] **Step 1: Create core/schema/compression.py**

```python
"""CompressionTrigger + VersionEntry — L3→L2 压缩版本管理类型。"""
from enum import StrEnum
from typing import TypedDict


class CompressionTrigger(StrEnum):
    COMPRESSION = "compression"
    FLUSH = "flush"
    MANUAL = "manual"


class VersionEntry(TypedDict):
    session_id: str
    version: int
    text: str
    timestamp: str       # IsoTimestamp
    trigger: str          # CompressionTrigger.value
    change_summary: list[str]
```

- [ ] **Step 2: Add test for CompressionTrigger enum values**

Create `core/schema/tests/test_compression.py`:

```python
"""CompressionTrigger + VersionEntry 类型测试。"""
import pytest


@pytest.mark.unit
class TestCompressionTrigger:
    def test_values(self):
        from core.schema.compression import CompressionTrigger
        assert CompressionTrigger.COMPRESSION == "compression"
        assert CompressionTrigger.FLUSH == "flush"
        assert CompressionTrigger.MANUAL == "manual"

    def test_str_compatible(self):
        from core.schema.compression import CompressionTrigger
        assert str(CompressionTrigger.COMPRESSION) == "compression"
        assert isinstance(CompressionTrigger.COMPRESSION, str)


@pytest.mark.unit
class TestVersionEntry:
    def test_typed_dict_shape(self):
        from core.schema.compression import VersionEntry
        entry: VersionEntry = {
            "session_id": "default",
            "version": 1,
            "text": "我是一个测试",
            "timestamp": "2026-07-21T00:00:00.000Z",
            "trigger": "compression",
            "change_summary": ["新增: 测试"],
        }
        assert entry["session_id"] == "default"
        assert entry["version"] == 1
        assert entry["trigger"] == "compression"
```

Run: `python -m pytest core/schema/tests/test_compression.py -v`
Expected: 3 passed

- [ ] **Step 3: Commit**

```bash
git add core/schema/compression.py core/schema/tests/test_compression.py
git commit -m "feat: add CompressionTrigger StrEnum + VersionEntry TypedDict"
```

---

### Task 2: CharacterVersionRepository

**Files:**
- Create: `core/storage/character_version_repo.py`
- Modify: `core/storage/__init__.py`

**Interfaces:**
- Consumes: `DocumentDBBackend`, `SessionId` from `core.schema.types`
- Consumes: `CompressionTrigger`, `VersionEntry` from `core.schema.compression` (Task 1)
- Produces: `CharacterVersionRepository(Repository)` — `append_version()`, `list_versions()`, `get_version()`, `count_versions()`
- Note: `append_version` auto-calculates version as max(version) + 1, or 1 if no documents exist
- Note: `list_versions` sorts by version descending, supports cursor pagination via `before_version`

- [ ] **Step 1: Read existing Repository ABC and session_repo for pattern**

Read `core/storage/base.py` and `core/storage/session_repo.py` for the Repository pattern (connect/health_check/close lifecycle, `DocumentDBBackend` usage, `_validate_collection` convention).

- [ ] **Step 2: Create core/storage/character_version_repo.py**

```python
"""CharacterVersionRepository — MongoDB character_l2_versions 集合。"""
import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger, VersionEntry
from .base import Repository

if TYPE_CHECKING:
    from core.schema.types import IsoTimestamp

logger = logging.getLogger(__name__)
COLLECTION = "character_l2_versions"


class CharacterVersionRepository(Repository):
    """L2 Self 版本历史存储 — append-only, 永久保留。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [
                ("session_id", 1), ("version", -1),
            ])
            logger.info("CharacterVersionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("CharacterVersionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    # ---- core API ----

    async def append_version(
        self, session_id: SessionId, text: str,
        timestamp: "IsoTimestamp", trigger: CompressionTrigger,
        change_summary: list[str],
    ) -> int:
        """追加一个版本。自动计算 version 序号（max+1）。返回新版本号。"""
        if self._db is None:
            return 0
        # 计算下一个版本号
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id},
            sort=[("version", -1)],
            limit=1,
        )
        next_version = (docs[0]["version"] + 1) if docs else 1
        doc = {
            "session_id": session_id,
            "version": next_version,
            "text": text,
            "timestamp": timestamp,
            "trigger": trigger.value,
            "change_summary": change_summary,
        }
        await self._db.insert_one(COLLECTION, doc)
        return next_version

    async def list_versions(
        self, session_id: SessionId, *,
        limit: int = 20, before_version: int | None = None,
    ) -> list[VersionEntry]:
        """分页查询版本历史。默认返回最近 20 条。"""
        if self._db is None:
            return []
        filter_expr: dict = {"session_id": session_id}
        if before_version is not None:
            filter_expr["version"] = {"$lt": before_version}
        docs = await self._db.find(
            COLLECTION, filter_expr,
            sort=[("version", -1)], limit=limit,
        )
        return [
            VersionEntry(
                session_id=doc["session_id"],
                version=doc["version"],
                text=doc["text"],
                timestamp=doc["timestamp"],
                trigger=doc["trigger"],
                change_summary=doc.get("change_summary", []),
            )
            for doc in docs
        ]

    async def get_version(
        self, session_id: SessionId, version: int,
    ) -> VersionEntry | None:
        """获取指定版本。"""
        if self._db is None:
            return None
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "version": version},
            limit=1,
        )
        if not docs:
            return None
        doc = docs[0]
        return VersionEntry(
            session_id=doc["session_id"],
            version=doc["version"],
            text=doc["text"],
            timestamp=doc["timestamp"],
            trigger=doc["trigger"],
            change_summary=doc.get("change_summary", []),
        )

    async def count_versions(self, session_id: SessionId) -> int:
        """返回版本总数。"""
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {"session_id": session_id})
```

- [ ] **Step 3: Update core/storage/__init__.py**

Add after the existing exports:

```python
from core.storage.character_version_repo import CharacterVersionRepository  # noqa: F401
```

And add to the docstring header comment:

```
#   character_version_repo — CharacterVersionRepository（MongoDB character_l2_versions 集合）
```

- [ ] **Step 4: Write service tests**

Create `core/storage/tests/test_character_version_repo.py`:

```python
"""CharacterVersionRepository 服务测试（需要 MongoDB）。"""
import pytest
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger


@pytest.mark.service
class TestCharacterVersionRepo:
    @pytest.fixture
    async def repo(self):
        from core.storage.character_version_repo import CharacterVersionRepository
        r = CharacterVersionRepository()
        await r.connect()
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_and_get(self, repo):
        sid = SessionId("test-append")
        v = await repo.append_version(
            session_id=sid, text="我是一个测试",
            timestamp="2026-07-21T00:00:00.000Z",
            trigger=CompressionTrigger.COMPRESSION,
            change_summary=["新增: 测试条目"],
        )
        assert v == 1
        entry = await repo.get_version(sid, 1)
        assert entry is not None
        assert entry["text"] == "我是一个测试"
        assert entry["trigger"] == "compression"
        assert entry["change_summary"] == ["新增: 测试条目"]

    @pytest.mark.asyncio
    async def test_append_auto_increment(self, repo):
        sid = SessionId("test-increment")
        v1 = await repo.append_version(
            session_id=sid, text="v1",
            timestamp="2026-07-21T00:00:00.000Z",
            trigger=CompressionTrigger.COMPRESSION,
            change_summary=[],
        )
        v2 = await repo.append_version(
            session_id=sid, text="v2",
            timestamp="2026-07-21T01:00:00.000Z",
            trigger=CompressionTrigger.FLUSH,
            change_summary=["新增: v2"],
        )
        assert v1 == 1
        assert v2 == 2

    @pytest.mark.asyncio
    async def test_list_versions_pagination(self, repo):
        sid = SessionId("test-list")
        for i in range(5):
            await repo.append_version(
                session_id=sid, text=f"版本{i}",
                timestamp=f"2026-07-21T0{i}:00:00.000Z",
                trigger=CompressionTrigger.COMPRESSION,
                change_summary=[f"改动{i}"],
            )
        # 默认返回最近 20 条
        versions = await repo.list_versions(sid)
        assert len(versions) == 5
        assert versions[0]["version"] == 5  # 最新在前
        assert versions[4]["version"] == 1

        # before_version 分页
        page2 = await repo.list_versions(sid, before_version=3)
        assert len(page2) == 2
        assert page2[0]["version"] == 2
        assert page2[1]["version"] == 1

    @pytest.mark.asyncio
    async def test_count(self, repo):
        sid = SessionId("test-count")
        for i in range(3):
            await repo.append_version(
                session_id=sid, text=f"版本{i}",
                timestamp=f"2026-07-21T0{i}:00:00.000Z",
                trigger=CompressionTrigger.COMPRESSION,
                change_summary=[],
            )
        assert await repo.count_versions(sid) == 3

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo):
        result = await repo.get_version(SessionId("no-such"), 999)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_list(self, repo):
        versions = await repo.list_versions(SessionId("no-such"))
        assert versions == []
```

Run: `python -m pytest core/storage/tests/test_character_version_repo.py -v --tb=short`
Expected: 6 passed (requires MongoDB running)

- [ ] **Step 5: Commit**

```bash
git add core/storage/character_version_repo.py core/storage/__init__.py core/storage/tests/test_character_version_repo.py
git commit -m "feat: add CharacterVersionRepository for L2 version history"
```

---

### Task 3: Rewrite PersonalityCompression.j2

**Files:**
- Modify: `prompt/PersonalityCompression.j2` (full rewrite)

**Interfaces:**
- Consumes: `CompressionSystemPrompt(character_name, l1_core)` — macro name unchanged from current
- Consumes: `CompressionUserPrompt(character_name, current_self, recent_changes)` — macro name unchanged, drops `l3_current` param
- Note: The `_compress()` render call uses these macro names. Keep them the same to minimize engine changes.

- [ ] **Step 1: Rewrite the template**

Replace the entire content of `prompt/PersonalityCompression.j2`:

```jinja2
{# PersonalityCompression — L3 State → L2 Self 长期自我压缩（编辑模式） #}

{%- macro CompressionSystemPrompt(character_name, l1_core) -%}
你是 {{ character_name }} 的自我认知编辑者。你维护一份文档来描述"我在变成什么样"。

# 硬约束：不可违背的底色

<immutable_core>
{{ l1_core }}
</immutable_core>

任何自我描述都不得与上述底色矛盾。

# 编辑原则

1. 你收到当前版本的文档 + 最近的状态变化轨迹
2. 编辑这份文档：保留仍然准确的描述，修正已经变化的，添加新出现的特质或倾向，删除或弱化不再适用的
3. 可以保守——不确定的事宁可保留原样
4. 以第一人称写，1-3 句
5. 不要用"作为一个AI"之类的第三人称
6. 真实的自我认知可以是不确定的、矛盾的
{%- endmacro -%}


{%- macro CompressionUserPrompt(character_name, current_self, recent_changes) -%}
{%- if current_self -%}
<current_self>
{{ current_self }}
</current_self>
{%- endif -%}

<recent_changes>
{%- for entry in recent_changes %}
- {{ entry.content }}
{%- endfor -%}
</recent_changes>

请基于以上信息：
1. 编辑你的自我认知文档，返回完整的新版本
2. 列出你做的具体改动

输出格式：
<new_self>
（新的自我认知文档全文）
</new_self>
<changes>
- 新增: xxx
- 强化: xxx
- 移除: xxx
- 修正: xxx
</changes>
{%- endmacro -%}
```

- [ ] **Step 2: Verify the old template backup exists in git**

Run: `git show HEAD:prompt/PersonalityCompression.j2 | head -5`
Expected: shows old template content

- [ ] **Step 3: Commit**

```bash
git add prompt/PersonalityCompression.j2
git commit -m "refactor: rewrite CompressionPrompt — LLM editor mode with structured output

- Change LLM role from 'feeling/writing' to 'editing a document'
- Require structured output: <new_self> + <changes> tags
- Drop l3_current param (recent_changes already covers state)
- Keep macro names unchanged for backward compatibility"
```

---

### Task 4: Engine changes — _compress, _extract_tag, trigger, version_repo

**Files:**
- Modify: `core/personality/engine.py:119-152` (__init__), `core/personality/engine.py:266-330` (_compress), `core/personality/engine.py:342-360` (flush_compression)

**Interfaces:**
- Consumes: `CompressionTrigger`, `VersionEntry` from `core.schema.compression` (Task 1)
- Consumes: `CharacterVersionRepository` from `core.storage` (Task 2)
- Consumes: Rewritten `PersonalityCompression.j2` (Task 3)
- Produces: `_compress(trigger=COMPRESSION)` with `trigger` parameter
- Produces: `_extract_tag(text, tag) -> str` module-level helper
- Produces: `flush_compression()` calls `_compress(trigger=FLUSH)`

- [ ] **Step 1: Add _extract_tag helper function**

Add before the `class CharacterEngine:` line (around line 97):

```python
def _extract_tag(text: str, tag: str) -> str:
    """从 XML 标签中提取内容。"""
    import re
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""
```

- [ ] **Step 2: Add version_repo parameter to __init__**

Add `version_repo` to `__init__` parameters (after `prompt_dir: str = "prompt"`):

```python
        version_repo: CharacterVersionRepository | None = None,
```

Add the instance attribute AFTER the try/except block (after line 135, at 8-space indent — outside the except block, before "子组件: CharacterReflector" comment):

```python
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None

        self._version_repo: CharacterVersionRepository | None = version_repo

        # 子组件: CharacterReflector
```

- [ ] **Step 3: Add import for CompressionTrigger + now_iso**

Add to engine.py imports (after existing `from core.schema.mood import ...` if still present, or near the other `core.schema` imports):

```python
from core.schema.compression import CompressionTrigger
from core.schema._utils import now_iso
```

- [ ] **Step 4: Rewrite _compress()**

Replace the current `_compress()` method (lines 266-330) with:

```python
    async def _compress(self, trigger: CompressionTrigger = CompressionTrigger.COMPRESSION) -> None:
        """执行 L3→L2 压缩：渲染模板 → 调 LLM → 解析 <new_self> + <changes> → 写版本历史。"""
        if self._pm is None:
            return

        current_self = self.state.self_l2
        new_entries = self._get_new_entries()
        history = self.state.state_history

        logger.info("CharacterEngine: 开始 L3→L2 压缩 (%d 新条目, 当前 L2 %d chars)",
                    len(new_entries), len(current_self))

        try:
            system = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionSystemPrompt",
                character_name=self._character_name,
                l1_core=self._l1_core,
            )
            user_prompt = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionUserPrompt",
                character_name=self._character_name,
                current_self=current_self,
                recent_changes=new_entries,
            )
        except Exception:
            logger.warning("CharacterEngine: 压缩模板渲染失败", exc_info=True)
            return

        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("CharacterEngine: 压缩 LLM 调用失败 — 下次重试", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        new_self = _extract_tag(text, "new_self")
        if not new_self:
            logger.warning("CharacterEngine: 压缩 LLM 返回无 <new_self>")
            # 仍更新追踪，避免重复在同一批条目上重试
            self.state.compression_last_at = time.time()
            self.state.compression_last_count = len(history)
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [c.strip("- ") for c in changes_raw.strip().split("\n") if c.strip()]

        # 更新 L2
        self.state.self_l2 = new_self
        self.state.compression_last_at = time.time()
        self.state.compression_last_count = len(history)

        logger.info("CharacterEngine: L3→L2 压缩完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_self), elapsed_ms, len(changes))

        # 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self.state.session_id,
                text=new_self,
                timestamp=now_iso(),
                trigger=trigger,
                change_summary=changes,
            )
        else:
            logger.warning("CharacterEngine: version_repo 未配置，版本历史未保存")
```

- [ ] **Step 5: Update flush_compression()**

Replace the `_compress()` call inside `flush_compression()`:

```python
    # Before (line ~358):
    await asyncio.wait_for(self._compress(), timeout=5.0)
    # After:
    await asyncio.wait_for(
        self._compress(trigger=CompressionTrigger.FLUSH), timeout=5.0)
```

- [ ] **Step 6: Write unit test for _extract_tag**

Add to `core/tests/test_mood_engine.py` (or create a new test file):

```python
@pytest.mark.unit
class TestExtractTag:
    def test_normal_extraction(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self>我是测试</new_self>", "new_self")
        assert result == "我是测试"

    def test_multiline(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self>\n第一行\n第二行\n</new_self>", "new_self")
        assert result == "第一行\n第二行"

    def test_tag_not_found(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("没有标签", "new_self")
        assert result == ""

    def test_empty_tag(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self></new_self>", "new_self")
        assert result == ""

    def test_whitespace_trimmed(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self>  \n  内容  \n  </new_self>", "new_self")
        assert result == "内容"
```

Run: `python -m pytest core/tests/test_mood_engine.py::TestExtractTag core/personality/tests/ -v --tb=short`
Expected: 5 new tests pass + existing personality tests pass

- [ ] **Step 7: Commit**

```bash
git add core/personality/engine.py core/tests/test_mood_engine.py
git commit -m "feat: rewrite _compress with LLM editor mode + version history

- _compress accepts trigger parameter (default COMPRESSION)
- Parse <new_self> and <changes> from LLM response
- Write version history via CharacterVersionRepository
- _extract_tag() helper for XML tag extraction
- flush_compression uses FLUSH trigger
- Fallback: version_repo=None still updates self_l2 without history"
```

---

### Task 5: Wire up in Agent.__init__

**Files:**
- Modify: `core/agent/agent.py:112-120`

- [ ] **Step 1: Add CharacterVersionRepository to Agent.__init__**

```python
# In Agent.__init__, after creating other repos (e.g., self._session_repo):
from core.storage.character_version_repo import CharacterVersionRepository
self._version_repo = CharacterVersionRepository()

# In CharacterEngine construction, add:
        self._character_engine = CharacterEngine(
            llm=self._llm,
            state=CharacterState(session_id=SessionId("")),
            character_name=self._character.name,
            user_name=self._user_name,
            base_personality=self._character.base_personality,
            character_config=self._character,
            output=self._output,
            version_repo=self._version_repo,              # <-- 新增
        )
```

- [ ] **Step 2: Add connect/close to lifecycle**

In `AgentLifecycle._start_background()` or wherever repos are connected, add:

```python
                if not await self._version_repo.connect():
                    logger.warning("CharacterVersionRepository 离线")
```

In `AgentLifecycle._flush_all()` or wherever repos are closed, add:

```python
            await self._version_repo.close()
```

- [ ] **Step 3: Commit**

```bash
git add core/agent/agent.py core/agent/lifecycle.py
git commit -m "feat: wire CharacterVersionRepository into Agent lifecycle"
```

---

### Task 6: Debug panel — version history API + aggregation

**Files:**
- Modify: `core/debug/info.py:277-281` (mood section area)
- Modify: `core/input/debug_routes.py` (add 2 new endpoints)

- [ ] **Step 1: Add version count to debug info**

In `core/debug/info.py`, after the CharacterEngine section (around line 293):

```python
    # ---- L2 Version History ----
    try:
        info.l2_version_count = 0
        engine = getattr(agent, "_character_engine", None)
        if engine and getattr(engine, "_version_repo", None):
            info.l2_version_count = await engine._version_repo.count_versions(
                agent._session.session_id
            )
    except Exception:
        info.l2_version_count = 0
```

Note: The `DebugInfo` dataclass needs `l2_version_count: int = 0` added to its fields.

- [ ] **Step 2: Locate DebugInfo dataclass and add field**

Find the `DebugInfo` class definition (likely in `core/debug/info.py` or a types file) and add:
```python
    l2_version_count: int = 0
```

- [ ] **Step 3: Add GET /debug/api/l2-versions endpoint**

In `debug_routes.py`, add a new handler in `register_debug_routes()`:

```python
    async def l2_versions_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        engine = getattr(agent, "_character_engine", None)
        if not engine or not getattr(engine, "_version_repo", None):
            return web.json_response({"error": "版本历史不可用"}, status=503)

        limit = int(request.query.get("limit", "20"))
        before = request.query.get("before")
        before_version = int(before) if before else None

        sid = agent._session.session_id
        versions = await engine._version_repo.list_versions(
            sid, limit=limit, before_version=before_version,
        )
        total = await engine._version_repo.count_versions(sid)

        return web.json_response({
            "versions": versions,
            "total": total,
            "has_more": len(versions) == limit and versions[-1]["version"] > 1,
        })

    app.router.add_get("/debug/api/l2-versions", l2_versions_handler)
```

- [ ] **Step 4: Add POST /debug/api/l2-rollback endpoint**

```python
    async def l2_rollback_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        engine = getattr(agent, "_character_engine", None)
        if not engine or not getattr(engine, "_version_repo", None):
            return web.json_response({"error": "版本历史不可用"}, status=503)

        try:
            body = await request.json()
            target_version = int(body["version"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "缺少 version 或格式错误"}, status=400)

        sid = agent._session.session_id
        entry = await engine._version_repo.get_version(sid, target_version)
        if entry is None:
            return web.json_response({"error": f"版本 {target_version} 不存在"}, status=404)

        # 回滚：设置 self_l2 → 追加一条 MANUAL 版本
        from core.schema._utils import now_iso
        from core.schema.compression import CompressionTrigger
        engine.state.self_l2 = entry["text"]
        new_version = await engine._version_repo.append_version(
            session_id=sid,
            text=entry["text"],
            timestamp=now_iso(),
            trigger=CompressionTrigger.MANUAL,
            change_summary=[f"回滚到版本 {target_version}"],
        )

        return web.json_response({
            "status": "ok",
            "new_version": new_version,
            "text": entry["text"],
        })

    app.router.add_post("/debug/api/l2-rollback", l2_rollback_handler)
```

- [ ] **Step 5: Commit**

```bash
git add core/debug/info.py core/input/debug_routes.py
git commit -m "feat: add L2 version history API and rollback endpoint"
```

---

### Task 7: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run full quick suite**

Run: `make test-quick`
Expected: 1649+ passed, 0 regressions (new tests added)

- [ ] **Step 2: Verify imports work standalone**

Run: `python -c "from core.schema.compression import CompressionTrigger, VersionEntry; print('OK')"`
Expected: `OK`

Run: `python -c "from core.storage.character_version_repo import CharacterVersionRepository; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Check for stray references**

Run: `grep -rn "PersonalityCompressionSystemPrompt\|PersonalityCompressionUserPrompt" --include="*.py" | grep -v __pycache__`
Expected: no results (macro names renamed from old naming convention)

- [ ] **Step 4: Update todo.md**

Mark "L3→L2 梯度压缩自动化" as ✅ with note about trigger mechanism pending.
Add a new todo: "L3→L2 触发机制 — 从批触发改为连续增量（每条 L3 条目实时评估）"

- [ ] **Step 5: Run personality tests specifically**

Run: `python -m pytest core/personality/tests/ -v --tb=short`
Expected: all pass

- [ ] **Step 6: Commit if all clean**
