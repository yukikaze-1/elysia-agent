# 项目清理

2026-06-05 项目清理：磁盘垃圾、代码整合、补缺失模块文件。

## 范围

| 类别 | 项目 | 操作 |
|------|------|------|
| 磁盘 | `log/` 下所有日志文件 (27MB) | 删除 |
| 磁盘 | `.playwright-mcp/` (59MB) | 删除 |
| 磁盘 | `.claude/worktrees/server-restart/` | 删除 |
| 磁盘 | 所有 `__pycache__/` 目录 (54个) | 删除 |
| 磁盘 | `static/live2d-demo/` (188MB) | 删除 |
| 磁盘 | `data/sandbox/` 测试产物 | 删除 |
| 磁盘 | `.pytest_cache/` | 删除 |
| 代码整合 | UUID 生成统一用 `core.message_id.new_message_id()` | 替换 8 个文件中的内联 `uuid` 调用 |
| 补缺失 | `Module/config/__main__.py` | 新增 |
| 补缺失 | `core/input/__main__.py` | 新增 |
| 补缺失 | `Module/prompt/__main__.py` | 新增 |
| 补缺失 | `Module/logger/__main__.py` | 新增 |
| 补缺失 | `core/input/README.md` | 新增 |
| 文档 | `core/tools/test_tool_parallel.md` | 移动到 `docs/` |
| 配置 | `.claude/settings.local.json` 中过期 `demo.py` 引用 | 清理 9 条 |
| 文档 | `todo.md` 补充已知清理项 | 新增 bench/ 迁移备注 |

## 不变更

- `Module/bench/` — 保持原样，todo 备注后续迁移
- `Module/database/graphdb/` — 保持原样
- `Module/schema/` — 保持原样
- `LoggerFactory` — 保持原样
- `OutputManager.stream()` — 保持原样

## UUID 整合详情

替换 `import uuid` + `str(uuid.uuid4())` / `uuid.uuid4().hex[:N]` → `from core.message_id import new_message_id`：

- `Module/tool/safety.py` — HITL request_id
- `core/memory/long_term.py` — MemoryEntry.id
- `core/input/server.py` — WS client_id
- `Module/database/documentdb/__main__.py` — 测试 message_id
- `Module/schema/event.py` — Event.id
- `Module/schema/message.py` — UserMessage/ChatMessage.message_id
- `core/cognitive_loop.py` — trace_id
- `core/task.py` — Task.id

同步修复 2 个测试中对旧 ID 长度的硬编码断言（`test_phase2.py`、`test_task_model.py`）。

## 验证

```bash
python -m pytest Module/ -v --asyncio-mode=auto    # 594 passed
python -m pytest core/tests/ -v --asyncio-mode=auto # 310 passed
```
