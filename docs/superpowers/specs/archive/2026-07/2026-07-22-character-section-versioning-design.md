# Character Section 动态版本化 — 通用基础设施 + C6 首发

> **Goal:** 将 `CharacterVersionRepository` 从 L2 专用升级为通用 section 版本存储，C6 SpeakingStyle 作为第二个接入方，实现 L2 演化 → C6 自动刷新的动态联动。
>
> **Tech Stack:** Python 3.11+, Jinja2, MongoDB (motor), IsoTimestamp
>
> **关联:** CharacterEngine Phase 2 — C3/C6/C7 与 CharacterEngine 动态联动

---

## 1. 架构

```
L3→L2 压缩完成（_compress() 末尾）
  ↓
触发 _refresh_c6()
  ↓
  1. 取当前 speaking_style_c6（可能为空）
  2. 若为空：调用 SpeakingStyleSection.render()（不传 speaking_style_c6）获取 L1 默认文本
  3. 取新的 self_l2
  4. 渲染 SpeakingStyleEdit.j2 prompt
  5. LLM 调用（复用 self._llm.think()，与 _compress() 相同参数模式）
  6. 解析 <new_speaking_style> + <changes>（_extract_tag + re.DOTALL，与 _compress() 相同模式）
  7. 更新 state.speaking_style_c6 = new_text
  8. append_version(content_type="c6", ...) 写入 character_versions
  ↓
C6 模板渲染:
  {% if speaking_style_c6 %}{{ speaking_style_c6 }}{% else %}[L1 默认全文]{% endif %}
```

### 通用基础设施

```
character_versions 集合
  ├── content_type="l2"   → CharacterState.self_l2
  ├── content_type="c6"   → CharacterState.speaking_style_c6
  ├── content_type="c7"   → CharacterState.proactive_c7        (未来)
  └── content_type="c2"   → CharacterState.relationship_c2     (未来)
```

所有 content_type 共享同一个 append-only 集合，通过 `(session_id, content_type, version)` 索引区分。

---

## 2. 变更清单

### 2.1 新增

| 文件 | 内容 |
|------|------|
| `prompt/SpeakingStyleEdit.j2` | C6 编辑 prompt 模板 — LLM 接收当前 C6 文本 + 新 L2，输出 `<new_speaking_style>` + `<changes>` |
| `core/personality/tests/test_speaking_style_edit.py` | C6 编辑逻辑测试 |
| `core/schema/tests/test_character.py` | `speaking_style_c6` 字段测试 |

### 2.2 修改

| 文件 | 改动 |
|------|------|
| `core/schema/compression.py` | +`CompressionTrigger.C6_REFRESH` 枚举值；`VersionEntry` TypedDict +`content_type: str` 字段 |
| `core/schema/character.py` | `CharacterState` +`speaking_style_c6: str = ""` |
| `core/storage/character_version_repo.py` | 集合名 `character_l2_versions` → `character_versions`；所有方法 +`content_type: str` 参数（默认 `"l2"`）；`append_version()` 内部 find 查询加 `content_type` 过滤 |
| `core/personality/engine.py` | +`_refresh_c6()` 方法 + `render_c6()` 方法；`_compress()` 末尾调用 `_refresh_c6()`；L2 rollback 后触发 `_refresh_c6()` |
| `core/schema/__init__.py` | 导出新增字段 |
| `Module/system_prompt/sections/speaking_style.py` | `set_context()` 通过 kwargs 接收 `speaking_style_c6: str`；`render()` 注入模板 |
| `Module/system_prompt/templates/c6_speaking_style.j2` | 顶部加 `{% if speaking_style_c6 %}{{ speaking_style_c6 }}{% else %}` 分支 |
| `core/prompt_assembler.py` | `build_system_prompt()` 调用 `render_c6()`，通过 `set_context(**kwargs)` 传入 |
| `core/debug/info.py` | `DebugInfo` +`c6_version_count: int`；`collect()` 从 version_repo 按 content_type 分别统计 |
| `core/input/debug_routes.py` | `/debug/api/l2-versions` → 提取 handler 函数，新旧路由共享（不发额外 HTTP 请求）；新增通用 `/debug/api/character-versions?type=`；rollback 通用化为 `/debug/api/character-rollback` |
| `static/debug.html` | C6 版本浏览器（与 L2 面板并列，复用同一套 JS 函数，传不同 `type` 参数） |
| `core/storage/tests/test_character_version_repo.py` | 扩展测试覆盖 content_type 过滤 + 旧文档兼容 |

### 2.3 文档

