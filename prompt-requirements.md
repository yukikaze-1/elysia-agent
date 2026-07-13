# LTM 自动更新与去重 — Prompt 生成需求

此文件交给 GPT / LLM 生成实际 prompt 文本。两个 prompt 分别对应 Consolidation 比对和 MemoryReflector 去重。

`prompt/MicroReflector.j2` 和 `prompt/MacroReflector.j2` 可作为风格参考（不直接复用）。

---

## Prompt A: Consolidation 时轻量比对

### 角色定义

你是一个记忆一致性检查器。你的任务是比对"新提取的记忆"和"已有相关记忆"，判断每条新记忆应该插入、更新已有、合并还是跳过。

### 输入

1. **新提取的记忆**（1-5 条）：本轮 consolidation 从对话中提取的记忆，每条含 type / content / poignancy
2. **相关已有记忆**（≤ 10 条）：从向量数据库中搜索出的最相关已有记忆，每条含 id / type / content / poignancy / timestamp

### 输出格式

对每条新记忆输出一个 action 的 JSON 数组：

```json
[
  {
    "action": "insert | update | merge | skip",
    "content": "...",
    "type": "Fact | Preference | Event | Plan | Note",
    "poignancy": 5,
    "target_id": "msg_xxx（仅 update/merge 需要）",
    "merged_content": "...（仅 merge 需要，包含原记忆关键信息）",
    "new_content": "...（仅 update 需要）"
  }
]
```

### Action 判断规则

| action | 条件 | 示例 |
|--------|------|------|
| `insert` | 新信息，不与任何已有记忆冲突 | 第一次提到某个事实 |
| `update` | 与已有记忆**矛盾**或**明显过时** | "养了一只猫"→"养了两只猫" |
| `merge` | 与已有记忆**高度相似**，合并为更完整的一条 | 同一事件的两次碎片描述 |
| `skip` | 完全重复，已有记忆已包含所有信息 | 完全相同的内容再次提取 |

### 关键约束

- 不强制匹配——没冲突就 insert
- update/merge 必须带 target_id（已有记忆的 id）
- merge 的 merged_content 必须包含原记忆关键信息，不缩水
- 输出纯 JSON 数组，不含 markdown 代码块
- 不确定时倾向于 insert（保守原则）

---

## Prompt B: MemoryReflector 定期深度去重

### 角色定义

你是一个记忆管理专家。你的任务是审查一组已有的长期记忆，识别应该合并的相似条目、应该更新的过时条目和应该删除的无意义条目。

### 输入

1. **本组记忆**（≤ 30 条）：一组已按相似度聚类的长期记忆，每条含 id / type / content / poignancy / timestamp
2. **上次执行摘要**（可能为空）：上次去重做了哪些变更，避免重复审查

### 输出格式

一个 JSON 对象，三组操作列表：

```json
{
  "merges": [
    {
      "ids": ["msg_xxx", "msg_yyy"],
      "merged_content": "合并后的第一人称叙事文本（中文）",
      "type": "Fact | Preference | Event | Plan | Note",
      "poignancy": 8
    }
  ],
  "updates": [
    {
      "id": "msg_zzz",
      "new_content": "更新后的第一人称叙事文本（中文）",
      "reason": "用户纠正 | 信息过时 | 事实变化"
    }
  ],
  "deletes": [
    {
      "id": "msg_www",
      "reason": "已被合并 | 完全过时 | 琐碎无意义"
    }
  ]
}
```

### 操作判断规则

| 操作 | 条件 | 注意事项 |
|------|------|----------|
| merge | 两条及以上**高度相似**，如：同一事件的多次片段描述、同一偏好的重复表述 | 合并后 content 必须包含所有原记忆的关键信息，不缩水。取最高 poignancy 或适当提升 |
| update | 内容**明显过时**、被用户明确纠正、事实已发生变化 | 仅更新真正过时的，不确定就保留 |
| delete | 已被 merge 吸收、纯噪音、琐碎到无意义（如"用户今天说了你好"） | 保守删除，不确定时保留 |

### 关键约束

- **保守原则**：不确定时倾向于保留，不强行合并/删除
- 不需要的操作写空数组 `[]`
- 每组操作上限：merge ≤ 5 组，update ≤ 5 条，delete ≤ 3 条
- 合并后的 content 必须是第一人称中文叙事（"我注意到..."、"妖梦告诉我..."）
- 输出纯 JSON 对象，不含 markdown 代码块
- 如果上次执行摘要提到已处理过这些记忆，直接返回空操作

---

## 参考文档

- 项目 CLAUDE.md: 代码结构和架构信息
- `prompt/MicroReflector.j2`: 风格参考（逐对话提取记忆）
- `prompt/MacroReflector.j2`: 风格参考（日记合成 + 情感分析）
- Spec: `docs/superpowers/specs/2026-07-13-ltm-auto-update-dedup.md`
