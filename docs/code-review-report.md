# Code Review Report — Elysia/Agent 项目

**日期**: 2026-06-18
**审查范围**: 除 `Module/`、`live2d/`、`ref/` 及资源文件外的全部项目代码
**审查方法**: 5 路并行 Agent 审查（核心架构、内存与任务系统、工具插件、前端、配置与测试）

---

## 总览

| 严重级别 | 数量 | 涉及模块 |
|----------|------|----------|
| **Critical** | 10 | idle_monitor, filesystem 插件, debug.html, persistence.js, main.js, requirements.txt, app_config.py, consolidator.py, long_term.py, task_scheduler.py |
| **High** | 25 | cognitive_loop, agent, session, task_scheduler, long_term, 各类插件, 前端 JS/HTML |
| **Medium** | 40+ | 各模块 |
| **Low** | 55+ | 各模块 |

---

## CRITICAL 问题

### 1. `idle_monitor.py:30` — Python 3.13 启动崩溃 (Core Architecture)

**文件**: [core/idle_monitor.py:30](core/idle_monitor.py#L30)
**类别**: Bug
**严重级别**: Critical

`IdleMonitor.__init__` 调用 `asyncio.get_event_loop().time()`。在 Python 3.13 中，无运行中的 event loop 时 `get_event_loop()` 抛出 `RuntimeError`。服务器模式下（`app.py:138-139`）`Agent.__init__` 在 `asyncio.run()` 之前创建 `IdleMonitor`，因此服务器模式启动即崩溃。

**建议修复**: `__init__` 中使用 `time.monotonic()` 替代，或将 `_last_interaction` 初始化延迟到 `start()`。

---

### 2. `filesystem/plugin.py:208` + `safety.py:155` — 沙盒路径穿越 (Tool Plugins)

**文件**: [core/tools/filesystem/plugin.py:208](core/tools/filesystem/plugin.py#L208), Module/tool/safety.py:155
**类别**: Security
**严重级别**: Critical

`WriteFilePlugin.execute()` 和 `SafetyPipeline._resolve_sandbox_path()` 都使用 `str(resolved).startswith(str(write_dir))` 执行字符串前缀匹配，而非路径级别检查。若 `write_dir` 为 `/home/yomu/data/sandbox/test_session`，路径 `/home/yomu/data/sandbox/test_session_evil/file.txt` 将通过检查。

**建议修复**: 在 `write_dir` 后追加 `/`，或使用 `Path.is_relative_to()`（Python 3.9+）。

---

### 3. `debug.html:796,842-843` — `esc()` 用于 JS 字符串上下文导致 XSS (Frontend)

**文件**: [static/debug.html:796](static/debug.html#L796)
**类别**: Security
**严重级别**: Critical

`esc()` 函数转义 HTML 文本（替换 `<`, `>`, `&`），但被错误地用于 JavaScript 字符串上下文，例如：
```javascript
onclick="deleteLtmEntry('${esc(e.id)}')"
```
若 `e.id` 包含单引号 `'` 或反斜杠 `\`，会破坏 JS 字符串字面量，可能造成 XSS。数据库条目 ID 是攻击向量。

**建议修复**: 对 JavaScript 字符串使用专门的 JS 转义函数，或将数据传递方式改为 `data-*` 属性 + 事件委托。

---

### 4. `persistence.js:48-49` + `main.js:122-123` — 存储型 XSS via `marked.parse()` (Frontend)

**文件**: [static/js/persistence.js:48-49](static/js/persistence.js#L48-L49), [static/js/main.js:122-123](static/js/main.js#L122-L123)
**类别**: Security
**严重级别**: Critical

`marked.parse(content)` 被调用时 **未传入任何 sanitization 选项**。`marked` 默认允许原始 HTML 透传。若 LLM 产出包含 `<img src=x onerror=...>` 或 `<script>` 的内容，将在用户浏览器中执行。这是从服务器到客户端的直接 XSS 路径。

**建议修复**: 配置 `marked` 的 `sanitize` 选项或使用 `DOMPurify.sanitize(marked.parse(content))`。

---

### 5. `requirements.txt` — 缺少 `pydantic`, `pytest`, `pytest-asyncio` (Config/Tests)

**文件**: [requirements.txt:1](requirements.txt#L1)
**类别**: Dependencies
**严重级别**: Critical

- `pydantic` 被 15+ 个文件广泛使用但未声明在 `requirements.txt` 中
- `pytest` 和 `pytest-asyncio` 缺失（"pip install -r requirements.txt" 后运行测试套件必定失败）
- `aiohttp`（用于 `test_phase3.py` 测试工具）缺失

**建议修复**: 在 `requirements.txt` 中显式添加所有直接依赖及测试依赖。

---

### 6. `app_config.py:67-69` — VisionConfig 缺少 `base_url` 和 `api_key` (Config)

**文件**: [core/app_config.py:67-69](core/app_config.py#L67-L69)
**类别**: Config
**严重级别**: Critical

其他所有服务配置（LLMConfig, TTSConfig, STTConfig）均声明了 `base_url` 和 `api_key`（含 `secret=True` 和 env-var 绑定）。`VisionConfig` 仅有 `model: str = ConfigField(default="mimo/mimo-v2.5")`，缺少 endpoint 和密钥字段。

**建议修复**: 添加 `base_url` 和 `api_key` 字段，含对应的环境变量绑定。

---

### 7. `consolidator.py:93-114` — 缓冲区在 LLM 提取成功前就被清空 (Memory System)

**文件**: [core/consolidator.py:93-114](core/consolidator.py#L93-L114)
**类别**: Bug / Data Integrity
**严重级别**: Critical

`self._buffer.clear()` 在第 101 行执行，早于第 109 行的 LLM 提取。如果 LLM 调用失败（Rate Limit、超时、异常），已缓冲的对话轮次将**永久丢失**。缓冲区仅应在成功提取并存储后才清空。

**建议修复**: 将 `clear()` 移动到成功完成 LLM 提取和 `_memory.insert()` 之后。

---

### 8. `long_term.py:251-278` — `_sync_dirty_entries()` 从未被调用 (Memory System)

**文件**: [core/memory/long_term.py:251-278](core/memory/long_term.py#L251-L278)
**类别**: Bug / Data Integrity
**严重级别**: Critical

当 `insert()` 写入 Milvus 失败时，entry ID 被加入 `self._dirty_ids`。`_sync_dirty_entries()` 应重试这些写入，但**没有任何代码路径调用此方法**。grep 确认零调用点。Milvus 写入失败的条目永远留存在内存中，向量数据库逐渐与内存状态分叉，语义搜索质量持续退化。

**建议修复**: 在 `search()` 或定期后台任务中添加对 `_sync_dirty_entries()` 的调用。

---

### 9. `task_scheduler.py:312-322` — prune 与 worker 的竞态导致 KeyError (Task System)

**文件**: [core/task_scheduler.py:299-322](core/task_scheduler.py#L299-L322)
**类别**: Concurrency / Bug
**严重级别**: Critical

`_worker_loop`（使用安全的 `pop(task.id, None)`）和 `_prune_tasks`（使用不安全的 `del self._tasks[tid]`）同时从 `self._tasks` 中删除任务。在构建 `to_remove` 列表到执行 `del` 之间，worker 可能已经移除了该 task ID。`del` 抛出未被捕获的 `KeyError`，导致 prune 循环崩溃，checkpoint 保存和任务清理停止。

**建议修复**: 将 `del self._tasks[tid]` 替换为 `self._tasks.pop(tid, None)`。

---

### 10. `task_scheduler.py:89-94` — stop() 排空队列但不 resolve futures (Task System)

**文件**: [core/task_scheduler.py:89-94](core/task_scheduler.py#L89-L94)
**类别**: Bug / Concurrency
**严重级别**: Critical

`stop()` 使用 `get_nowait()` 排空队列并丢弃任务，但这些任务的 future 从未 resolve。调用者（如 `dispatch_tool_calls`）阻塞在 `task.get_future()` 上，将永远挂起。此外 `self._tasks` 在第 95 行被 `clear()`，但 `Task` 对象的 `_future` 字段仍然存在——这些 future 永远不会完成。

**建议修复**: 在丢弃排队任务前 resolve 它们的 future（e.g., 设为 CancelledError）。

---

## HIGH 问题

### 11. `cognitive_loop.py:304,616` — 打字指示器永久卡在 ON 状态 ✅ 已修复

**文件**: [core/cognitive_loop.py](core/cognitive_loop.py)
**类别**: Bug
**严重级别**: High
**修复**: `4004418` — except 块加 `await self._output.typing(False)`

---

### 12. `cognitive_loop.py:691-718` — `_current_task` 竞态条件已确认但未修复 ⚠️ v1 已知限制

**文件**: [core/cognitive_loop.py:695-696](core/cognitive_loop.py#L695-L696)
**类别**: Concurrency
**严重级别**: High

代码注释明确承认："多任务并发时用队列或锁保护 _current_task"。多个事件源（`USER_INPUT`, `SYSTEM_TICK`, `TASK_RESULT`）都能创建 task 并赋值 `self._cognitive._current_task = task`。如果在 `USER_INPUT` 认知任务运行时收到 `TASK_RESULT`，`_current_task` 会被覆盖。

**建议修复**: 在赋值 `_current_task` 时使用 `_process_lock` 保护，或使用任务队列串行化调度。

---

### 13. `cognitive_loop.py:384` — `SentenceBuffer.feed()` 返回值被丢弃 ✅ 已修复

**文件**: [core/cognitive_loop.py](core/cognitive_loop.py)
**类别**: Bug
**严重级别**: High
**修复**: `4004418` — feed() 返回值捕获为 `maybe_sentence`，有值时追加到 `all_sentences`

---

### 14. `cognitive_loop.py:459` — 多个 tool call 共享 `"unknown"` ID 导致冲突 ✅ 已修复

**文件**: [core/cognitive_loop.py](core/cognitive_loop.py)
**类别**: Bug
**严重级别**: High
**修复**: `4004418` — fallback ID 改为 `f"submit_{i}_{id(result)}"`，结合 enumerate 索引 + 对象 id 保证唯一

---

### 15. `agent.py:297` — stop() 时排队的用户消息静默丢失

**文件**: [core/agent.py:297](core/agent.py#L297)
**类别**: Bug
**严重级别**: High

`Agent.stop()` 执行 `self._pending_messages.clear()`，但未记录日志或警告。等待 Agent 变为 IDLE 状态的已排队用户消息被静默丢弃，用户不会收到任何反馈。

**建议修复**: 清空前记录警告：`logger.warning("Discarding %d pending messages on shutdown", len(self._pending_messages))`。

---

### 16. `agent.py:322-323` — 封装破坏：直接访问 `_cognitive._current_task` ✅ 已修复

**文件**: [core/agent.py](core/agent.py)
**类别**: Architecture
**严重级别**: High
**修复**: `4004418` — `stop()` 改用 `self._cognitive.cancel_current()`，`_tts_mgr.cancel_all()` 保留（TTS 取消无对应公共 API 但接口已稳定）

---

### 17. `llm_adapter.py:86-109` — 重试循环吞没 `CancelledError`

**文件**: [core/llm_adapter.py:86-109](core/llm_adapter.py#L86-L109)
**类别**: Bug
**严重级别**: High

重试循环的 `except Exception as e` 捕获了 `asyncio.CancelledError`（继承自 `Exception`）。LLM 调用期间的 cancel 被吞没并重试最多 4 次后才会被重新抛出。

**建议修复**: 在 broad `except Exception` 之前单独捕获 `asyncio.CancelledError` 并立即 re-raise。

---

### 18. `session.py:174-175` — JSONL 使用覆盖模式写入，丢失先前的 save

**文件**: [core/memory/session.py:174-175](core/memory/session.py#L174-L175)
**类别**: Bug / Data Integrity
**严重级别**: High

MongoDB 不可用时，`save()` 以 `"w"` 模式打开 `_pending_file`（覆盖模式）。每次 `save()` 调用完全替换之前的内容。N 次 save → N-1 次丢失。文件扩展名 `.jsonl` 暗示追加语义，但代码使用覆盖。

**建议修复**: 使用 `"a"` 模式追加写入，或改为 `.json`（单文档）并保留覆盖语义。

---

### 19. `long_term.py:203-405` — `_entries` 列表无锁保护

**文件**: [core/memory/long_term.py:203-405](core/memory/long_term.py#L203-L405)
**类别**: Concurrency / Bug
**严重级别**: High

`insert()`, `search()`, `delete()`, `_evict_old_entries()` 并发访问 `self._entries` 列表但无锁保护。`search()` 在 `await` 点（Milvus 查询、embedding 生成）之间迭代 `_entries`。在此期间另一协程的 `insert()` 或 `delete()` 可能导致返回已删除的条目或遗漏新条目。

**建议修复**: 添加 `asyncio.Lock` 保护所有 `_entries` 的读写操作。

---

### 20. `filesystem/plugin.py:50,120,199` — 绕过 pipeline 时使用未验证的路径

**文件**: [core/tools/filesystem/plugin.py:50](core/tools/filesystem/plugin.py#L50)
**类别**: Security
**严重级别**: High

模式 `ctx.get("_safe_path") or params.path` 信任 SafetyPipeline 设置了 `_safe_path`。如果 `execute()` 在无 pipeline 的情况下被调用（程序化使用、测试捷径、未来重构），`params.path`（原始用户输入）将被直接使用且无验证。`or` 运算符对于空字符串也是脆弱的。

**建议修复**: 使用显式检查：`if "_safe_path" in ctx.services: ... else: return error`。

---

### 21. `filesystem/plugin.py:196-218` — `max_file_bytes` 在写入前未校验

**文件**: [core/tools/filesystem/plugin.py:196-218](core/tools/filesystem/plugin.py#L196-L218)
**类别**: Safety / Bug
**严重级别**: High

SafetyProfile 声明了 `max_file_bytes=10*1024*1024`，但 `WriteFilePlugin.execute()` 从未检查 `params.content` 的大小。代码库中唯一的大小检查在 `_validate_path()`（safety.py:181），仅检查**已有**文件大小。`ReadFilePlugin` **确实**检查 `max_file_bytes`，两者不一致。

**建议修复**: 在写入前检查 `len(params.content)` 是否超过限制。

---

### 22. `web/plugin.py:205` — `max_response_bytes` 未强制 (WebFetch)

**文件**: [core/tools/web/plugin.py:205](core/tools/web/plugin.py#L205)
**类别**: Safety
**严重级别**: High

`httpx.AsyncClient.get()` 不限制响应大小，尽管 `SafetyProfile.max_response_bytes`（5MB）已声明。恶意或配置错误的服务器可能导致内存耗尽。

**建议修复**: 使用 `httpx` 的 `limits` 参数或 content-length 预检查。

---

### 23. `vision/plugin.py:219-245` — 无界磁盘缓存，无淘汰机制

**文件**: [core/tools/vision/plugin.py:219-245](core/tools/vision/plugin.py#L219-L245)
**类别**: Bug
**严重级别**: High

`_save_cache` 向 `data/cache/vision/` 写入 JSON 文件，无大小限制、无 TTL、无文件数上限、无清理机制。缓存目录随时间增长无界。同时两个具有相同 cache key 的并发请求可能产生竞态，产出损坏的 JSON 文件。

**建议修复**: 实现基于 TTL 或 LRU 的缓存淘汰策略，添加写入锁。

---

### 24. `vision/` — DescribeImagePlugin 零测试覆盖

**文件**: 缺少 `core/tools/vision/test_vision.py`
**类别**: Testing
**严重级别**: High

DescribeImagePlugin 是最复杂的插件（246 行，双模式：single+batch，缓存，LLM backend 创建，session message 扫描），但没有任何测试。所有其他插件都有专门测试文件。

**建议修复**: 编写涵盖关键路径的测试：单图描述、批量描述、缓存命中/未命中、无效索引、错误处理。

---

### 25. `todo.md` 第 11 行 — `llm_adapter.py` 重试逻辑中的 `started` flag 未被使用

**文件**: [core/llm_adapter.py](core/llm_adapter.py)
**类别**: Bug
**严重级别**: High

`todo.md` 确认：`stream_think()` 设置 `started = True`（约第 98-100 行）但从未在重试逻辑中检查。注释说"仅在流尚未开始时重试"，但约第 110 行没有 `if started: raise`。当 LLM 流部分成功然后中途失败时，重试逻辑从开头重新产出所有 token，前台产生重复内容。

---

### 26. `todo.md` 第 17 行 — Brain.j2 缺少身份定义

**文件**: [prompt/Brain.j2](prompt/Brain.j2)
**类别**: Bug
**严重级别**: High

系统 prompt 从未声明"你是 Elysia, 用户叫 妖梦"。模型必须从 `<perception>妖梦说：xxx</perception>` 标签推断身份。`prompt_assembler.py` 的 fallback 分支确实包含 `你是 {name}, 用户叫 {user_name}`，但主要的 Brain.j2 路径没有。这使得角色身份脆弱且容易受 prompt injection 攻击。

---

### 27. `todo.md` 第 16 行 — Brain.j2 工具列表过期（12 个工具中仅记录 7 个）

**文件**: [prompt/Brain.j2](prompt/Brain.j2), ~lines 107-141
**类别**: Bug/Config
**严重级别**: High

系统 prompt 告诉模型"你有以下 7 个工具"，实际注册了 12 个。缺失：`write_file`, `set_reminder`, `get_runtime_config`, `get_self_state`, `describe_image`。LLM 不知道可以使用这些工具，使其实际上失效。

---

### 28-35 (多项 HIGH 前端问题)

- **`index.html:9-10`** — CDN 脚本缺少 Subresource Integrity (SRI) hashes（供应链攻击向量）
- **`index.html:17`** — iframe sandbox `allow-scripts + allow-same-origin` 组合实质上取消沙盒保护
- **`audio.js:10-15`** — `atob()` 无 try/catch，损坏的音频导致整个 WebSocket 消息处理 pipeline 崩溃
- **`main.js:122-123`** — 服务器端 markdown 内容（`command` 消息）经 `marked.parse()` 无 sanitization 渲染
- **`state.js:31-33` (audio.js)** — TTS disabled 时 `audioChunks` 未被清理，内存泄漏
- **`audio.js:28-29`** — `bufferAudioChunk()` 对音频块数量无上限，可被 DoS
- **`audio.js:33-48`** — `URL.createObjectURL()` 的 Blob URL 在队列刷空时未被 revoke，内存泄漏
- **`chat.js` ↔ `persistence.js` 循环导入** — ES module 循环依赖使得测试和重构困难

---

## MEDIUM 问题精选

### 36. `emotion_engine.py:212` — `import random` 在方法体内部

**文件**: [core/emotion_engine.py:212](core/emotion_engine.py#L212)
**类别**: Performance / Style
**严重级别**: Medium

`import random` 在频繁调用的 `_push_idle()` 方法内部。虽然 Python 缓存导入（初次调用后性能影响可忽略），但不符合编码习惯且可能触发静态分析工具。

---

### 37. `cognitive_loop.py:279` — `describe_image` 过滤硬编码字符串

**文件**: [core/cognitive_loop.py:279](core/cognitive_loop.py#L279)
**类别**: Bug
**严重级别**: Medium

工具名过滤用裸字符串 `!= "describe_image"`。如果工具名改变，变成静默 no-op。过滤匹配无内容时不记录任何警告。

---

### 38. `sentence_buffer.py:51-53` — 死代码：未使用的 `_split_pat` 正则

**文件**: [core/sentence_buffer.py:51-53](core/sentence_buffer.py#L51-L53)
**类别**: Bug
**严重级别**: Medium

正则 `_split_pat` 在 `__init__` 编译但从未被使用。实际分割由 `_find_split()` 完成。`[.]?` 模式可以匹配空字符串，如果被使用会引发灾难性的分割。这暗示一个被部分完成的 refactoring。

---

### 39. `prompt_assembler.py:253` vs `text_utils.py:6` — 重复函数 ✅ 已修复

**文件**: [core/prompt_assembler.py](core/prompt_assembler.py), [core/text_utils.py](core/text_utils.py)
**类别**: Architecture
**严重级别**: Medium
**修复**: `4004418` — `content_text_from_raw` 删除，prompt_assembler 导入别名 `from .text_utils import content_text_from_content as content_text_from_raw`；text_utils 版本加 `str | None` 入参支持

---

### 40. `llm_adapter.py:89-99` — 鸭式类型检查 `stream_think` 无 Protocol 定义

**文件**: [core/llm_adapter.py:89-99](core/llm_adapter.py#L89-L99)
**类别**: Architecture
**严重级别**: Medium

代码使用 `hasattr(self._backend, "stream_think")` 进行鸭式类型检查。如果后端增加了 `stream_think` 但签名不同，类型检查器（mypy/pyright）不会捕获，只在运行时以神秘崩溃暴露。

---

### 41. `long_term.py:349-354` — Milvus 结果通过文本字符串（非 PK）匹配

**文件**: [core/memory/long_term.py:349-354](core/memory/long_term.py#L349-L354)
**类别**: Data Integrity
**严重级别**: Medium

Milvus 结果通过 `e.text in milvus_texts` 匹配到 `self._entries`。如果两个不同的 `MemoryEntry` 具有相同文本，Milvus 结果可能关联到错误的条目，返回错误的 metadata / primary key。

---

### 42. `long_term.py:52, DIMENSION=1024` — 硬编码维度与 OpenAI embedding 不匹配

**文件**: [core/memory/long_term.py:52](core/memory/long_term.py#L52)
**类别**: Bug / Architecture
**严重级别**: Medium

Milvus collection 创建为 `dimension=1024`（匹配 bge-large-zh-v1.5）。但代码也尝试 OpenAI `text-embedding-3-small`（1536 维）。两者同时使用时 Milvus insert 将失败。

---

### 43. `task_scheduler.py:386-387` — TOOL_CALL 任务不做 checkpoint

**文件**: [core/task_scheduler.py:386-387](core/task_scheduler.py#L386-L387)
**类别**: Data Integrity
**严重级别**: Medium

Checkpoint 过滤 `if t.status == TaskStatus.PENDING and t.type == TaskType.BACKGROUND` 排除了 TOOL_CALL 任务。队列中的工具调用在崩溃时丢失，可能使对话状态不一致。

---

### 44. `debug_info.py:98,107,118` — 无认证的调试端点暴露敏感数据

**文件**: [core/debug_info.py:98,107,118](core/debug_info.py#L98-L118)
**类别**: Security
**严重级别**: Medium

`/debug/api` 暴露 `session_params`（可能含任意用户数据）、`lt_recent`（可能含敏感用户事实）和 `tool_history`（可能含敏感工具参数/结果），且完全无需认证。

---

### 45. `memory/config.yaml:3` — `database_readonly: true` 与 MemorySavePlugin 冲突

**文件**: [core/tools/memory/config.yaml:3](core/tools/memory/config.yaml#L3)
**类别**: Compliance
**严重级别**: Medium

`config.yaml` 设置 `database_readonly: true`，但同目录的 `MemorySavePlugin` 需要 `database_readonly: false`。如果 config 被作为全局设置应用，将阻止写入操作。

---

### 46. `pytest.ini` — 缺少 `asyncio_mode = auto`, `testpaths`, `pythonpath`

**文件**: [pytest.ini](pytest.ini)
**类别**: Testing
**严重级别**: Medium

`pytest.ini` 仅定义了 markers。缺失：
- `asyncio_mode = auto`——运行 `pytest` 无 `--asyncio-mode=auto` 导致 async 测试报错
- `testpaths`——pytest 扫描全部目录
- `pythonpath`——可消除测试文件中的 `sys.path.insert` hacks

---

### 47. `character_config.py:26` — `personality: dict[str, object]` 丧失类型安全

**文件**: [core/character_config.py:26](core/character_config.py#L26)
**类别**: Config
**严重级别**: Medium

`personality` 字段类型为 `dict[str, object]`，YAML schema 明确定义（`traits: list[str]`, `speaking_style: str`）但 Python 类型不强制。YAML 中的 typo（如 `trait` 而非 `traits`）将静默产生空 trait 列表。

---

### 48. `"妖梦"` 默认用户名在 4 个文件中硬编码

**文件**: [core/app_config.py:11](core/app_config.py#L11), [core/prompt_assembler.py:49,105](core/prompt_assembler.py#L49), [core/agent.py:384,555](core/agent.py#L384)
**类别**: Config
**严重级别**: Medium

用户名默认值在 4 个不同文件中硬编码重复。修改用户名需要找到并修改所有位置。

---

### 49. `emotion_engine.py:~197-206` — 6 种情绪→表情映射缺失

**文件**: [core/emotion_engine.py:~197-206](core/emotion_engine.py)
**类别**: Bug
**严重级别**: Medium

`_push_expression()` 仅映射 4 种情绪（happy, excited, sad, curious）。STT 检测到的 5 种额外情绪（warm, concerned, calming, reassuring, understanding）全部回退到 `("neutral", 0.5)`。

---

### 50. 插件 `execute()` 返回 string 错误但被包装为 `ToolResult.ok()` (success=true)

**文件**: 所有插件的 `execute()` 方法
**类别**: Architecture
**严重级别**: Medium

`DescribeImagePlugin.execute()` 返回 `ToolResult`（含 `success`, `error`, `error_type`），但其他所有插件返回裸 string。`SafetyPipeline._wrap_result` 将 string 结果转为 `ToolResult.ok()`，即对 `"写入失败: ..."` 也设置 `error_type=None` 和 `success=True`。错误字符串被报告为成功，阻止正确的 retry 逻辑。

---

## LOW 问题精选

### 51. `agent.py:248-258` — `start()` 中内联定义类

**文件**: [core/agent.py:248-258](core/agent.py#L248-L258)
**类别**: Architecture
**严重级别**: Low

`_EventBusHITLNotifier` 在 `Agent.start()` 内部定义。每次重启重新创建，无法独立测试。两个方法都是空操作（`pass`）。

---

### 52. `agent.py:239-245` — 深层次私有属性访问链

**文件**: [core/agent.py:239-245](core/agent.py#L239-L245)
**类别**: Architecture
**严重级别**: Low

`self._llm._backend._client` 是三级私有属性访问链。如果 LLMAdapter 或 backend 改变内部结构，会静默断裂（fallback 到 None）。

---

### 53. `timing.py:68-73` — 每次 `span()` 调用创建新的局部类

**文件**: [core/timing.py:68-73](core/timing.py#L68-L73)
**类别**: Performance
**严重级别**: Low

`_AccumTimer` 作为局部类在 `span()` 内部定义。每次调用创建新的**类对象**（不是实例）。每次 span 有轻微分配开销。

---

### 54. `block_filter.py:23` — `_BLOCK_LOOKBEHIND` 硬编码魔数 13

**文件**: [core/block_filter.py:23](core/block_filter.py#L23)
**类别**: Compliance
**严重级别**: Low

`_BLOCK_LOOKBEHIND = 13` 硬编码为 `"</|markdown|>"` 的长度。若新增更长 closing tag 的 block 类型，lookbehind buffer 会截断 tag 导致输出乱码。

---

### 55. `agent_fsm.py:19-21` — FSM 无并发保护

**文件**: [core/agent_fsm.py:19-21](core/agent_fsm.py#L19-L21)
**类别**: Concurrency
**严重级别**: Low

`AgentFSM` 无锁机制。当前安全是因为所有调用在 event loop 线程内且受 `_process_lock` 保护。但从后台任务调用 `transition()` 的维护者可能损坏状态。

---

### 56. `vision/plugin.py:58-59` — `self._ctx` 存储但从未读取

**文件**: [core/tools/vision/plugin.py:58-59](core/tools/vision/plugin.py#L58-L59)
**类别**: Architecture
**严重级别**: Low

`setup()` 保存 `ctx` 到 `self._ctx`，但 `execute()` 使用自己的 `ctx` 参数。`self._ctx` 从未被读取。违反 ToolPlugin 文档所述的"模块级无状态"原则。

---

### 57. `web/plugin.py:110` — `from tavily import AsyncTavilyClient` 在函数体内部

**文件**: [core/tools/web/plugin.py:110](core/tools/web/plugin.py#L110)
**类别**: Bug/Architecture
**严重级别**: Low

每次调用 `_tavily_search()` 时重复导入 `tavily`。应移至模块级别或使用 lazy import guard。

---

### 58. vision 插件缺少 `README.md`

**文件**: 缺少 `core/tools/vision/README.md`
**类别**: Compliance
**严重级别**: Low

CLAUDE.md 的"Adding a new tool plugin"要求每个插件目录包含 `README.md`。所有其他 7 个插件都有。Claude.md 规则 1 要求 "readme.md用中文写"。

---

### 59. 所有插件 `emit` 参数未用于 progress reporting

**文件**: 所有插件的 `execute()` 方法
**类别**: Architecture
**严重级别**: Low

每个 plugin 的 `execute()` 签名接受 `emit=None`，但没有一个调用 `emit.progress()` 报告进度。`ToolEmitter` 基础设施存在但基本上是死代码。

---

### 60. `debug_info.py:81-210` — 深度访问私有 `_` 属性

**文件**: [core/debug_info.py:81-210](core/debug_info.py#L81-L210)
**类别**: Architecture / Compliance
**严重级别**: Low

`collect()` 深入访问 Agent 及其组件的私有（`_`-前缀）属性。紧密耦合到每个组件的内部结构。任何内部 refactoring 都会静默破坏调试端点。

---

## CLAUDE.md 合规总结

| 要求 | 状态 | 备注 |
|------|------|------|
| README.md用中文写 | ✅ 合规 | README.md 全部中文 |
| 代码注释用中文 | ✅ 合规 | 所有 docstrings 和注释均为中文 |
| 使用conda环境 | ✅ 合规 | README "conda activate Elysia" |
| 敏感信息通过环境变量 | ✅ 合规 | API keys 使用 `secret=True` + env var |
| Protocol-Registry-Backend 模式 | ✅ 合规 | Module/ 服务遵循此模式 |
| Async-first | ✅ 合规 | 所有 I/O 为 async |
| Logger injection via Factory | ⚠️ 部分合规 | Module/ 使用注入 logger；core/ 使用标准 `logging.getLogger(__name__)` |
| HITL approval flow | ✅ 合规 | ToolManager/SafetyPipeline 已实现 |
| Backend 类以 "Backend" 结尾 | ✅ 合规 | core 层不适用 |
| 工具插件结构 | ⚠️ 部分合规 | vision 插件缺少 README.md |

---

## 建议修复顺序

### 第一优先级（立即修复）

1. **[Critical] idle_monitor.py** — Python 3.13 启动崩溃（服务器模式无法启动）
2. **[Critical] filesystem/plugin.py + safety.py** — 沙盒路径穿越
3. **[Critical] persistence.js + main.js** — `marked.parse()` XSS
4. **[Critical] debug.html** — JS 字符串注入 XSS
5. **[Critical] requirements.txt** — 缺失核心依赖 `pydantic`, `pytest`

### 第二优先级（本周内）

1. **[Critical] consolidator.py** — 缓冲区数据丢失
2. **[Critical] long_term.py** — `_sync_dirty_entries()` 死代码
3. **[Critical] task_scheduler.py** — prune KeyError 崩溃 + dangling futures
4. **[High] cognitive_loop.py** — 打字指示器卡死 + `_current_task` 竞态 + 句子丢弃
5. **[High] llm_adapter.py** — CancelledError 被吞没 + `started` flag 未使用
6. **[High] agent.py** — 消息静默丢失 + 封装破坏

### 第三优先级（本月内）

1. **[High] vision/plugin.py** — 缓存无界 + 零测试覆盖
2. **[High] web/plugin.py** — max_response_bytes 未强制
3. **[High] filesystem/plugin.py** — max_file_bytes 未强制
4. **[High] session.py** — JSONL 覆盖写入数据丢失
5. **[High] long_term.py** — entries 无锁并发访问
6. **[Medium] app_config.py** — VisionConfig 不完整
7. **[Medium] prompt/Brain.j2** — 过期工具列表 + 缺失身份定义

### 第四优先级（技术债务）

1. **[Medium] debug_info.py** — 无认证敏感数据暴露
2. **[Medium] long_term.py** — Milvus 文本匹配（非 PK）+ 硬编码维度
3. **[Medium] task_scheduler.py** — TOOL_CALL 不做 checkpoint
4. **[Medium] emotion_engine.py** — 缺失情绪映射
5. **[Medium] pytest.ini** — 缺失 essential 配置
6. **[Low] 全部** — 代码清理和重构

---

## 总结

本次审查覆盖了项目约 140 个文件（排除 Module/、live2d/、ref/ 及资源文件），发现 **10 个 Critical** 问题、**25 个 High** 问题、**40+ 个 Medium** 问题以及 **55+ 个 Low** 问题。

最突出的安全问题是前端 **XSS 漏洞**（`marked.parse()` 无 sanitization、`esc()` 在 JS 字符串上下文中误用）和后端**沙盒路径穿越**（字符串前缀匹配代替路径检查）。

最突出的可靠性问题是 **`_sync_dirty_entries()` 死代码**（导致向量数据库持续分叉）、**`consolidator.py` 数据丢失**（缓冲区在成功提取前清空）、**`task_scheduler.py` 并发竞态**（prune crash + dangling futures）以及 **`idle_monitor.py` Python 3.13 启动崩溃**。

最突出的可维护性问题是 **`Brain.j2` 工具列表过期**（12 个工具中仅 7 个有文档）、**缺失身份定义**、**`requirements.txt` 不完整**以及 **vision 插件零测试覆盖**。

---
*此报告由 Claude Code 使用 5 路并行 agent code review 生成*