| 文件 | 改动 |
|------|------|
| `CLAUDE.md` | 更新 CharacterEngine 描述：C6 动态联动 |
| `core/personality/README.md` | 新增 C6 动态联动章节；更新数据流图 |
| `docs/architecture.md` | character_versions 集合描述更新 |
| `docs/completed.md` | 完成后归档 |
| `todo.md` | 标记 C6 联动完成 |

---

## 3. 数据结构

### 3.1 CharacterState 新增字段

```python
# core/schema/character.py
class CharacterState(BaseModel):
    # ... 现有字段 ...
    speaking_style_c6: str = ""  # C6 动态文本。空串 = 使用 L1 默认模板
    # 未来预留:
    # proactive_c7: str = ""
    # relationship_c2: str = ""
```

### 3.2 CompressionTrigger 新增值

```python
class CompressionTrigger(StrEnum):
    COMPRESSION = "compression"
    FLUSH = "flush"
    MANUAL = "manual"
    C6_REFRESH = "c6_refresh"       # 新增
```

### 3.3 VersionEntry 新增字段

```python
# core/schema/compression.py
class VersionEntry(TypedDict):
    session_id: str
    version: int
    text: str
    timestamp: str
    trigger: str
    change_summary: list[str]
    content_type: str              # 新增: "l2" | "c6"
```

### 3.4 character_versions 文档结构

```json
{
    "session_id": "default",
    "content_type": "l2",
    "version": 3,
    "text": "...",
    "timestamp": "2026-07-22T15:30:00.000000+08:00",
    "trigger": "compression",
    "change_summary": ["调整了神秘模式的触发频率", "语气从委婉改为更直接"]
}
```

索引：`(session_id, content_type, version)` 复合索引（非唯一 → 唯一迁移，见 3.6）。

### 3.5 CharacterVersionRepository API 变更

```python
# 所有方法加 content_type 参数（默认 "l2" 保持向后兼容）

async def append_version(
    self, session_id, text, timestamp, trigger, change_summary,
    content_type: str = "l2",  # 新增
) -> int:
    # 版本号计算: find({"session_id": session_id, "content_type": content_type},
    #                 sort=[("version", -1)], limit=1)
    # 必须加 content_type 过滤，否则 L2 和 C6 版本号互相干扰
    ...

async def list_versions(
    self, session_id, *, limit=20, before_version=None,
    content_type: str = "l2",  # 新增
) -> list[VersionEntry]: ...

async def get_version(
    self, session_id, version,
    content_type: str = "l2",  # 新增
) -> VersionEntry | None: ...

async def count_versions(
    self, session_id,
    content_type: str = "l2",  # 新增
) -> int: ...
```

### 3.6 迁移策略

**索引迁移**：

- 当前索引为非唯一 `(session_id, version)`（`character_version_repo.py:28-30`）
- 新建唯一索引 `(session_id, content_type, version)`
- 唯一性安全：由于旧文档没有 `content_type` 字段，MongoDB 对"缺失字段"统一视为 `null`。只要现有数据中没有同一 `(session_id, version)` 的重复文档，创建唯一索引就能成功。实施时先用 `aggregate` 检查重复 → 有重复则合并/WARN → 再建索引
- 旧索引 `(session_id, version)` 在新建成功后立即删除（非"保留不做自动删除"）。新索引前缀已覆盖旧索引的查询模式，保留只会增加写开销

**数据迁移**：

- 现有 `character_l2_versions` 集合中的文档没有 `content_type` 字段
- 集合改名：通过 `renameCollection` 或代码中切换集合名。由于 MongoDB 不支持 `renameCollection` 在同一个 db 内事务，采用：代码先连接 `character_versions`，若为空且 `character_l2_versions` 有数据，复制文档 + 添加 `content_type: "l2"`
- `list_versions()` / `get_version()` / `count_versions()` 在迁移完成后的查询：精确匹配 `content_type`
- 兼容期（迁移前）：`{"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}`

---

## 4. Prompt 设计

### 4.1 SpeakingStyleEdit.j2

变量来源：由 `engine._refresh_c6()` 通过 `PromptManager.render()` 传入。

