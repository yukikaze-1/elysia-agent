# LTM 自动更新与去重 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add consolidation-time memory comparison and periodic MemoryReflector for auto-update/dedup of long-term memories.

**Architecture:** 5 tasks — data model extensions → consolidation comparison → MemoryReflector class → agent lifecycle wiring → prompt placeholder. Tasks 1-4 are sequential (each depends on prior). Task 5 is parallel.

**Tech Stack:** Python 3.13, asyncio, Milvus, embedding (bge-large-zh-v1.5), sklearn (KMeans)

## Global Constraints

- 保守原则：不确定时倾向于保留，不强制匹配/合并/删除
- 被吸收的记忆不立即删除（mark_absorbed → MemoryReflector 后续清理）
- 执行前预校验整个 LLM 输出批次，校验失败跳过整个批次
- 部分失败不阻塞后续操作（记日志 + 跳过）
- 不使用后台定时器（MemoryReflector 仅在 response hook 中触发）
- `_write_lock` 保护所有 LongTermMemory 修改方法
- 开发阶段不启用驱逐（_evict_old_entries 调用已注释）

---

### Task 1: LongTermMemory — 数据模型扩展 + 驱逐暂停

**Files:**
- Modify: `core/memory/long_term.py:38-66` (__init__), `core/memory/long_term.py:264-265` (insert), `core/memory/long_term.py:312-360` (_sync_dirty_entries area), `core/memory/long_term.py:343-382` (update_text), `core/memory/long_term.py:383-396` (delete), `core/memory/long_term.py:398-407` (clear_all), `core/memory/long_term.py:411-530` (search)

**Interfaces:**
- Produces: `LongTermMemory._absorbed_ids: set[str]`, `LongTermMemory._write_lock: asyncio.Lock`
- Produces: `LongTermMemory.get_all() -> list[MemoryEntry]`
- Produces: `LongTermMemory.mark_absorbed(entry_id: str) -> None`
- Produces: `LongTermMemory.update_text(entry_id: str, new_text: str, poignancy: int | None = None, memory_type: str | None = None) -> bool` (extended signature)
- Modifies: `LongTermMemory.search()` — filter out absorbed entries
- Modifies: `LongTermMemory.insert()` — comment out `_evict_old_entries()`
- Modifies: `LongTermMemory.delete()` — also discard from `_absorbed_ids`
- Modifies: `LongTermMemory.clear_all()` — also clear `_absorbed_ids`

- [ ] **Step 1: Add _absorbed_ids and _write_lock to __init__**

In `core/memory/long_term.py`, `__init__` method, add after existing field initializations:

```python
self._absorbed_ids: set[str] = set()  # 被 merge 吸收但尚未 delete 的条目 ID
self._write_lock = asyncio.Lock()     # 保护所有修改操作的并发锁
```

- [ ] **Step 2: Comment out _evict_old_entries() in insert()**

In `insert()`, line 265, change:

```python
self._entries.append(entry)
# self._evict_old_entries()  # TODO: 开发阶段暂不禁用驱逐，避免丢失测试记忆
```

- [ ] **Step 3: Add get_all() method**

After `_get_by_id()` (around line 310), add:

```python
def get_all(self) -> list[MemoryEntry]:
    """返回全部活跃条目的副本（排除已吸收条目）。"""
    return [e for e in self._entries if e.id not in self._absorbed_ids]
```

- [ ] **Step 4: Add mark_absorbed() method**

After `get_all()`, add:

```python
def mark_absorbed(self, entry_id: str) -> None:
    """标记条目已被 merge 吸收（不删除，只标记）。"""
    self._absorbed_ids.add(entry_id)
```

- [ ] **Step 5: Extend update_text() signature**

Change the method signature from:

```python
async def update_text(self, entry_id: str, new_text: str) -> bool:
```

To:

```python
async def update_text(
    self, entry_id: str, new_text: str,
    poignancy: int | None = None,
    memory_type: str | None = None,
) -> bool:
```

Inside the method, after the line `e.text = new_text`, add:

```python
if poignancy is not None:
    e.poignancy = poignancy
if memory_type is not None:
    e.memory_type = memory_type
```

And update the Milvus metadata dict (around line 359) to include the updated fields:

```python
metadata=[{
    "entry_id": e.id,
    "text": new_text,
    "memory_type": e.memory_type,
    "poignancy": e.poignancy,
    "timestamp": e.timestamp,
}],
```

- [ ] **Step 6: Update delete() to discard from _absorbed_ids**

In `delete()`, after `self._entries.pop(i)`, add:

```python
self._absorbed_ids.discard(entry_id)
```

- [ ] **Step 7: Update clear_all() to clear _absorbed_ids**

In `clear_all()`, after `self._entries.clear()`, add:

```python
self._absorbed_ids.clear()
```

- [ ] **Step 8: Update search() to filter absorbed entries**

In `search()`, after the `candidates` list is populated (around line 423), add filter:

```python
# Exclude absorbed entries from search results
candidates = [e for e in candidates if e.id not in self._absorbed_ids]
```

And similarly, when falling back to local entries without Milvus, filter:

```python
candidates = [e for e in self._entries if e.id not in self._absorbed_ids]
```

- [ ] **Step 9: Add _write_lock to insert(), update_text(), delete(), clear_all()**

Wrap each method body that modifies data with `async with self._write_lock:`. The protected methods:
- `insert()` — wrap from after "await self._ensure_embedding" to end
- `update_text()` — wrap the inner logic (after the signature check)
- `delete()` — wrap the inner logic
- `clear_all()` — wrap the inner logic
- `mark_absorbed()` — no lock needed (set.add is atomic in asyncio single-thread)

- [ ] **Step 10: Verify import and basic sanity**

```bash
conda run -n Elysia python -c "
from core.memory.long_term import LongTermMemory
ltm = LongTermMemory()
assert hasattr(ltm, '_absorbed_ids')
assert hasattr(ltm, '_write_lock')
assert ltm.get_all() == []
ltm.mark_absorbed('test_id')
assert 'test_id' in ltm._absorbed_ids
print('OK')
"
```

Expected: `OK`

- [ ] **Step 11: Commit**

```bash
git add core/memory/long_term.py
git commit -m "feat: LTM data model extension — absorbed_ids, write_lock, get_all, mark_absorbed, extended update_text"
```

---

### Task 2: MemoryConsolidator — consolidation 时轻量比对

**Files:**
- Modify: `core/consolidator.py:94-140` (_do_consolidate), add `_compare_and_resolve()`
- Modify: `core/consolidator.py:42-56` (__init__ — add prompt_path parameter)

**Interfaces:**
- Consumes: `LongTermMemory.search()`, `LongTermMemory.get_all()`, `LongTermMemory.insert()`, `LongTermMemory.update_text()`, `LongTermMemory.mark_absorbed()`
- Consumes: `LLMAdapter.think()` (for comparison call)

- [ ] **Step 1: Add _compare_and_resolve() method**

After `_parse_result()`, add:

```python
async def _compare_and_resolve(self, new_items: list[dict]) -> int:
    """将新提取的记忆与已有记忆比对，执行 insert/update/merge/skip。
    返回实际新增的记忆数。失败时回退：全部 insert。"""
    if not new_items or not self._memory.get_all():
        # 无已有记忆，全部直接 insert
        for item in new_items:
            content = item.get("content", "").strip()
            if content:
                await self._memory.insert(
                    content,
                    memory_type=item.get("type", "note"),
                    poignancy=int(item.get("poignancy", 5)),
                    source="consolidation",
                )
        return len(new_items)

    try:
        # 1. 搜索相关已有记忆
        related = await self._memory.search(
            query_text=" ".join(item.get("content", "") for item in new_items),
            top_k=10,
        )
        related_dicts = [
            {"id": e.id, "type": e.memory_type, "content": e.text,
             "poignancy": e.poignancy, "timestamp": e.timestamp}
            for e in related
        ]

        # 2. LLM 比对
        compare_prompt = COMPARE_PROMPT.format(
            new_items=json.dumps(new_items, ensure_ascii=False, indent=2),
            existing_items=json.dumps(related_dicts, ensure_ascii=False, indent=2),
        )
        result = await self._llm.think(
            [{"role": "user", "content": compare_prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        actions = self._parse_result(result.text)

        # 3. 预校验
        if not self._validate_actions(actions, new_items):
            raise ValueError("Action validation failed")

        # 4. 执行 — merge → update → insert
        inserted = 0
        for action in actions:
            act = action.get("action", "insert")
            if act == "merge":
                target_id = action.get("target_id", "")
                merged = action.get("merged_content", "")
                if target_id and merged:
                    await self._memory.update_text(
                        target_id, merged,
                        poignancy=action.get("poignancy"),
                        memory_type=action.get("type"),
                    )
                    # 标记被吸收的新记忆（如果有 id）
                    # Note: 这里只处理新记忆 merge 到已有记忆的情况。
                    # 被吸收的是"新记忆中对应的条目"——在本轮范围内由 LLM 判断。
            elif act == "update":
                target_id = action.get("target_id", "")
                new_content = action.get("new_content", "")
                if target_id and new_content:
                    await self._memory.update_text(target_id, new_content)
            elif act == "insert":
                content = action.get("content", "").strip()
                if content:
                    await self._memory.insert(
                        content,
                        memory_type=action.get("type", "note"),
                        poignancy=int(action.get("poignancy", 5)),
                        source="consolidation",
                    )
                    inserted += 1
            # skip — 无操作

        return inserted

    except Exception:
        logger.debug("Consolidation compare failed, falling back to insert all",
                     exc_info=True)
        # 回退：全部 insert
        for item in new_items:
            content = item.get("content", "").strip()
            if content:
                await self._memory.insert(
                    content,
                    memory_type=item.get("type", "note"),
                    poignancy=int(item.get("poignancy", 5)),
                    source="consolidation",
                )
        return len(new_items)

@staticmethod
def _validate_actions(actions: list[dict], new_items: list[dict]) -> bool:
    """预校验 LLM 输出的 actions 批次。返回 True 表示通过。"""
    if not isinstance(actions, list):
        return False
    for action in actions:
        act = action.get("action", "")
        if act not in ("insert", "update", "merge", "skip"):
            return False
        if act in ("update", "merge") and not action.get("target_id"):
            return False
    return True
```

- [ ] **Step 2: Add COMPARE_PROMPT placeholder**

At module level in `consolidator.py`, after `EXTRACTION_PROMPT`:

```python
# Consolidation 比对 prompt — 文件路径，运行时加载
# 实际 prompt 文本由 GPT 生成后放入 prompt/consolidation_compare.txt
COMPARE_PROMPT_PATH = Path("prompt/consolidation_compare.txt")
_COMPARE_PROMPT_CACHE: str | None = None

def _load_compare_prompt() -> str:
    global _COMPARE_PROMPT_CACHE
    if _COMPARE_PROMPT_CACHE is not None:
        return _COMPARE_PROMPT_CACHE
    if COMPARE_PROMPT_PATH.exists():
        _COMPARE_PROMPT_CACHE = COMPARE_PROMPT_PATH.read_text(encoding="utf-8")
        return _COMPARE_PROMPT_CACHE
    # 回退：简单拼接（开发阶段 prompt 未就绪时可用）
    _COMPARE_PROMPT_CACHE = (
        "Compare these new memory items:\n{new_items}\n\n"
        "With existing related memories:\n{existing_items}\n\n"
        "For each new item, output: insert/update/merge/skip as JSON array."
    )
    return _COMPARE_PROMPT_CACHE

COMPARE_PROMPT = _load_compare_prompt()  # Module-level constant for format()
```

Wait, `COMPARE_PROMPT` needs to be callable with `.format()`. Let me fix — use a property or lazy load:

```python
_COMPARE_PROMPT: str | None = None

def _get_compare_prompt() -> str:
    global _COMPARE_PROMPT
    if _COMPARE_PROMPT is not None:
        return _COMPARE_PROMPT
    path = Path("prompt/consolidation_compare.txt")
    if path.exists():
        _COMPARE_PROMPT = path.read_text(encoding="utf-8")
    else:
        _COMPARE_PROMPT = (
            "Compare these new memory items:\n{new_items}\n\n"
            "With existing related memories:\n{existing_items}\n\n"
            "For each new item, output: insert/update/merge/skip as JSON array."
        )
    return _COMPARE_PROMPT
```

