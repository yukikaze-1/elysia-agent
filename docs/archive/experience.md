> ⚠️ 已归档（2026-08-09）：历史记录，不再随代码库更新。当前有效文档见 [../README.md](../README.md)。

# 开发经验记录

## 2026-07-13：数据清理——前后端三层缓存连环坑

### 问题

清理完数据库（MongoDB + Milvus）后重启服务，浏览器仍然显示旧对话消息。

### 根因

消息缓存存在于**三个独立层级**，只清一层不够：

```
浏览器 localStorage (elysia_chat)
  → 服务端 session._messages (内存)
    → MongoDB sessions 集合 (持久化)
```

**清理时序漏洞：** 服务 shutdown 时 `Agent.stop()` 调用 `session.save()`，把当前内存中的旧消息写回了刚刚清空的 MongoDB。下次启动 `session.load()` 又把旧消息加载回来。浏览器 localStorage 再缓存一份。

### 正确清理顺序

1. **关停服务**
2. **清 MongoDB**（sessions + message_archive + token_usage）
3. **清 Milvus/LTM**（`long_term.clear_all()`）
4. **启动服务**（此时 session.load() 找不到文档，创建空 deque）
5. **浏览器 Ctrl+Shift+R 硬刷新**（跳过静态文件缓存加载新版 JS）

### 后续改进

- 移除了前端 localStorage 消息缓存（`persistence.js`），消息现在仅从服务端 WebSocket 推送
- 页面加载时不再从 localStorage 恢复，每次从零开始

---

## 2026-07-13：Prompt 文件管理与命名规范

### 问题

三个 prompt 文件命名不统一（.txt vs .j2，snake_case vs PascalCase），路径硬编码导致 Linux 大小写敏感文件系统找不到文件。

### 结论

- `core/prompts/` 目录下所有 prompt 模板统一使用 **PascalCase `.j2`** 扩展名，与现有 `MicroReflector.j2`、`MacroReflector.j2` 一致
- 代码中文件路径使用 `Path("core/prompts/ExactName.j2")`，确保大小写匹配（实际通过 `PromptManager` 相对 `__file__` 加载，不受 cwd 影响）
- GPT 生成的 prompt 参考文件放入 `core/prompts/ref/`，使用 `snake_case.md`
- Prompt 需求文档放在根目录，交付 GPT 后移入 `prompt/ref/`

### 现有 Prompt 清单

| 文件 | 用途 | 加载方 |
|------|------|--------|
| `ExtractMemory.j2` | 从对话提取记忆 | `core/memory/consolidator.py` |
| `ConsolidationCompare.j2` | 新记忆 vs 已有记忆比对 | `core/memory/consolidator.py` |
| `MemoryReflector.j2` | 定期深度去重 | `core/memory/memory_reflector.py` |

### Prompt 渲染方式

prompt 文件包含 JSON 示例（大量 `{` `}`），使用 Python `.format()` 会炸。统一用 `str.replace("{variable}", value)` 避免转义大括号。

---

## 2026-07-13：`conda run` stdout 缓冲

### 问题

`start.sh` 使用 `conda run -n Elysia python app.py` 启动，终端在启动横幅后卡住——服务实际已运行，但 `print()` 输出被 conda 缓冲直到进程退出才显示。

### 修复

加 `--no-capture-output`：`conda run -n Elysia --no-capture-output python app.py`

---

## 2026-07-13：httpx + `ALL_PROXY=socks://` 冲突

### 问题

系统环境变量 `ALL_PROXY=socks://127.0.0.1:7897/` 导致 `httpx`（openai 库底层 HTTP 客户端）启动崩溃：`ValueError: Unknown scheme for proxy URL URL('socks://...')`。

`httpx` 只支持 `http://`、`https://`、`socks5://`，不支持 `socks://`。

### 修复

`start.sh` 启动前 `export ALL_PROXY="" all_proxy=""` 清除。

---

## 2026-07-13：`BASH_SOURCE` + 符号链接路径解析

### 问题

根目录 `start.sh` → `scripts/start.sh` 符号链接。`scripts/start.sh` 中使用 `dirname "${BASH_SOURCE[0]}"` 拿到的是 `./start.sh`（符号链接路径），`cd ..` 跳到了 `/home/yomu/` 而非 `/home/yomu/Agent/`。

### 修复

`readlink -f "${BASH_SOURCE[0]}"` 解析真实路径。

---

## 2026-07-13：`motor` 插入时修改传入 dict

### 问题

`motor`（MongoDB 异步驱动）的 `insert_one()` 会在传入的 dict 上注入 `_id: ObjectId` 字段，导致下次 `session.save()` 时尝试把 `ObjectId` 序列化到 JSON。

### 修复

`insert_one()` / `insert_many()` 调用时传入 `dict(document)` 副本，`session.load()` 恢复时 `m.pop("_id", None)` 移除。

---

## 2026-07-13：Debug 页面 DOM 重建丢失状态

### 问题

Debug 数据库面板每次 poll 时用 `innerHTML` 重建整个 DOM，导致搜索框输入值、面板折叠状态丢失。

### 修复

重建前保存状态（搜索值、过滤器选择、折叠布尔值），`innerHTML` 后恢复。`refreshArchive()` 同样处理。