```
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

### 4.2 _refresh_c6() 实现规范

```python
async def _refresh_c6(self) -> None:
    """L2 更新后刷新 C6 说话风格。失败不阻塞。"""
    # 1. 获取当前 C6 文本
    current = self.state.speaking_style_c6
    if not current:
        # 首次：渲染 L1 默认模板获取基准文本
        from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
        sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")
        sec.set_context(user_name="")  # user_name 当前在默认模板中不使用，传空
        current = await sec.render()

    # 2. 渲染 prompt
    prompt = self._prompt_mgr.render("SpeakingStyleEdit.j2",
        character_name=self._config.name,
        self_l2=self.state.self_l2,
        current_speaking_style=current,
    )

    # 3. LLM 调用（与 _compress() 相同模式）
    raw = await self._llm.think(prompt)
    if not raw:
        logger.warning("C6 refresh: LLM 返回空，跳过")
        return

    # 4. 解析 XML 标签（复用 _extract_tag，与 _compress() 一致）
    new_style = _extract_tag(raw, "new_speaking_style")
    changes_raw = _extract_tag(raw, "changes")

    if not new_style:
        logger.warning("C6 refresh: <new_speaking_style> 解析失败")
        return

    # 5. 解析 changes（按行 split，去空行/纯空白）
    changes = [l.strip() for l in (changes_raw or "").splitlines() if l.strip()]

    # 6. 更新 state + 写版本历史
    self.state.speaking_style_c6 = new_style
    await self._version_repo.append_version(
        session_id=self.state.session_id,
        text=new_style,
        timestamp=now_iso(),
        trigger=CompressionTrigger.C6_REFRESH,
        change_summary=changes,
        content_type="c6",
    )
```

### 4.3 render_c6() 数据接口

```python
@dataclass
class C6Data:
    speaking_style_c6: str  # "" 表示使用默认模板

def render_c6(self) -> C6Data:
    return C6Data(speaking_style_c6=self.state.speaking_style_c6)