And in `_compare_and_resolve()`, use:
```python
prompt_template = _get_compare_prompt()
compare_prompt = prompt_template.format(
    new_items=json.dumps(new_items, ensure_ascii=False, indent=2),
    existing_items=json.dumps(related_dicts, ensure_ascii=False, indent=2),
)
```

- [ ] **Step 3: Call _compare_and_resolve() at end of _do_consolidate()**

In `_do_consolidate()`, replace the current insert loop (lines 118-129) with a call to `_compare_and_resolve()`:

```python
# Replace:
#   for item in items:
#       content = item.get("content", "").strip()
#       ...
#       await self._memory.insert(...)

# With:
if items:
    inserted = await self._compare_and_resolve(items)
    logger.debug("Consolidation compared: %d items → %d inserted", len(items), inserted)
```

- [ ] **Step 4: Add Path import**

At top of `consolidator.py`, add:

```python
from pathlib import Path
```

- [ ] **Step 5: Verify import and prompt loading works**

```bash
conda run -n Elysia python -c "
from core.consolidator import MemoryConsolidator, _get_compare_prompt
p = _get_compare_prompt()
assert len(p) > 0
print('OK')
"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add core/consolidator.py
git commit -m "feat: consolidation compare — LLM-based insert/update/merge/skip with fallback"
```

---

### Task 3: MemoryReflector — 定期深度去重

**Files:**
- Create: `core/memory/memory_reflector.py`
- Modify: `core/consolidator.py:42-56` (__init__ — add mr_new_memory_count callback)

**Interfaces:**
- Consumes: `LongTermMemory.get_all()`, `LongTermMemory.update_text()`, `LongTermMemory.delete()`, `LongTermMemory._write_lock`
- Consumes: `LLMAdapter.think()` (for review calls)
- Consumes: `session.set_param()` / `session.get_param()` (counters + summary persistence)
- Produces: `MemoryReflector` class with `check_and_reflect()`, `restore_state()`, `flush()`

- [ ] **Step 1: Create MemoryReflector class skeleton**

Create `core/memory/memory_reflector.py`:

```python
"""MemoryReflector — 定期深度审查 LTM，合并相似、修正过时、删除无意义记忆。

多条件触发（轮次 / 新增记忆数），embedding 聚类分组 + LLM 批量审查。
"""

import asyncio
import json
import logging
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.memory.long_term import LongTermMemory
    from core.memory.session import SessionMemory
    from core.llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)

# MemoryReflector prompt — 文件路径，运行时加载
REFLECTOR_PROMPT_PATH = Path("prompt/memory_reflector.txt")
_REFLECTOR_PROMPT: str | None = None

def _get_reflector_prompt() -> str:
    global _REFLECTOR_PROMPT
    if _REFLECTOR_PROMPT is not None:
        return _REFLECTOR_PROMPT
    if REFLECTOR_PROMPT_PATH.exists():
        _REFLECTOR_PROMPT = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    else:
        _REFLECTOR_PROMPT = (
            "Review these long-term memories:\n{memories}\n\n"
            "Last run summary: {last_summary}\n\n"
            "Output merges/updates/deletes as JSON."
        )
    return _REFLECTOR_PROMPT


class MemoryReflector:
    """定期深度去重记忆。

    对标 CharacterReflector 的生命周期模式:
    - restore_state(): session.load() 之后从 params 恢复计数器
    - check_and_reflect(): 在 _after_response() 中调用，检查触发条件
    - flush(): shutdown 时持久化计数器到 session params
    """

    # 触发阈值
    ROUND_THRESHOLD = 50      # N 轮对话
    NEW_MEMORY_THRESHOLD = 30  # N 条新记忆
    CLUSTER_SIZE = 20          # 每组记忆数（上限 30）

    def __init__(
        self,
        llm: "LLMAdapter",
        memory: "LongTermMemory",
        session: "SessionMemory",
    ):
        self._llm = llm
        self._memory = memory
        self._session = session
        self._lock = asyncio.Lock()
        self._round_count = 0
        self._new_memory_count = 0
        self._last_summary: dict = {}

    # ---- 计数器 ----

    def increment_round(self) -> None:
        """每轮对话后 +1（在 _after_response 中调用）。"""
        self._round_count += 1

    def increment_new_memories(self, count: int) -> None:
        """consolidation 有新记忆后 +N。"""
        self._new_memory_count += count

    # ---- 触发检查 ----

    def _should_run(self) -> bool:
        """检查是否满足触发条件。"""
        return (
            self._round_count >= self.ROUND_THRESHOLD
            or self._new_memory_count >= self.NEW_MEMORY_THRESHOLD
        )

    async def check_and_reflect(self) -> dict | None:
        """在 _after_response() 中调用。满足条件则执行反射。返回执行摘要或 None。"""
        if not self._should_run():
            return None

        if self._lock.locked():
            logger.debug("MemoryReflector skipped — previous run still active")
            return None

        async with self._lock:
            return await self._do_reflect()

    # ---- 核心逻辑 ----

    async def _do_reflect(self) -> dict:
        """执行一次完整的去重周期。"""
        all_entries = self._memory.get_all()
        if not all_entries:
            self._reset_counters()
            return {"ran_at": _time.time(), "merges": 0, "updates": 0, "deletes": 0}

        # 1. 聚类分组
        groups = self._cluster_entries(all_entries)

        total_merges = 0
        total_updates = 0
        total_deletes = 0

        # 2. 逐组 LLM 审查
        for group in groups:
            try:
                actions = await self._review_group(group)
                # 预校验
                if not self._validate_actions(actions):
                    logger.warning("MemoryReflector: action validation failed for group, skipping")
                    continue
                # 执行
                m, u, d = await self._execute_actions(actions)
                total_merges += m
                total_updates += u
                total_deletes += d
            except Exception:
                logger.debug("MemoryReflector: group review failed", exc_info=True)

        # 3. 记录摘要 + 重置计数器
        summary = {
            "ran_at": _time.time(),
            "merges": total_merges,
            "updates": total_updates,
            "deletes": total_deletes,
        }
        self._last_summary = summary
        self._reset_counters()
        self._save_state()

        if total_merges or total_updates or total_deletes:
            logger.info(
                "MemoryReflector: %d merges, %d updates, %d deletes",
                total_merges, total_updates, total_deletes,
            )
        return summary

    def _cluster_entries(self, entries) -> list[list]:
        """将条目按相似度聚类分组，每组 ≤ CLUSTER_SIZE 条。

        首选 embedding KMeans，降级按 memory_type 分组 + oversized 拆分。
        """
        n = len(entries)
        if n <= self.CLUSTER_SIZE:
            return [entries]

        # 首选：embedding KMeans（要求全部条目都有 embedding，避免索引越界）
        embeddings = [e.embedding for e in entries if e.embedding]
        if len(embeddings) == len(entries):  # 100% 有 embedding
            try:
                from sklearn.cluster import KMeans
                import numpy as np
                n_clusters = max(1, (n + self.CLUSTER_SIZE - 1) // self.CLUSTER_SIZE)
                X = np.array(embeddings)
                kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
                labels = kmeans.fit_predict(X)
                groups: dict[int, list] = {}
                for i, e in enumerate(entries):
                    groups.setdefault(int(labels[i]), []).append(e)
                # 确保每组 ≤ CLUSTER_SIZE（KMeans 可能不均匀）
                result = []
                for g in groups.values():
                    if len(g) > self.CLUSTER_SIZE * 2:
                        # 二次拆分
                        for i in range(0, len(g), self.CLUSTER_SIZE):
                            result.append(g[i:i + self.CLUSTER_SIZE])
                    else:
                        result.append(g)
                return result
            except Exception:
                logger.debug("KMeans clustering failed, falling back to type-based")

        # 降级：按 memory_type 分组 + oversized 拆分
        by_type: dict[str, list] = {}
        for e in entries:
            by_type.setdefault(e.memory_type, []).append(e)
        result = []
        for type_entries in by_type.values():
            for i in range(0, len(type_entries), self.CLUSTER_SIZE):
                result.append(type_entries[i:i + self.CLUSTER_SIZE])
        return result

    async def _review_group(self, group) -> dict:
        """LLM 审查一组记忆，返回 actions dict。"""
        entries_json = [
            {"id": e.id, "type": e.memory_type, "content": e.text,
             "poignancy": e.poignancy, "timestamp": e.timestamp}
            for e in group
        ]
        prompt = _get_reflector_prompt().format(
            memories=json.dumps(entries_json, ensure_ascii=False, indent=2),
            last_summary=json.dumps(self._last_summary, ensure_ascii=False),
        )
        result = await self._llm.think(
            [{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        return self._parse_actions(result.text)

    @staticmethod
    def _parse_actions(text: str) -> dict:
        """解析 LLM 输出为 actions dict。"""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"merges": [], "updates": [], "deletes": []}

    @staticmethod
    def _validate_actions(actions: dict) -> bool:
        """预校验 actions 批次。"""
        if not isinstance(actions, dict):
            return False
        for key in ("merges", "updates", "deletes"):
            if key not in actions or not isinstance(actions[key], list):
                return False
        # 检查操作上限
        if len(actions["merges"]) > 5 or len(actions["updates"]) > 5 or len(actions["deletes"]) > 3:
            logger.warning("MemoryReflector: actions exceed per-group limits, rejecting")
            return False
        return True

    async def _execute_actions(self, actions: dict) -> tuple[int, int, int]:
        """执行 actions（merge → update → delete）。返回 (merges, updates, deletes) 计数。"""
        merges = updates = deletes = 0

        for m in actions.get("merges", []):
            ids = m.get("ids", [])
            merged = m.get("merged_content", "")
            if len(ids) >= 2 and merged:
                try:
                    # 更新第一条的内容，标记其余为 absorbed
                    await self._memory.update_text(
                        ids[0], merged,
                        poignancy=m.get("poignancy"),
                        memory_type=m.get("type"),
                    )
                    for absorbed_id in ids[1:]:
                        self._memory.mark_absorbed(absorbed_id)
                    merges += 1
                except Exception:
                    logger.debug("MemoryReflector merge failed for %s", ids[0], exc_info=True)

        for u in actions.get("updates", []):
            uid = u.get("id", "")
            new_content = u.get("new_content", "")
            if uid and new_content:
                try:
                    await self._memory.update_text(uid, new_content)
                    updates += 1
                except Exception:
                    logger.debug("MemoryReflector update failed for %s", uid, exc_info=True)

        for d in actions.get("deletes", []):
            did = d.get("id", "")
            if did:
                try:
                    await self._memory.delete(did)
                    deletes += 1
                except Exception:
                    logger.debug("MemoryReflector delete failed for %s", did, exc_info=True)

        return merges, updates, deletes

    # ---- 生命周期 ----

    def _reset_counters(self) -> None:
        self._round_count = 0
        self._new_memory_count = 0

    def restore_state(self) -> None:
        """从 session.params 恢复计数器和上次执行摘要。"""
        self._round_count = self._session.get_param("mr_round_count", 0)
        self._new_memory_count = self._session.get_param("mr_new_memory_count", 0)
        self._last_summary = self._session.get_param("mr_last_summary", {})

    async def flush(self) -> None:
        """持久化计数器和摘要到 session.params。"""
        self._save_state()

    def _save_state(self) -> None:
        self._session.set_param("mr_round_count", self._round_count)
        self._session.set_param("mr_new_memory_count", self._new_memory_count)
        self._session.set_param("mr_last_summary", self._last_summary)
```

