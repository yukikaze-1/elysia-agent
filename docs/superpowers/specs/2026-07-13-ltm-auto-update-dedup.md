# LTM 自动更新与去重

**日期:** 2026-07-13
**状态:** 待实现

## 背景

当前 MemoryConsolidator 只做增量插入——每 6 对对话调用 LLM 提取新记忆，直接 `insert()`。它不会回看已有记忆，不会检测冲突/过时/重复。用户需要在 debug 页面手动编辑和删除。

## 目标

1. **Consolidation 时轻量比对** — 每次提取新记忆后，与相关已有记忆比对，自动决定新增/更新/合并/跳过
2. **MemoryReflector 定期深度去重** — 多条件触发，分批审查记忆，合并相似、修正过时

## 设计

### 1. 数据模型扩展

#### 1.1 LongTermMemory 内部 `_absorbed_ids`

```python
# long_term.py — __init__
self._absorbed_ids: set[str] = set()  # 被 merge 吸收但尚未 delete 的条目 ID
```

- consolidation merge 执行后：`self._absorbed_ids.add(absorbed_id)`
- MemoryReflector delete 执行后：`self._absorbed_ids.discard(id)`
- `delete()` 方法：同时从 `_absorbed_ids` 中移除
- `clear_all()` 方法：同时清空 `_absorbed_ids`

#### 1.2 search() 过滤已吸收条目

```python
# long_term.py — search()
# 搜索时排除被吸收条目，防止污染结果
candidates = [e for e in self._entries if e.id not in self._absorbed_ids]
```

#### 1.3 新增 get_all() 方法

```python
# long_term.py
def get_all(self) -> list[MemoryEntry]:
    """返回全部活跃条目的副本（排除已吸收条目）。"""
    return [e for e in self._entries if e.id not in self._absorbed_ids]
```

#### 1.4 新增 mark_absorbed() 方法

```python
# long_term.py
def mark_absorbed(self, entry_id: str) -> None:
    """标记条目已被 merge 吸收（不删除，只标记）。"""
    self._absorbed_ids.add(entry_id)
```

#### 1.5 扩展 update_text() 支持更新 poignancy 和 memory_type

merge 产生的新记忆可能有不同的类型和重要性。扩展签名：

```python
# long_term.py
async def update_text(
    self, entry_id: str, new_text: str,
    poignancy: int | None = None,
    memory_type: str | None = None,
) -> bool:
```

内部更新时，若参数不为 None 则一并修改 `e.poignancy` / `e.memory_type`，并同步到 Milvus metadata。

### 2. 驱逐暂停

开发阶段暂不禁用记忆驱逐：

```python
# long_term.py — insert()
self._entries.append(entry)
# TODO: 开发阶段暂不禁用驱逐，避免丢失测试记忆
# self._evict_old_entries()
```

### 3. Consolidation 时轻量比对

**触发时机：** MemoryConsolidator 每 6 对对话提取完新记忆后（`_do_consolidate` 末尾）

**流程：**

```
新记忆 (1-5条)
  → embedding 搜索已激活记忆 (top 10 最相关)
  → LLM 比对: 新记忆 + 相关已有记忆
  → 对每条新记忆输出 action: insert | update | merge | skip
  → 预校验 LLM 输出（target_id 存在性、字段完整性）
  → 校验通过 → 执行（merge → update → insert，skip 无操作）
  → 校验失败或异常 → 回退: 全部新记忆走 insert（不阻塞 consolidation）
```

**LLM 输入规格：**

| 字段 | 内容 |
|------|------|
| 新提取的记忆 | 本轮 LLM 输出，每条含 type / content / poignancy |
| 相关已激活记忆 | embedding 搜索 top 10，每条含 id / type / content / poignancy / timestamp |

**LLM 输出规格：**

对每条新记忆输出一个 action：

