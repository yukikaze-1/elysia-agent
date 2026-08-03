# Character Section 动态版本化 — 通用基础设施 + C6 首发

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `CharacterVersionRepository` 升级为通用 section 版本存储，C6 SpeakingStyle 作为首个非 L2 接入方，实现 L2 演化 → C6 自动刷新联动。

**Architecture:** 4 层改动 — (1) schema 层：CompressionTrigger + VersionEntry + CharacterState 扩展，(2) storage 层：repo 通用化 + content_type，(3) engine 层：_refresh_c6() 编辑模式，(4) 渲染层：C6 模板条件分支 + section + PromptAssembler 接线。

**Tech Stack:** Python 3.11+, Jinja2, MongoDB (motor), re (regex for XML tag extraction)

## Global Constraints

- 所有新增方法加 `content_type: str = "l2"` 保持向后兼容
- `append_version()` 版本号计算必须加 `content_type` 过滤（L2/C6 独立计数）
- `VersionEntry` TypedDict 必须新增 `content_type: str` 字段
- 旧文档（无 `content_type` 字段）查询时用 `$or` 兼容
- `_refresh_c6()` 失败不阻塞主流程（WARN + 保留旧值）
- L2 rollback 后触发 `_refresh_c6()` 确保 C6 与 L2 一致
- C6 模板首次渲染时 `speaking_style_c6` 由 `set_context(kwargs)` 注入
- Debug API 旧路由通过函数委托保持兼容（不发起额外 HTTP 请求）

---

### Task 1: Schema — CompressionTrigger + VersionEntry + CharacterState 扩展

**Files:**
- Modify: `core/schema/compression.py:6-18`
- Modify: `core/schema/character.py:16-30`
- Modify: `core/schema/__init__.py`
- Test: `core/schema/tests/test_compression.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `CompressionTrigger.C6_REFRESH`, `VersionEntry.content_type: str`, `CharacterState.speaking_style_c6: str`

- [ ] **Step 1: Write unit tests for schema additions**

```python
# core/schema/tests/test_compression.py — 追加在文件末尾

def test_c6_refresh_trigger_exists():
    """C6_REFRESH 枚举值存在。"""
    from core.schema.compression import CompressionTrigger
    assert hasattr(CompressionTrigger, "C6_REFRESH")
    assert CompressionTrigger.C6_REFRESH == "c6_refresh"


def test_version_entry_content_type():
    """VersionEntry 包含 content_type 字段。"""
    # TypedDict 在运行时仅通过 __annotations__ 验证
    from core.schema.compression import VersionEntry
    assert "content_type" in VersionEntry.__annotations__
```

```python
# core/schema/tests/test_character.py — 新建文件

"""CharacterState 新增字段的单元测试。"""


def test_speaking_style_c6_default():
    """新字段默认值为空串。"""
    from core.schema.character import CharacterState
    s = CharacterState(session_id="test")
    assert s.speaking_style_c6 == ""


def test_speaking_style_c6_assignable():
    """字段可赋值和持久化。"""
    from core.schema.character import CharacterState
    s = CharacterState(session_id="test", speaking_style_c6="新的说话风格")
    assert s.speaking_style_c6 == "新的说话风格"
    # 验证序列化往返
    data = s.model_dump()
    assert data["speaking_style_c6"] == "新的说话风格"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest core/schema/tests/test_compression.py::test_c6_refresh_trigger_exists core/schema/tests/test_compression.py::test_version_entry_content_type -v`
Expected: FAIL

Run: `pytest core/schema/tests/test_character.py -v`
Expected: FAIL (file not found or fields missing)

- [ ] **Step 3: Implement schema changes**

```python
# core/schema/compression.py

class CompressionTrigger(StrEnum):
    COMPRESSION = "compression"
    FLUSH = "flush"
    MANUAL = "manual"
    C6_REFRESH = "c6_refresh"       # L2 更新后 C6 自动刷新


class VersionEntry(TypedDict):
    session_id: str
    version: int
    text: str
    timestamp: str       # IsoTimestamp
    trigger: str          # CompressionTrigger.value
    change_summary: list[str]
    content_type: str     # "l2" | "c6" — 版本所属的 content 类型
```

```python
# core/schema/character.py — 在 mood_intensity 字段后添加

class CharacterState(BaseModel):
    # ... 现有 12 个字段不变 ...
    speaking_style_c6: str = ""  # C6 动态文本。空串 = 使用 L1 默认模板