- [ ] **Step 2: Verify import works**

```bash
conda run -n Elysia python -c "from core.memory.memory_reflector import MemoryReflector; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core/memory/memory_reflector.py
git commit -m "feat: MemoryReflector — periodic LTM dedup with KMeans clustering + LLM review"
```

---

### Task 4: Agent + CognitiveLoop — 生命周期接线

**Files:**
- Modify: `core/agent.py:88-89` (__init__ — create MemoryReflector), `core/agent.py:227-233` (_start_persistence — restore_state), `core/agent.py:417-418` (stop — flush)
- Modify: `core/cognitive_loop.py:755-775` (_after_response — increment counters + trigger)

**Interfaces:**
- Consumes: `MemoryReflector` (from Task 3)
- Modifies: `CognitiveLoop._after_response()` — adds counter increment + reflect check

- [ ] **Step 1: Create MemoryReflector in Agent.__init__**

In `core/agent.py`, `__init__`, after the CharacterReflector setup (around line 127), add:

```python
# ---- 记忆反射器 ----
self._memory_reflector = MemoryReflector(
    llm=self._llm,
    memory=self._long_term,
    session=self._session,
)
```

And add the import at top:

```python
from .personality import CharacterReflector, PersonalityEngine
from .memory.memory_reflector import MemoryReflector  # 新增
```