| action | 条件 | 额外输出字段 |
|--------|------|-------------|
| `insert` | 新信息，不与任何已有记忆冲突 | content, type, poignancy |
| `update` | 与已有记忆矛盾或明显过时 | **target_id**, new_content |
| `merge` | 与已有记忆高度相似，合并为一条更完整的 | **target_id**, merged_content |
| `skip` | 完全重复 | — |

**执行细节：**
- merge → `long_term.update_text(target_id, merged_content)` + `long_term.mark_absorbed(absorbed_id)`
- update → `long_term.update_text(target_id, new_content)`
- insert → `long_term.insert()`
- 被 mark_absorbed 的条目：不立即删除，Milvus 向量保留；MemoryReflector 后续清理
- 预校验：执行前验证所有 target_id 存在且未被吸收

**关键约束：**
- 不强制匹配——没冲突就 insert
- update/merge 必须带 target_id
- 输出 JSON 数组，失败回退全 insert
- 执行前预校验批次完整性，校验失败跳过整个比对结果

### 4. MemoryReflector 定期深度去重

**新增文件：** `core/memory/memory_reflector.py`

#### 4.1 触发条件（两条件，取先到达）

时间条件已移除——仅在 response hook 中检查，空闲时不触发。改为两个条件：

| 条件 | 阈值 | 计数器 |
|------|------|--------|
| N 轮对话 | 50 轮 | session param `mr_round_count` |
| N 条新记忆 | 30 条 | session param `mr_new_memory_count` |

计数器在 `_after_response()` 中更新（每轮 +1），consolidation 有新 insert 时 `mr_new_memory_count` +N。

触发后所有计数器重置为 0。

#### 4.2 分页策略（防止 LLM 上下文溢出）

首选方案：**embedding 相似度聚类**

```
全部已激活记忆的 embeddings
  → sklearn KMeans (n_clusters = ceil(total / 20))
  → 每簇作为一组（每组约 20 条，上限 30）
  → 每组独立 LLM 审查
  → 串行执行各组结果
```

降级方案（embedding 不可用时）：

```
按 memory_type 分组 → 每组 ≤ 30 条
超过 30 条的同类型组 → 按 timestamp 分片（每片 30 条）
```

跨组去重由 LLM 在每组内完成。不同组的相似记忆合并留给下次反射周期。

#### 4.3 流程

```
触发条件满足
  → 获取全部已激活记忆 (get_all)
  → 按 type 或 embedding 聚类分组（每组 ≤ 30 条）
  → 每组: LLM 审查 → 输出 merges / updates / deletes
  → 汇总所有组输出
  → 预校验（target_id 存在性、去重 operands 不冲突）
  → 执行（先 merge → 后 update → 最后 delete）
  → 记录上次执行摘要
  → 重置计数器
```

#### 4.4 LLM 输入规格

| 字段 | 内容 |
|------|------|
| 本组记忆 | ≤ 30 条，每条含 id / type / content / poignancy / timestamp |
| 上次执行摘要 | 上次做了哪些变更（如有） |

#### 4.5 LLM 输出规格

```json
{
  "merges": [
    {"ids": ["id1","id2"], "merged_content": "...", "type": "...", "poignancy": 8}
  ],
  "updates": [
    {"id": "id3", "new_content": "...", "reason": "用户纠正/信息过时"}
  ],
  "deletes": [
    {"id": "id4", "reason": "已被合并/完全过时/不重要"}
  ]
}
```

| 操作 | 条件 |
|------|------|
| merge | 两条及以上高度相似（同一事件的不同碎片、同一偏好的多次表述） |
| update | 内容过时、被用户明确纠正、事实已变化 |
| delete | 已被 merge 吸收、琐碎无意义、纯噪音 |

**关键约束：**
- **保守原则**：不确定时倾向于保留
- 合并后 content 必须包含原记忆关键信息，不缩水
- 不需要的操作写空数组 `[]`
- 每组操作上限：merge ≤ 5 组，update ≤ 5 条，delete ≤ 3 条