```

```python
# core/schema/__init__.py — 确认 CompressionTrigger, VersionEntry 已在导出列表中
# （当前应该已有 import，只需确认 speaking_style_c6 通过 CharacterState 的导出即可。
#  如果是 from core.schema.character import CharacterState，则无需额外改动）
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest core/schema/tests/test_compression.py core/schema/tests/test_character.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/schema/compression.py core/schema/character.py core/schema/__init__.py core/schema/tests/test_compression.py core/schema/tests/test_character.py
git commit -m "feat: add C6_REFRESH trigger, content_type to VersionEntry, speaking_style_c6 to CharacterState"
```

---

### Task 2: CharacterVersionRepository — 通用化 + content_type

**Files:**
- Modify: `core/storage/character_version_repo.py` (整个文件)
- Test: `core/storage/tests/test_character_version_repo.py`

**⚠️ 部署前注意**：集合从 `character_l2_versions` 改名为 `character_versions`，旧数据不会自动迁移。上线后需手动清空/删除旧集合（`db.character_l2_versions.drop()`）。L2 版本历史从零开始。

**Interfaces:**
- Consumes: `CompressionTrigger.C6_REFRESH`, `VersionEntry.content_type` (Task 1)
- Produces: 所有方法加 `content_type` 参数；`append_version` 版本号按 content_type 独立计算

- [ ] **Step 1: Write extension tests**

```python
# core/storage/tests/test_character_version_repo.py — 追加以下测试