- [ ] **Step 2: Wire restore_state() in _start_persistence()**

In `_start_persistence()`, after the emotion restore_state call (line ~233), add:

```python
if self._memory_reflector:
    self._memory_reflector.restore_state()
```

- [ ] **Step 3: Wire flush() in stop()**

In `stop()`, after `_flush_reflector()` (around line 420), add:

```python
# 1.6. MemoryReflector 计数器落盘
if self._memory_reflector:
    await self._memory_reflector.flush()
```

- [ ] **Step 4: Inject MemoryReflector into CognitiveLoop**

In `__init__`, after creating `_cognitive`, add:

```python
self._cognitive.set_memory_reflector(self._memory_reflector)
```

And add the setter in `cognitive_loop.py`:

```python
def set_memory_reflector(self, reflector) -> None:
    self._memory_reflector = reflector
```

In `CognitiveLoop.__init__`, add:

```python
self._memory_reflector = None
```

In `cognitive_loop.py`, `_after_response()`, before the consolidation block, initialize `items = []`. Then after consolidation, add the MemoryReflector increment:

```python
items = []  # 初始化，避免 NameError
# ... existing consolidation block ...
if self._consolidator:
    try:
        items = await self._consolidator.consolidate(user_message, assistant_response)
        if items:
            logger.debug("记忆沉淀: %d 条 (%d ms)", ...)
    except ...
# ... existing reflector block ...

# 新增：更新 MemoryReflector 计数器
if self._memory_reflector:
    self._memory_reflector.increment_round()
    if items:
        self._memory_reflector.increment_new_memories(len(items))
    # 检查是否需要触发去重（非阻塞，快速判断）
    try:
        await self._memory_reflector.check_and_reflect()
    except Exception:
        logger.debug("MemoryReflector check failed", exc_info=True)

- [ ] **Step 6: Create placeholder prompt files**

```bash
touch prompt/consolidation_compare.txt
touch prompt/memory_reflector.txt
```

Add a comment header to each:

`prompt/consolidation_compare.txt`:
```
# Consolidation 比对 Prompt
# 占位文件 — 实际内容待 GPT 生成
# 需求规格见: prompt-requirements.md → Prompt A
```

`prompt/memory_reflector.txt`:
```
# MemoryReflector 去重 Prompt
# 占位文件 — 实际内容待 GPT 生成
# 需求规格见: prompt-requirements.md → Prompt B
```

- [ ] **Step 7: Verify imports work**

```bash
conda run -n Elysia python -c "from core.agent import Agent; print('OK')"
```

Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add core/agent.py core/cognitive_loop.py prompt/consolidation_compare.txt prompt/memory_reflector.txt
git commit -m "feat: wire MemoryReflector into Agent lifecycle + CognitiveLoop hooks"
```

---

### Task 5: Prompt + 文档

**Files:**
- Create: `prompt/consolidation_compare.txt` (replaced by GPT-generated prompt)
- Create: `prompt/memory_reflector.txt` (replaced by GPT-generated prompt)
- Modify: `CLAUDE.md` — add MemoryReflector to architecture section

- [ ] **Step 1: Update CLAUDE.md architecture section**

In `CLAUDE.md`, after the `CharacterReflector` line in the `core/` architecture table, add:

```
| `memory/memory_reflector.py` | MemoryReflector: periodic LTM dedup (KMeans clustering + LLM batch review)
```

- [ ] **Step 2: Note in prompt-requirements.md**

At top of `prompt-requirements.md`, add status line:

```
**状态:** 规格已定义，prompt 文本待 GPT 生成
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md prompt-requirements.md
git commit -m "docs: update CLAUDE.md + prompt requirements for LTM auto-update/dedup"
```

---

### Verification

After all tasks:

```bash
# Import check
conda run -n Elysia python -c "
from core.memory.long_term import LongTermMemory
from core.consolidator import MemoryConsolidator
from core.memory.memory_reflector import MemoryReflector
from core.agent import Agent
print('All imports OK')
"

# Tests
conda run -n Elysia python -m pytest core/tests/ -v -q 2>&1 | tail -5
```
