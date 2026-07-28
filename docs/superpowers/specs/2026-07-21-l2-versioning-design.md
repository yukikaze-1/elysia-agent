# L2 Self 版本化 — LLM 编辑模式 + 永久历史

> **Goal:** 将 L3→L2 压缩从 "LLM 全量重写一段文本" 升级为 "LLM 编辑一份持续迭代的文档"，所有历史版本 append-only 保留到独立 MongoDB 集合。
>
> **Tech Stack:** Python 3.11+, Jinja2, MongoDB (motor), IsoTimestamp
>
> **关联:** CharacterEngine Phase 2 — L3→L2 梯度压缩自动化

---

## 1. 架构

```
CharacterReflector 产出 L3 条目
  → _should_compress()（现行机制：5 条 or 7 天）
    → _compress():
      1. 取 self_l2（当前最后一版）
      2. 渲染 CompressionPrompt（编辑模式）
      3. LLM 返回 <new_self> + <changes>
      4. 更新 self.state.self_l2 = new_self
      5. append_version() 写入 character_l2_versions
```

## 2. 变更清单

### 2.1 新增

| 文件 | 内容 |
|------|------|
| `core/schema/compression.py` | `CompressionTrigger` StrEnum + `VersionEntry` TypedDict |
| `core/storage/character_version_repo.py` | `CharacterVersionRepository` — append/list/get/count |

### 2.2 修改

| 文件 | 改动 |
|------|------|
| `prompt/PersonalityCompression.j2` | 重写：编辑模式 + 结构化输出（`<new_self>` + `<changes>`） |
| `core/personality/engine.py` | `_compress()` 解析 XML 标签 → 写版本历史；新增 `_extract_tag()` 辅助函数；`flush_compression()` trigger 改为 `FLUSH`；`CharacterEngine.__init__` 接受 `version_repo` 参数 |
| `core/debug/info.py` | 新增 `l2_versions` 数据聚合 |
| `core/input/debug_routes.py` | 新增 `GET /debug/api/l2-versions` 端点 |
| `core/storage/__init__.py` | 导出 `CharacterVersionRepository` |

### 2.3 不改

| 文件 | 说明 |
|------|------|
| `core/schema/character.py` | `CharacterState.self_l2: str` 不变；历史在独立集合 |
| `core/personality/engine.py:_should_compress()` | 触发条件照旧（5 条 or 7 天），todo.md 标注后续更新 |

## 3. 数据结构

### 3.1 CharacterVersionRepository — MongoDB `character_l2_versions` 集合

```python
# 文档结构
{
    session_id: "default",
    version: 57,                                        # int, 递增
    text: "我变得温柔了，开始在深夜陪伴中感受到满足...",   # str, L2 全文
    timestamp: "2026-07-21T15:30:00.000Z",              # IsoTimestamp
    trigger: "compression",                              # CompressionTrigger
    change_summary: [                                    # list[str]
        "新增: 享受深夜的安静陪伴",
        "强化: 温柔特质更明显",
        "移除: 不再像以前那样害羞",
    ],
}
```

索引：`[(session_id, 1), (version, -1)]`

### 3.2 API

```python
class CharacterVersionRepository(Repository):
    COLLECTION = "character_l2_versions"

    async def append_version(
        self, session_id: SessionId, text: str,
        timestamp: IsoTimestamp, trigger: CompressionTrigger,
        change_summary: list[str],
    ) -> int:
        """追加一个版本。自动计算 version 序号（max+1）。返回新版本号。"""

    async def list_versions(
        self, session_id: SessionId, *, limit: int = 20,
        before_version: int | None = None,
    ) -> list[VersionEntry]:
        """分页查询版本历史。默认返回最近 20 条。"""

    async def get_version(
        self, session_id: SessionId, version: int,
    ) -> VersionEntry | None:
        """获取指定版本。"""

    async def count_versions(self, session_id: SessionId) -> int:
        """返回版本总数。"""
```

### 3.3 Schema types

```python
# core/schema/compression.py

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

## 4. Prompt 模板

`prompt/PersonalityCompression.j2` — 重写：

```jinja2
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