### 5. 并发控制

#### 5.1 共享锁

consolidation compare 和 MemoryReflector 操作同一个 LongTermMemory。加共享锁防止交错执行：

```python
# long_term.py — __init__
self._write_lock = asyncio.Lock()
```

所有修改方法（`insert`, `update_text`, `delete`, `mark_absorbed`, `clear_all`）获取此锁。

#### 5.2 MemoryReflector 自身锁

MemoryReflector 加 `asyncio.Lock`，防止多个反射周期并发（类似 CharacterReflector）。

### 6. 生命周期

对标 CharacterReflector，补上持久化和恢复钩子：

#### 6.1 restore_state()

在 `agent.py` `_start_persistence()` 中，`session.load()` 之后调用：

```python
if self._memory_reflector:
    self._memory_reflector.restore_state()
```

职责：从 session.params 恢复上次执行摘要（`mr_last_summary`）、计数器（`mr_round_count`、`mr_new_memory_count`）。

#### 6.2 flush()

在 `agent.py` `stop()` 中，`_flush_reflector()` 之后调用：

```python
if self._memory_reflector:
    await self._memory_reflector.flush()
```

职责：将当前执行摘要 + 计数器持久化到 session.params（`mr_last_summary`、`mr_round_count`、`mr_new_memory_count`）。

#### 6.3 执行摘要持久化

每次 MemoryReflector 执行完成后，将本次变更摘要写入 `session.set_param("mr_last_summary", summary)`。摘要格式：

```json
{
  "ran_at": 1720857600.0,
  "merges": 3,
  "updates": 2,
  "deletes": 1
}
```

下次 LLM 审查时将此摘要传入 prompt，防止重复审查同一批记忆。

### 7. 部分失败处理

执行前预校验整个 LLM 输出批次。校验通过后执行——执行中某操作失败不阻塞后续操作（记录日志 + 跳过）。

```
预校验 → 通过
  → execute merge_1  → 成功/失败（记日志）
  → execute merge_2  → 成功/失败（记日志）
  → execute update_1 → ...
  → execute delete_1 → ...
  → 汇总结果 → 记录摘要
```

不回滚已执行操作。失败项记录到下次执行摘要中供重试。

### 8. 执行顺序（不变）

**Consolidation 比对：**
```
merge → update → insert
```

**MemoryReflector 去重：**
```
merge → update → delete
```

### 9. 不做的

- 被 merge 吸收的记忆在 consolidation 比对阶段不自动删除（mark_absorbed + MemoryReflector 后续清理）
- 不修改 `_evict_old_entries()` 的内部逻辑（只注释调用）
- MemoryReflector 不访问对话历史（只审查 LTM 记忆本身）
- 不引入后台定时器（时间触发条件已移除）

## 涉及文件

| 文件 | 动作 | 说明 |
|------|------|------|
| `core/memory/long_term.py` | 修改 | `_absorbed_ids` + `_write_lock`；注释 `_evict_old_entries()`；新增 `get_all()`、`mark_absorbed()`；扩展 `update_text()` 支持 poignancy/memory_type；`search()` 过滤已吸收；修改方法加锁 |
| `core/consolidator.py` | 修改 | `_do_consolidate` 末尾加比对逻辑，新增 `_compare_and_resolve()` |
| `core/memory/memory_reflector.py` | **新建** | MemoryReflector 类（含聚类分组 + 并发控制 + restore_state/flush） |
| `core/agent.py` | 修改 | 创建 MemoryReflector；`_start_persistence()` 中 L232 调 `restore_state()`；`stop()` 中 `_flush_reflector` 后调 `flush()` |
| `core/cognitive_loop.py` | 修改 | post-response hooks 中更新计数器 + 触发 MemoryReflector 检查 |
| `prompt/` | 新增 | Consolidation 比对 prompt + MemoryReflector prompt（规格已定义，文本交 GPT 生成） |