```

### 4.4 C6 数据流模式说明

C6 通过 `set_context(**kwargs)` 注入（与 C3/C4/C5/C7 一致），而非像 C1/C9 用专用 update 方法。差异理由：

- C1/C9 的数据（PersonalityData / AwarenessData）来自 `render_c1()` / `render_c9()` 的返回，是 PersonalitySection/SelfPerceptionSection 的特化需求
- C6 的数据只是一个字符串 `speaking_style_c6`，不需要专用类型。走 `set_context` 更简单，不引入不必要的抽象

### 4.5 L2 回滚后 C6 联动

L2 rollback（`POST /debug/api/character-rollback` with `type=l2`）后：

1. 恢复 `CharacterState.self_l2 = 旧文本`
2. 立即触发 `_refresh_c6()` — 用新的 self_l2 重新编辑 C6
3. 如果 `_refresh_c6()` 失败，WARN + C6 保留旧值（不回滚 C6）。下次压缩会自然修复

同样，C6 rollback（`type=c6`）只恢复 `speaking_style_c6`，不触发 L2 联动。

---

## 5. 触发机制

- **触发时机**：`_compress()` 成功更新 L2 后，调用 `_refresh_c6()`
- **首次触发**：默认 `speaking_style_c6` 为空（使用 L1 默认），首次压缩后立即生成第一版 C6。`_refresh_c6()` 通过调用 `SpeakingStyleSection.render()` 获取 L1 默认渲染文本作为 `current_speaking_style`
- **频率控制**：与 L2 压缩相同频率（state_history 新增 >= 5 条或距上次 > 7 天）。不做额外节流——L2 不更新时 C6 也不需要更新
- **flush 触发**：`flush_compression()` → L2 更新 → C6 自动刷新
- **rollback 触发**：L2 rollback → C6 自动刷新

---

## 6. 模板渲染

### 6.1 C6 模板改造

```jinja2
{# c6_speaking_style.j2 #}
<speaking_style>
{% if speaking_style_c6 %}
{{ speaking_style_c6 }}
{% else %}
每次回复前，先在心里想一下（这是你自己的事），再决定说出来什么。
……
（当前的 L1 默认全文，完全不动，包在 {% else %} 中）
{% endif %}
</speaking_style>
```

### 6.2 SpeakingStyleSection 改造

```python
class SpeakingStyleSection(Jinja2Section):
    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""
        self._speaking_style_c6 = ""    # 新增

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._speaking_style_c6 = kwargs.get("speaking_style_c6", "")  # 通过 kwargs 注入

    async def render(self) -> str:
        return self._render_template(
            user_name=self._user_name,
            speaking_style_c6=self._speaking_style_c6,
        )
```

### 6.3 PromptAssembler 注入

```python
# build_system_prompt() 中，和 _refresh_personality_data() 同级:
if self._personality_engine is not None:
    c6 = self._personality_engine.render_c6()
    speaking_style_c6 = c6.speaking_style_c6
else:
    speaking_style_c6 = ""

# set_context 时传入:
self._manager.set_context(
    ...
    speaking_style_c6=speaking_style_c6,
)
```

---

## 7. Debug 面板

### 7.1 API 通用化

```python
# 旧路由保留 — handler 内部调用通用函数，不发起额外 HTTP 请求
GET /debug/api/l2-versions?limit=20&before=5
  → handler 函数直接调用 _list_character_versions(session_id, content_type="l2", ...)

# 新增通用端点
GET /debug/api/character-versions?type=l2&limit=20&before=5
GET /debug/api/character-versions?type=c6&limit=20&before=5

# Rollback 通用化（替代旧 /debug/api/l2-rollback）
POST /debug/api/character-rollback
  body: {"type": "l2", "version": 3}   → 回滚 L2 + 触发 C6 刷新
  body: {"type": "c6", "version": 3}   → 仅回滚 C6，不触发 L2 联动
```

实现方式：

```python
# 提取共享 handler 函数
async def _list_character_versions(request, content_type: str):
    ...

# 旧路由
app.router.add_get("/debug/api/l2-versions",
    lambda req: _list_character_versions(req, content_type="l2"))

# 新路由
app.router.add_get("/debug/api/character-versions",
    lambda req: _list_character_versions(req, content_type=req.query.get("type", "l2")))

# Rollback 同理：提取 _rollback_character_version(req, content_type)
```

### 7.2 前端

- CharacterEngine 面板：L2 版本数 + C6 版本数 + 各自的"浏览"按钮
- 版本浏览器模态框：复用同一套 JS 函数，传 `type` 参数区分
- 回滚按钮同样通用化

---

## 8. 测试

### 8.1 单元测试

| 测试 | 文件 | 内容 |
|------|------|------|
| `test_c6_refresh_trigger_enum` | `core/schema/tests/test_compression.py` | `C6_REFRESH` 枚举值存在；`VersionEntry` 含 `content_type` |
| `test_speaking_style_c6_default` | `core/schema/tests/test_character.py` | 新字段默认空串 |
| `test_speaking_style_c6_assign` | 同上 | 字段可赋值 |

### 8.2 Service 测试

| 测试 | 文件 | 内容 |
|------|------|------|
| `test_append_version_c6` | `core/storage/tests/test_character_version_repo.py` | c6 类型 append → version=1（独立计数） |
| `test_append_version_l2_c6_independent` | 同上 | L2 和 C6 版本号不互相干扰 |
| `test_list_versions_filtered_by_type` | 同上 | `content_type="c6"` 只返回 C6 条目 |
| `test_old_docs_no_content_type` | 同上 | 旧文档（无 content_type）被当作 l2 兼容 |
| `test_refresh_c6_generates` | `core/personality/tests/test_engine.py` | `_refresh_c6()` 调用 LLM 并更新 state |
| `test_refresh_c6_first_time` | 同上 | speaking_style_c6 为空时首次生成（验证调用了 SpeakingStyleSection.render()） |
| `test_c6_edit_output_parsed` | 同上 | `<new_speaking_style>` + `<changes>` 解析正确 |
| `test_c6_refresh_failure_no_block` | 同上 | LLM 返回空/解析失败时保留旧值，不抛异常 |
| `test_c6_template_fallback` | `Module/system_prompt/tests/` | `speaking_style_c6=""` 时渲染 L1 默认模板 |
| `test_c6_template_dynamic` | 同上 | `speaking_style_c6="动态文本"` 时渲染动态文本 |

### 8.3 需要更新的现有测试

- `test_character_version_repo.py`：所有调用处保持默认 `content_type="l2"`，行为不变
- `test_engine.py`：`_compress()` 新增 `_refresh_c6()` 调用路径需 mock
- `test_debug_routes.py`：旧 `/debug/api/l2-versions` 仍然可用，需验证返回 l2 数据

---

## 9. 注意事项

1. **L2 和 C6 的编辑是串行的**：先 L2 压缩 → 再 C6 刷新。不做并行——C6 依赖新的 L2
2. **C6 编辑失败不阻塞**：LLM 调用失败/返回空/解析失败 → WARN 日志 + 保留 `speaking_style_c6` 旧值，不影响主流程
3. **首次压缩 C6 也跟着首次生成**：`speaking_style_c6` 为空 → `_refresh_c6()` 调用 `SpeakingStyleSection.render()` 获取 L1 默认渲染文本作为 `current_speaking_style` 输入
4. **C6 版本号独立于 L2**：各自 per content_type 计数
5. **L2 rollback → C6 自动刷新**：确保 C6 与当前 L2 一致。C6 rollback 不触发 L2 联动
6. **C7 接入时只需**：+字段、+prompt、+`_refresh_c7()` 方法、+debug 面板。基础设施零改动