相比旧版的差异：
- 删除了 `l3_current` 参数（`recent_changes` 已包含当前状态）
- 要求结构化输出（`<new_self>` + `<changes>`）
- 语言从"感受和写下"改为"编辑文档"——更明确的角色定义

## 5. 引擎改动

### 5.1 `__init__`

```python
def __init__(self, ..., version_repo: CharacterVersionRepository | None = None):
    ...
    self._version_repo = version_repo
```

### 5.2 `_compress()` 重写

```python
async def _compress(self, trigger: CompressionTrigger = CompressionTrigger.COMPRESSION) -> None:
    if self._pm is None:
        return

    current_self = self.state.self_l2
    new_entries = self._get_new_entries()

    try:
        system = self._pm.render_macro(
            "PersonalityCompression.j2", "CompressionSystemPrompt",
            character_name=self._character_name, l1_core=self._l1_core,
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
        result = await self._llm.think([...])
        text = result.text.strip()
    except Exception:
        logger.warning("CharacterEngine: 压缩 LLM 调用失败", exc_info=True)
        return

    new_self = _extract_tag(text, "new_self")
    if not new_self:
        logger.warning("CharacterEngine: 压缩 LLM 返回无 <new_self>")
        return

    changes_raw = _extract_tag(text, "changes")
    changes = [c.strip("- ") for c in changes_raw.strip().split("\n") if c.strip()]

    # 更新
    self.state.self_l2 = new_self
    self.state.compression_last_at = time.time()
    self.state.compression_last_count = len(self.state.state_history)

    if self._version_repo:
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

### 5.3 `_extract_tag()` 辅助函数

```python
def _extract_tag(text: str, tag: str) -> str:
    """从 XML 标签中提取内容。"""
    import re
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""
```

### 5.4 `flush_compression()` — 传入 FLUSH trigger

```python
async def flush_compression(self) -> None:
    # ... 现有检查逻辑不变 ...
    async with self._compression_lock:
        try:
            await asyncio.wait_for(
                self._compress(trigger=CompressionTrigger.FLUSH), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("CharacterEngine: 压缩 flush 超时")
```

### 5.5 C1 渲染 — `render_c1()` 不变

`self.state.self_l2` 仍指向最新版本，C1 渲染逻辑完全不变。

## 6. Debug 面板

### 6.1 `debug/info.py`

```python
# 新增
info.l2_version_count = 0
if agent._character_engine._version_repo:
    info.l2_version_count = await agent._character_engine._version_repo.count_versions(
        agent._session.session_id
    )
```

### 6.2 API 端点

```
GET /debug/api/l2-versions?limit=20&before=57
  → {
      "versions": [...],
      "total": 134,
      "has_more": true
    }
```

## 7. 回滚

debug 面板提供 "回滚到此版本" 按钮：

```
POST /debug/api/l2-rollback
  body: {"version": 55}
  → 将 self.state.self_l2 设为 version 55 的 text
  → append 一条新版本: {text: 回滚后的文本, trigger: "manual", change_summary: ["回滚到版本 55"]}
```

回滚本身也是一条版本记录——不丢失历史。

## 8. 降级 & 兼容

| 场景 | 处理 |
|------|------|
| `version_repo` 为 None（MongoDB 不可用） | 照常压缩、更新 `self_l2`，不写历史，打 WARNING 日志 |
| `self_l2` 为空（首次压缩） | UserPrompt 中不输出 `<current_self>` 标签，LLM 从零写 |
| LLM 返回无 `<new_self>` | 不更新 `self_l2`，不写版本历史 |
| LLM 返回无 `<changes>` | `change_summary = []`，照常保存 |
| 已有 session 没有历史集合 | 第一次压缩时自动创建（MongoDB 惰性建集合），version 从 1 开始 |

## 9. 测试

| 层 | 测试点 |
|----|--------|
| 单元 | `_extract_tag()` 正常提取 / 标签不存在 / 空标签 |
| 单元 | `CompressionTrigger` 枚举值 |
| 服务 | `CharacterVersionRepository` CRUD：append 自动递增 / list 分页 / get / count |
| 集成 | `_compress()` 产出正确 `new_self` + `changes` 并写入版本 |
| 集成 | `flush_compression()` trigger = FLUSH |
| 集成 | version_repo=None 降级路径（只更新 self_l2，不崩溃） |