@pytest.mark.service
class TestCharacterVersionRepoContentType:
    """content_type 过滤和独立计数。"""

    async def test_append_version_c6_type(self, repo):
        """c6 类型 append → version=1（独立于 L2）。"""
        v = await repo.append_version(
            session_id="test_ct", text="C6 text",
            timestamp="2026-07-22T00:00:00+08:00",
            trigger=CompressionTrigger.C6_REFRESH,
            change_summary=["调整"], content_type="c6",
        )
        assert v == 1

    async def test_l2_c6_independent_versions(self, repo):
        """L2 和 C6 版本号互不干扰。"""
        sid = "test_independent"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [],
                                  content_type="l2")
        v_c6 = await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                         CompressionTrigger.C6_REFRESH, [],
                                         content_type="c6")
        assert v_c6 == 1  # C6 从 1 开始
        v_l2_2 = await repo.append_version(sid, "l2_v2", "2026-07-22T00:00:00+08:00",
                                            CompressionTrigger.COMPRESSION, [],
                                            content_type="l2")
        assert v_l2_2 == 2  # L2 继续增长
        v_c6_2 = await repo.append_version(sid, "c6_v2", "2026-07-22T00:00:00+08:00",
                                            CompressionTrigger.C6_REFRESH, [],
                                            content_type="c6")
        assert v_c6_2 == 2  # C6 也独立增长

    async def test_list_versions_filtered_by_type(self, repo):
        """content_type 过滤：只返回匹配类型的版本。"""
        sid = "test_filter_type"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [], content_type="l2")
        await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")

        l2_versions = await repo.list_versions(sid, content_type="l2")
        assert len(l2_versions) == 1
        assert l2_versions[0]["content_type"] == "l2"

        c6_versions = await repo.list_versions(sid, content_type="c6")
        assert len(c6_versions) == 1
        assert c6_versions[0]["content_type"] == "c6"

    async def test_count_versions_by_type(self, repo):
        """count_versions 按 content_type 分别计数。"""
        sid = "test_count_by_type"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [], content_type="l2")
        await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")
        await repo.append_version(sid, "c6_v2", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")

        assert await repo.count_versions(sid, content_type="l2") == 1
        assert await repo.count_versions(sid, content_type="c6") == 2

    async def test_default_content_type_is_l2(self, repo):
        """默认 content_type="l2" 向后兼容。"""
        sid = "test_default_type"
        v = await repo.append_version(sid, "text", "2026-07-22T00:00:00+08:00",
                                       CompressionTrigger.COMPRESSION, [])
        assert v == 1
        versions = await repo.list_versions(sid)
        assert len(versions) == 1
        assert versions[0].get("content_type", "l2") == "l2"
```

- [ ] **Step 2: Implement repository changes**

Replace the entire `character_version_repo.py`:

```python
"""CharacterVersionRepository — MongoDB character_versions 集合。"""
import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger, VersionEntry
from .base import Repository

if TYPE_CHECKING:
    from core.schema.types import IsoTimestamp

logger = logging.getLogger(__name__)
COLLECTION = "character_versions"       # 改名（原 character_l2_versions）


class CharacterVersionRepository(Repository):
    """角色 Section 版本历史存储 — append-only, 永久保留。
    支持多种 content_type: "l2", "c6", 未来扩展 "c7", "c2" 等。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            # 检查脏数据（重复文档），有重复时降级为非唯一索引
            dup_pipeline = [
                {"$group": {
                    "_id": {"sid": "$session_id", "ct": "$content_type", "v": "$version"},
                    "count": {"$sum": 1},
                }},
                {"$match": {"count": {"$gt": 1}}},
            ]
            try:
                dups = await self._db.aggregate(COLLECTION, dup_pipeline)
            except Exception:
                dups = []  # aggregate 不支持时跳过检查
            unique = len(list(dups)) == 0 if dups else True
            if not unique:
                logger.warning(
                    "CharacterVersionRepository: 发现重复文档，降级为非唯一索引")
            await self._db.ensure_index(COLLECTION, [
                ("session_id", 1), ("content_type", 1), ("version", -1),
            ], unique=unique)
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
        content_type: str = "l2",
    ) -> int:
        """追加一个版本。自动计算 version 序号（per content_type，max+1）。返回新版本号。"""
        if self._db is None:
            return 0
        # 计算下一个版本号 — 必须加 content_type 过滤
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "content_type": content_type},
            sort=[("version", -1)],
            limit=1,
        )
        next_version = (docs[0]["version"] + 1) if docs else 1
        doc = {
            "session_id": session_id,
            "content_type": content_type,
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
        content_type: str = "l2",
    ) -> list[VersionEntry]:
        """分页查询版本历史。默认返回最近 20 条。"""
        if self._db is None:
            return []
        # 兼容旧文档（无 content_type 字段）— 当作 l2
        type_filter: dict = (
            {"content_type": content_type}
            if content_type != "l2"
            else {"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}
        )
        filter_expr: dict = {"session_id": session_id, **type_filter}
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
                content_type=doc.get("content_type", "l2"),
            )
            for doc in docs
        ]

    async def get_version(
        self, session_id: SessionId, version: int,
        content_type: str = "l2",
    ) -> VersionEntry | None:
        """获取指定版本。"""
        if self._db is None:
            return None
        type_filter: dict = (
            {"content_type": content_type}
            if content_type != "l2"
            else {"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}
        )
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "version": version, **type_filter},
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
            content_type=doc.get("content_type", "l2"),
        )

    async def count_versions(
        self, session_id: SessionId,
        content_type: str = "l2",
    ) -> int:
        """返回版本总数。"""
        if self._db is None:
            return 0
        type_filter: dict = (
            {"content_type": content_type}
            if content_type != "l2"
            else {"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}
        )
        return await self._db.count(
            COLLECTION,
            {"session_id": session_id, **type_filter},
        )
```

- [ ] **Step 3: Verify existing tests still pass + new tests pass**

Run: `pytest core/storage/tests/test_character_version_repo.py -v --tb=short`
Expected: all PASS (existing + new)

- [ ] **Step 4: Run full quick tests for regression**

Run: `pytest -m "unit or service" -q --tb=short`
Expected: all PASS (except pre-existing `test_append_and_get` if DB has leftover data)

- [ ] **Step 5: Commit**

```bash
git add core/storage/character_version_repo.py core/storage/tests/test_character_version_repo.py
git commit -m "refactor: generalize CharacterVersionRepository with content_type support"
```

---

### Task 3: SpeakingStyleEdit.j2 — C6 编辑 prompt 模板

**Files:**
- Create: `prompt/SpeakingStyleEdit.j2`

**Interfaces:**
- Consumes: nothing (new file)
- Produces: `render(character_name, self_l2, current_speaking_style)` → system prompt text

- [ ] **Step 1: Create the template**

```jinja2
{# SpeakingStyleEdit — C6 说话风格编辑（L2 更新后触发） #}

你是 {{ character_name }}。你正在审视自己的说话风格。

## 你的当前自我认知（长期自我 L2）
{{ self_l2 }}

## 你当前的说话风格描述
{{ current_speaking_style }}

## 任务
编辑上面的"说话风格描述"，使其与你的当前自我认知（L2）保持一致。

编辑原则：
- 保持原有结构（五模式切换、声线、称呼、♪规则、夸奖、调情、心情影响）
- 只调整与 L2 不一致的部分。如果 L2 说"说话更直接了"，减少神秘模式的描述比重
- 如果 L2 没有显著变化，可以只做微小调整或保持不变
- 不要在风格中添加 L2 没有的内容
- 语调始终是明快、柔和、自信、亲昵。这是不可变的底色

输出格式：
<new_speaking_style>编辑后的完整说话风格描述</new_speaking_style>
<changes>具体的改动说明（每条一行，如果没有改动就写"无变化"）</changes>
```

- [ ] **Step 2: Verify template renders without error**

Run: `python -c "from Module.system_prompt import PromptManager; pm = PromptManager('prompt'); print(pm.render('SpeakingStyleEdit.j2', character_name='Elysia', self_l2='测试', current_speaking_style='测试风格'))"`
Expected: 输出包含 `## 你的当前自我认知` 和 `测试`

- [ ] **Step 3: Commit**

```bash
git add prompt/SpeakingStyleEdit.j2
git commit -m "feat: add SpeakingStyleEdit.j2 prompt for C6 dynamic editing"
```

---

### Task 4: CharacterEngine — _refresh_c6() + render_c6()

**Files:**
- Modify: `core/personality/engine.py:88-99` (add C6Data dataclass), `core/personality/engine.py:280-356` (add _refresh_c6 + modify _compress)
- Test: `core/personality/tests/test_engine.py`

**Interfaces:**
- Consumes: `CompressionTrigger.C6_REFRESH` (Task 1), `CharacterVersionRepository.append_version(content_type="c6")` (Task 2), `SpeakingStyleEdit.j2` (Task 3)
- Produces: `C6Data`, `render_c6() -> C6Data`, `_refresh_c6()`

- [ ] **Step 1: Add C6Data and update imports**

```python
# engine.py — 在 C9Data 之后添加

@dataclass
class C6Data:
    """C6 SpeakingStyle section 渲染数据。"""
    speaking_style_c6: str  # "" 表示使用默认模板
```

```python
# engine.py — 更新 import（在文件顶部现有的 CompressionTrigger import 后确认已导入）

# 确保 CompressionTrigger 已在 import 中（当前已存在）
```

- [ ] **Step 2: Add render_c6() method**

```python
# engine.py — CharacterEngine 类中，render_c9() 之后

def render_c6(self) -> C6Data:
    """产出 C6 SpeakingStyle section 的数据。"""
    return C6Data(speaking_style_c6=self.state.speaking_style_c6)
```

- [ ] **Step 3: Add _refresh_c6() method**

```python
# engine.py — CharacterEngine 类中，_compress() 方法之后

async def _refresh_c6(self) -> None:
    """L2 更新后刷新 C6 说话风格。LLM 编辑模式 + 版本历史。失败不阻塞。"""
    if self._pm is None:
        return

    # 1. 获取当前 C6 文本
    current = self.state.speaking_style_c6
    if not current:
        # 首次：渲染 L1 默认模板获取基准文本
        try:
            from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
            sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")
            sec.set_context(user_name="")
            current = await sec.render()
        except Exception:
            logger.warning("CharacterEngine: C6 默认模板渲染失败，跳过刷新")
            return

    if not current.strip():
        return

    # 2. 渲染 prompt
    try:
        prompt = self._pm.render(
            "SpeakingStyleEdit.j2",
            character_name=self._character_name,
            self_l2=self.state.self_l2,
            current_speaking_style=current,
        )
    except Exception:
        logger.warning("CharacterEngine: C6 prompt 渲染失败", exc_info=True)
        return

    # 3. LLM 调用（与 _compress() 相同模式）
    t0 = time.perf_counter()
    try:
        result = await self._llm.think(
            [{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = result.text.strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("CharacterEngine: C6 refresh LLM 调用失败", exc_info=True)
        return

    # 4. 解析 XML 标签
    new_style = _extract_tag(text, "new_speaking_style")
    if not new_style:
        logger.warning("CharacterEngine: C6 refresh <new_speaking_style> 解析失败")
        return

    changes_raw = _extract_tag(text, "changes")
    changes = [l.strip("- ").strip() for l in (changes_raw or "").splitlines() if l.strip()]

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # 5. 更新 state
    self.state.speaking_style_c6 = new_style

    logger.info("CharacterEngine: C6 刷新完成 (%d chars, %.0f ms, %d 条改动)",
                len(new_style), elapsed_ms, len(changes))

    # 6. 写版本历史
    if self._version_repo is not None:
        await self._version_repo.append_version(
            session_id=self.state.session_id,
            text=new_style,
            timestamp=now_iso(),
            trigger=CompressionTrigger.C6_REFRESH,
            change_summary=changes,
            content_type="c6",
        )
```

- [ ] **Step 4: Modify _compress() to trigger _refresh_c6()**

In `_compress()`, after the version history write block (line ~353), add the C6 refresh call:

```python
# engine.py — _compress() 方法，在 existing write 之后

        # ... 现有代码：写 L2 版本历史 ...
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

        # 新增：触发 C6 刷新
        await self._refresh_c6()
```

- [ ] **Step 5: Write engine tests**

```python
# core/personality/tests/test_engine.py — 追加

class TestC6Refresh:
    """_refresh_c6() 测试。"""

    @pytest.mark.service
    async def test_refresh_c6_generates(self, engine_with_mocks):
        """_refresh_c6() 调用 LLM 并更新 state.speaking_style_c6。"""
        engine = engine_with_mocks
        # 模拟 LLM 返回
        engine._llm.think = AsyncMock(return_value=MagicMock(
            text="<new_speaking_style>编辑后的风格</new_speaking_style>\n<changes>调整了语气</changes>"
        ))
        # 确保 pm 可用
        assert engine._pm is not None
        # 先设一个当前值
        engine.state.speaking_style_c6 = "原始风格"
        engine.state.self_l2 = "测试 L2"

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "编辑后的风格"

    @pytest.mark.service
    async def test_refresh_c6_first_time(self, engine_with_mocks):
        """speaking_style_c6 为空时首次生成。"""
        engine = engine_with_mocks
        engine._llm.think = AsyncMock(return_value=MagicMock(
            text="<new_speaking_style>首次生成的风格</new_speaking_style>\n<changes>无变化</changes>"
        ))
        engine.state.speaking_style_c6 = ""
        engine.state.self_l2 = ""

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "首次生成的风格"

    @pytest.mark.service
    async def test_refresh_c6_llm_empty_no_block(self, engine_with_mocks):
        """LLM 返回空时保留旧值，不抛异常。"""
        engine = engine_with_mocks
        engine._llm.think = AsyncMock(return_value=MagicMock(text=""))
        old = engine.state.speaking_style_c6 = "旧值"

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "旧值"  # 不变

    @pytest.mark.service
    async def test_refresh_c6_parse_failure_no_block(self, engine_with_mocks):
        """<new_speaking_style> 解析失败时保留旧值。"""
        engine = engine_with_mocks
        engine._llm.think = AsyncMock(return_value=MagicMock(
            text="<other_tag>内容</other_tag>"  # 无 <new_speaking_style>
        ))
        old = engine.state.speaking_style_c6 = "旧值"

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "旧值"

    @pytest.mark.service
    async def test_render_c6_returns_data(self, engine_with_mocks):
        """render_c6() 返回正确的 C6Data。"""
        engine = engine_with_mocks
        engine.state.speaking_style_c6 = "动态风格"

        data = engine.render_c6()

        assert data.speaking_style_c6 == "动态风格"

    @pytest.mark.service
    async def test_render_c6_empty_default(self, engine_with_mocks):
        """speaking_style_c6 为空时返回空串。"""
        engine = engine_with_mocks
        engine.state.speaking_style_c6 = ""

        data = engine.render_c6()

        assert data.speaking_style_c6 == ""
```

- [ ] **Step 6: Run engine tests**

Run: `pytest core/personality/tests/test_engine.py -v --tb=short`
Expected: new tests PASS; existing tests PASS (如果现有测试 mock 了 _compress 的完整流程，可能需要调整 mock 以包含 _refresh_c6)

- [ ] **Step 7: Commit**

```bash
git add core/personality/engine.py core/personality/tests/test_engine.py
git commit -m "feat: add _refresh_c6() + render_c6() to CharacterEngine"
```

---

### Task 5: C6 模板 + Section + PromptAssembler 接线

**Files:**
- Modify: `Module/system_prompt/templates/c6_speaking_style.j2` (顶部加条件分支)
- Modify: `Module/system_prompt/sections/speaking_style.py` (set_context + render)
- Modify: `core/prompt_assembler.py:59-72,90-107` (创建 section + 传入数据)

**Interfaces:**
- Consumes: `C6Data` (Task 4), `CharacterState.speaking_style_c6` (Task 1)
- Produces: C6 模板条件渲染，SpeakingStyleSection 接收 speaking_style_c6 参数

- [ ] **Step 1: Update C6 template**

打开 `Module/system_prompt/templates/c6_speaking_style.j2`，做两处插入：

1. 在 `<speaking_style>` 下一行插入：

```jinja2
{% if speaking_style_c6 %}
{{ speaking_style_c6 }}
{% else %}
```

2. 在 `</speaking_style>` 上一行插入：

```jinja2
{% endif %}
```

即整段 L1 默认文本（90+ 行）包在 `{% else %}` 和 `{% endif %}` 之间。文件头部 version 从 `3.0.0` 更新为 `3.1.0`，description 末尾加上"支持 L2 驱动的动态演化"，新增 `@params speaking_style_c6: str` 注释。

- [ ] **Step 2: Update SpeakingStyleSection**

```python
# Module/system_prompt/sections/speaking_style.py

class SpeakingStyleSection(Jinja2Section):
    name = "SpeakingStyle"
    version = "3.1.0"
    description = "回复风格：五模式切换，支持 L2 驱动的动态演化"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""
        self._speaking_style_c6 = ""    # 新增

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._speaking_style_c6 = kwargs.get("speaking_style_c6", "")

    async def render(self) -> str:
        return self._render_template(
            user_name=self._user_name,
            speaking_style_c6=self._speaking_style_c6,
        )
```

- [ ] **Step 3: Update PromptAssembler**

```python
# core/prompt_assembler.py — build_system_prompt() 方法中

    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                                   stt_emotion: str = "", stt_events=None,
                                   tts_provider: str = "") -> str:
        # 从 CharacterEngine 获取当前 mood
        mood = "neutral"
        speaking_style_c6 = ""        # 新增
        if self._personality_engine is not None:
            mood = self._personality_engine.state.mood
            c6 = self._personality_engine.render_c6()           # 新增
            speaking_style_c6 = c6.speaking_style_c6            # 新增
        self._manager.set_context(
            user_message=user_message,
            user_name=user_name,
            user_pronoun=self._character.user_pronoun,
            stt_emotion=stt_emotion,
            stt_events=stt_events or [],
            tts_provider=tts_provider,
            mood=mood,
            speaking_style_c6=speaking_style_c6,                # 新增
        )
        self._refresh_personality_data()
        return await self._manager.render()
```

- [ ] **Step 4: Write system prompt section tests**

```python
# Module/system_prompt/tests/ — 追加到现有 section 测试文件

@pytest.mark.service
class TestSpeakingStyleSectionDynamic:
    async def test_fallback_to_default_template(self):
        """speaking_style_c6 为空时渲染 L1 默认模板。"""
        sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")
        sec.set_context(user_name="妖梦", speaking_style_c6="")
        result = await sec.render()
        assert "五模式切换" in result          # L1 默认内容
        assert "轻松模式" in result

    async def test_render_dynamic_text(self):
        """speaking_style_c6 非空时渲染动态文本。"""
        sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")
        sec.set_context(user_name="妖梦", speaking_style_c6="自定义说话风格文本")
        result = await sec.render()
        assert "自定义说话风格文本" in result
        assert "五模式切换" not in result     # 不使用 L1 默认
```

- [ ] **Step 5: Run system prompt tests**

Run: `pytest Module/system_prompt/tests/ -v --tb=short`
Expected: all PASS (70 existing + new)

- [ ] **Step 6: Commit**

```bash
git add Module/system_prompt/templates/c6_speaking_style.j2 Module/system_prompt/sections/speaking_style.py core/prompt_assembler.py Module/system_prompt/tests/
git commit -m "feat: wire C6 dynamic text through template + section + PromptAssembler"
```

---

### Task 6: Debug 面板 — 通用版本 API + C6 浏览器

**Files:**
- Modify: `core/debug/info.py:77,211-218`
- Modify: `core/input/debug_routes.py:428-485`
- Modify: `static/debug.html:783,1210-1275`

**Interfaces:**
- Consumes: `CharacterVersionRepository.count_versions(content_type="c6")` (Task 2)
- Produces: `/debug/api/character-versions?type=c6`, `/debug/api/character-rollback`, 前端 C6 版本浏览器

- [ ] **Step 1: Update DebugInfo**

```python
# core/debug/info.py — 在 l2_version_count 后添加

@dataclass
class DebugInfo:
    # ... existing fields ...
    l2_version_count: int = 0
    c6_version_count: int = 0      # 新增：C6 版本历史总数
```

```python
# core/debug/info.py — collect() 中，l2_version_count 的获取代码后追加

        # L2 版本数 (content_type="l2", 默认兼容)
        try:
            info.l2_version_count = await engine._version_repo.count_versions(
                sid, content_type="l2")
        except Exception:
            info.l2_version_count = 0

        # C6 版本数
        try:
            info.c6_version_count = await engine._version_repo.count_versions(
                sid, content_type="c6")
        except Exception:
            info.c6_version_count = 0
```

- [ ] **Step 2: Add generalized debug API routes**

```python
# core/input/debug_routes.py — 替换 L2 versions handler 段（lines 426-485）

# 提取通用版本查询函数
async def _list_character_versions(agent, content_type: str,
                                    limit: int, before_version: int | None):
    engine = getattr(agent, "_character_engine", None)
    if not engine or not getattr(engine, "_version_repo", None):
        return None, "版本历史不可用", 503
    sid = agent._session.session_id
    versions = await engine._version_repo.list_versions(
        sid, limit=limit, before_version=before_version,
        content_type=content_type,
    )
    total = await engine._version_repo.count_versions(sid, content_type=content_type)
    return {
        "versions": versions,
        "total": total,
        "has_more": len(versions) == limit and versions[-1]["version"] > 1,
    }, None, 200

# 确认 debug_routes.py 顶部已有: from core.schema._utils import now_iso
# 若没有，需添加

async def _character_rollback(agent, content_type: str, target_version: int):
    engine = getattr(agent, "_character_engine", None)
    if not engine or not getattr(engine, "_version_repo", None):
        return None, "版本历史不可用", 503
    sid = agent._session.session_id
    entry = await engine._version_repo.get_version(sid, target_version,
                                                    content_type=content_type)
    if entry is None:
        return None, f"版本 {target_version} 不存在", 404

    # 恢复文本
    if content_type == "l2":
        engine.state.self_l2 = entry["text"]
    elif content_type == "c6":
        engine.state.speaking_style_c6 = entry["text"]

    new_version = await engine._version_repo.append_version(
        session_id=sid, text=entry["text"], timestamp=now_iso(),
        trigger=CompressionTrigger.MANUAL,
        change_summary=[f"回滚到版本 {target_version}"],
        content_type=content_type,
    )

    # L2 rollback 后触发 C6 刷新
    if content_type == "l2":
        try:
            await engine._refresh_c6()
        except Exception:
            pass

    return {"status": "ok", "new_version": new_version, "text": entry["text"]}, None, 200
```

```python
# 旧路由（兼容）
async def l2_versions_handler(request: web.Request) -> web.Response:
    agent = request.app["agent"]
    limit = int(request.query.get("limit", "20"))
    before = request.query.get("before")
    before_version = int(before) if before else None
    result, error, status = await _list_character_versions(
        agent, "l2", limit, before_version)
    if error:
        return web.json_response({"error": error}, status=status)
    return web.json_response(result)

app.router.add_get("/debug/api/l2-versions", l2_versions_handler)

# 新通用路由
async def character_versions_handler(request: web.Request) -> web.Response:
    agent = request.app["agent"]
    content_type = request.query.get("type", "l2")
    if content_type not in ("l2", "c6"):
        return web.json_response({"error": "type 必须为 l2 或 c6"}, status=400)
    limit = int(request.query.get("limit", "20"))
    before = request.query.get("before")
    before_version = int(before) if before else None
    result, error, status = await _list_character_versions(
        agent, content_type, limit, before_version)
    if error:
        return web.json_response({"error": error}, status=status)
    return web.json_response(result)

app.router.add_get("/debug/api/character-versions", character_versions_handler)

# 新通用 rollback
async def character_rollback_handler(request: web.Request) -> web.Response:
    agent = request.app["agent"]
    try:
        body = await request.json()
        content_type = body["type"]
        target_version = int(body["version"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "缺少 type/version 或格式错误"}, status=400)
    if content_type not in ("l2", "c6"):
        return web.json_response({"error": "type 必须为 l2 或 c6"}, status=400)
    result, error, status = await _character_rollback(
        agent, content_type, target_version)
    if error:
        return web.json_response({"error": error}, status=status)
    return web.json_response(result)

app.router.add_post("/debug/api/character-rollback", character_rollback_handler)
```

- [ ] **Step 3: Update debug.html — C6 version count + browser**

```javascript
// debug.html — CharacterEngine panel: 在 L2 版本数旁加 C6 版本数

// 现有: <span id="ce-l2-version">0</span>
// 新增:

const vc6 = info.c6_version_count || 0;
// 在 L2 版本数同行显示:
// L2: ${vc} 版 | C6: ${vc6} 版
// C6 加"浏览"按钮 → onclick="loadCharacterVersions('c6')"
```

```javascript
// debug.html — 通用化版本浏览器

let versionList = [];
let currentVersionType = 'l2';

async function loadCharacterVersions(type, before) {
  currentVersionType = type;
  let url = '/debug/api/character-versions?type=' + type + '&limit=20';
  if (before) url += '&before=' + before;
  try {
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    versionList = data.versions || [];
    renderCharacterVersions(data, type);
  } catch (e) { alert('加载失败: ' + e.message); }
}

function renderCharacterVersions(data, type) {
  const modalId = 'character-versions-modal';
  const old = document.getElementById(modalId);
  if (old) old.remove();

  const label = type === 'l2' ? 'L2 Self' : 'C6 说话风格';
  const div = document.createElement('div');
  div.id = modalId;
  // ... same styling as before ...
  let html = '<h3>' + label + ' 版本历史 (' + (data.total || 0) + ' 版)</h3>';
  // ... same close button ...

  for (const v of (data.versions || [])) {
    // ... same rendering, but:
    // rollback button → onclick="rollbackCharacterVersion(\'' + type + '\', ' + v.version + ')"
  }

  // ... same has_more logic, calling loadCharacterVersions(type, last.version) ...
}

async function rollbackCharacterVersion(type, version) {
  if (!confirm('确认回滚 ' + type + ' 到版本 ' + version + '？')) return;
  try {
    const resp = await fetch('/debug/api/character-rollback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({type: type, version: version}),
    });
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    alert('回滚完成，新版本: ' + data.new_version);
    loadCharacterVersions(type);
  } catch (e) { alert('回滚失败: ' + e.message); }
}

// 兼容旧的 loadL2Versions / rollbackL2 调用
async function loadL2Versions(before) { loadCharacterVersions('l2', before); }
async function rollbackL2(version) { rollbackCharacterVersion('l2', version); }
```

- [ ] **Step 4: Run quick tests**

Run: `pytest -m "unit or service" -q --tb=short`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/debug/info.py core/input/debug_routes.py static/debug.html
git commit -m "feat: generalize debug version API + add C6 version browser"
```

---

### Task 7: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `core/personality/README.md`
- Modify: `docs/architecture.md`
- Modify: `todo.md`

- [ ] **Step 1: Update CLAUDE.md — CharacterEngine 描述**

在 CharacterEngine 段末尾加：

```
- **C6 SpeakingStyle 动态联动**: L2 演化后，LLM 编辑说话风格描述使与 L2 一致。版本历史与 L2 共享 `character_versions` 集合，per content_type 独立计数。
```

- [ ] **Step 2: Update personality/README.md — 架构图 + 数据流**

在 L2 更新数据流后追加：

```
C6 更新（L2 演化后联动）:
  _compress() 成功后 → _refresh_c6()
    → LLM 编辑当前 C6 文本（<new_speaking_style>）
    → CharacterState.speaking_style_c6
    → append_version(content_type="c6")
    → C6 模板条件渲染
```

在相关文档表加行：`prompt/SpeakingStyleEdit.j2 | C6 编辑 prompt 模板`

- [ ] **Step 3: Update architecture.md — character_versions 集合**

`docs/architecture.md` 中 character_l2_versions 相关描述改为 `character_versions`（支持 l2/c6 等 content_type）。

- [ ] **Step 4: Update todo.md**

```markdown
- ✅ ~~C3/C6/C7 与 CharacterEngine 动态联动（C6 部分）~~ — L2→C6 编辑模式 + 版本历史。C7 待后续。
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md core/personality/README.md docs/architecture.md todo.md
git commit -m "docs: update docs for C6 dynamic linkage + character_versions generalization"
```

---

### 自检

| Spec 覆盖 | 对应 Task |
|-----------|----------|
| CompressionTrigger.C6_REFRESH | Task 1 |
| VersionEntry.content_type | Task 1 |
| CharacterState.speaking_style_c6 | Task 1 |
| character_versions 集合 + content_type 过滤 | Task 2 |
| 版本号独立计算 | Task 2 |
| 旧文档 $or 兼容 | Task 2 |
| SpeakingStyleEdit.j2 | Task 3 |
| _refresh_c6() + render_c6() + C6Data | Task 4 |
| L2 rollback → C6 联动 | Task 6 (rollback handler) |
| C6 首次生成 L1 来源 | Task 4 |
| C6 模板条件分支 | Task 5 |
| SpeakingStyleSection set_context | Task 5 |
| PromptAssembler 接线 | Task 5 |
| Debug API 通用化 | Task 6 |
| 前端 C6 浏览器 | Task 6 |
| 文档 | Task 7 |
