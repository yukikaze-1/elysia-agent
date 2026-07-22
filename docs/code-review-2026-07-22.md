# Elysia/Agent 全项目代码审查报告

**审查日期**: 2026-07-22  
**审查范围**: 整个项目（排除 `/data`、`docs/superpowers`、`docs/plan`、`docs/spec`）  
**审查方法**: 9 个并行审查 agent 覆盖全代码库（~1.4M 行），按严重度排序  

**已修复**: #2 (consolidator.py format→replace) + #14 (ws_handler except:pass→debug) — 2026-07-22

---

## 一、严重 Bug（会导致崩溃或数据丢失）

### 1. `llm_adapter.py:91` — stream_think 重试路径 `NameError` 崩溃

[core/adapter/llm_adapter.py](core/adapter/llm_adapter.py#L91)

变量 `started` 仅在 `if hasattr(self._backend, "stream_think")` 分支内定义，但 `except` 重试处理逻辑在 line 107 处无条件引用 `started`。当后端没有 `stream_think` 方法、走 `else` 分支调用 `self._backend.chat()` 时，若抛出可重试异常，`except` 块中引用未定义的 `started` 会引发 `NameError`，掩盖原始异常。

**失败场景**: 使用不支持 `stream_think` 的后端（如某些 Ollama 版本），LLM 调用返回 HTTP 429/5xx → 重试逻辑因 `NameError` 崩溃 → 用户看到通用错误，原始问题被隐藏。

---

### 2. ✅ `consolidator.py:200` — 用户消息含 `{...}` 导致 `KeyError`，记忆提取静默失败

[core/memory/consolidator.py](core/memory/consolidator.py#L200)

`_get_extraction_prompt().format(conversation=conversation_text[:2000], ...)` 使用 Python `str.format()`，当对话文本中包含 `{...}` 字符时（如用户说 "delete {FOO} to bar"），`str.format()` 将 `{FOO}` 解释为占位符并抛出 `KeyError`。外层 `except Exception` 捕获后返回 `[]`，**静默丢弃 BUFFER_THRESHOLD（6 轮对话）的所有缓冲消息**。相比之下，同一文件的 `_get_compare_prompt()` 区域（line 253-259）正确地使用了安全的 `.replace()` 方法。

**失败场景**: 用户在 6 条消息的窗口中说了一句包含 `{任意文本}` 的话 → 整批消息被静默丢弃 → 记忆巩固完全丢失，无任何日志。

---

### 3. `cognitive/loop.py:807-808` — `_response_recorder` 未初始化导致 `AttributeError`

[core/cognitive/loop.py](core/cognitive/loop.py#L807)

`_response_recorder` 仅在 `set_response_recorder()` 被调用时设置，但在 `__init__` 中从未初始化为 `None`。如果 `CognitiveLoop` 在不经过 `AgentLifecycle.start_cognitive()`（该路径在 lifecycle.py:214 处设置了 recorder）的情况下被构造，访问 `self._response_recorder` 会抛出 `AttributeError`。

**失败场景**: 测试代码或替代 Agent 实例在不调用 `set_response_recorder()` 的情况下构造 `CognitiveLoop` → 第一条助手回复触发 `AttributeError` → 响应循环崩溃。

---

### 4. `long_term.py:150-167` — 嵌入加载超时后线程泄漏 + 数据竞争

[core/memory/long_term.py](core/memory/long_term.py#L150)

`_ensure_embedding` 使用 `asyncio.wait_for(loop.run_in_executor(...), timeout=self._embed_timeout)`。超时后 asyncio Task 被取消，但 **线程池中的 OS 线程继续运行**。线程最终完成时会设置 `self._embedding`，而此时异步代码可能在并发读写 `self._embedding`。结果：Agent 以文本模式启动，数分钟后嵌入模型在后台突然激活，改变了 `insert()`/`search()` 的行为。

**失败场景**: 网络慢或模型下载超过 10 秒 → Agent 以纯文本模式启动 → 数分钟后线程完成 → `insert()` 突然开始向量嵌入 → 行为静默变化，可能与并发操作冲突。

---

### 5. `agent.py:348-390` — 认知任务引用被覆盖导致竞态条件

[core/agent/agent.py](core/agent/agent.py#L348)

两个代码路径覆盖 `self._cognitive._current_task`：`_handle_user_input`（line 356）和 `_handle_task_result`（line 390）。当用户消息到达时 FSM 处于 `IDLE_BACKGROUND_TASK`，用户消息创建新的认知任务。如果后台任务结果在同一时刻到达，`_handle_task_result` 创建另一个认知任务，**覆盖** `_current_task`。中断触发时，`cancel_current()` 只取消其中一个——另一个继续不受控运行。

**失败场景**: 用户发送消息时后台任务处于 pending → 后台结果在用户消息处理中途到达 → 用户按"停止" → 只有一个任务被取消 → 孤儿任务继续运行，可能产生输出并破坏 FSM 状态。

---

### 6. `long_term.py:250` — 驱逐功能被注释，内存无界增长

[core/memory/long_term.py](core/memory/long_term.py#L250)

```python
# self._evict_old_entries()  # TODO: 开发阶段暂不禁用驱逐，避免丢失测试记忆
```

`_evict_old_entries()` 调用被注释掉。驱逐仅在初始 `_load_from_milvus()` (line 120) 中运行。一旦加载完成，新条目永远积累。TODO 标注为"开发阶段"，但代码中没有任何运行时开关（环境变量或配置标志）来在生产环境中启用驱逐。

**失败场景**: 长时间运行的 Agent 会话频繁进行记忆巩固 → 数千个 `MemoryEntry` 对象在内存和 Milvus 集合中积累 → 内存耗尽 + 搜索性能退化。

---

### 7. `tools/reminder/plugin.py:33-37` — 提醒通知永不送达

[core/tools/reminder/plugin.py](core/tools/reminder/plugin.py#L33)

`asyncio.ensure_future(_remind())` 创建后台任务，其返回值（提醒消息字符串）**从未被消费、转发到任何输出通道或呈现给用户**。工具报告成功（"已设置提醒..."）但提醒静默消失。

**失败场景**: 用户设置提醒 → `_remind()` 协程等待并返回 `"⏰ 提醒: ..."` → 无人读取返回值 → 用户永远收不到提醒通知。

---

### 8. `tts_adapter.py:304-307` — 句子排序门锁死锁

[core/adapter/tts_adapter.py](core/adapter/tts_adapter.py#L304)

`_release_events[seq+1]` 门锁在 finally 块中设置，但事件创建是惰性的（在 `stream_sentence` 中，line 141）。如果 TTS 任务在下一个句子的 `stream_sentence` 被调用之前完成，那么下一个句子的门锁事件尚未创建，`set()` 被跳过，导致死锁。

**失败场景**: 异步操作（如 LLM 响应回调）在两个 `stream_sentence` 调用之间交错 → 句子 N 的 TTS 任务在 N+1 的事件创建之前完成 → 句子 N+1 永远等待 → 所有后续句子阻塞。

---

## 二、安全漏洞

### 9. 管理端点无认证 — `/shutdown`、`/restart`、`/debug/api/*`

[Module/input/server.py](Module/input/server.py#L96)、[Module/input/debug_routes.py](Module/input/debug_routes.py)

关键管理端点没有任何认证或授权检查。服务器绑定到 `0.0.0.0:8765`（[config.yaml](config.yaml#L22)），同一网络上的任何人都可以：
- `POST /shutdown` — 停止服务
- `POST /restart` — 重启服务
- `DELETE /debug/api/sessions` — 清空所有会话
- `DELETE /debug/api/ltm` — 删除所有长期记忆
- `/debug/api/prompts` — 查看完整 LLM 提示历史

**失败场景**: 局域网内任何人或恶意网页可发送 `GET /shutdown` 停止服务，或 `DELETE /debug/api/ltm` 擦除所有长期记忆。

---

### 10. SSE 端点 CORS 全开通配符

[Module/task_system/debug_api.py](Module/task_system/debug_api.py#L266)

```python
"Access-Control-Allow-Origin": "*",
```

SSE 事件流 `/debug/api/tasks/stream` 允许任意来源订阅实时任务状态更新。

**失败场景**: 用户访问恶意网站，该网站 JavaScript 获取 SSE 流 → 泄露实时任务状态、描述和错误详情。

---

### 11. `/static/` 目录列表已启用

[Module/input/server.py](Module/input/server.py#L56)

```python
app.router.add_static("/static/", _STATIC_DIR, show_index=True)
```

`show_index=True` 启用了目录列表，暴露所有静态文件给浏览者。

**失败场景**: 攻击者浏览 `http://host:8765/static/js/` → 枚举所有 JS 源文件 → 发现内部 API 路径和端点名称辅助定向攻击。

---

### 12. 用户消息被明文记录到应用日志

[Module/input/ws_handler.py](Module/input/ws_handler.py#L158)

```python
logger.info("WS 收到文本: %s", text_content.strip()[:100])
```

用户提交的文本（前 100 字符）以 INFO 级别记录。STT 转录文本以 DEBUG 级别记录（line 232）。日志文件（`/log/` 目录）累积用户对话明文片段。

**失败场景**: 任何有文件系统访问权限的人可读取日志文件 → 无需访问应用或数据库即可查看对话片段。

---

### 13. API Key 可能通过自定义 Provider 接口泄露

[Module/input/custom_providers.py](Module/input/custom_providers.py#L37)、[Module/input/api_routes.py](Module/input/api_routes.py#L79)

`/api/providers` POST 端点接受包含 `api_key` 值的 `entry` dict，并将其明文保存到 `custom_providers.yaml`。GET 端点返回完整内容（包括 API key 值，如果已存储）。

**失败场景**: 用户通过设置 UI 添加自定义 LLM 提供者 → API key 被明文存储到 YAML 文件 → 任何查询 `/api/providers` 的客户端可获取 API key。

---

## 三、严重错误处理问题（静默数据丢失）

### 14. ✅ `ws_handler.py` — 多处 `except Exception: pass` 静默吞错误

[Module/input/ws_handler.py](Module/input/ws_handler.py)

在 5 个不同位置发现静默异常吞没：
- **Line 42-52**: WebSocket 能力广播失败 → 前端永远不会知道 STT/TTS 可用 → 整个会话进入纯文本模式
- **Line 184-185**: 模型切换确认失败 → 前端显示过时模型名称或无限 "切换中..."
- **Line 226-227**: STT 转录回显失败 → 用户看不到他们说的话被识别
- **Line 244-245**: 唤醒词通知失败 → 用户零视觉反馈，可能重复唤醒

---

### 15. `module/openai_compat.py:178-189` — 流式路径静默丢弃 Token 使用统计

[Module/llm/openai_compat.py](Module/llm/openai_compat.py#L178)

`_call_api` 流式分支从未设置 `stream_options={'include_usage': True}`，也从未检查 `chunk.usage`。结果：`chat_stream()` 调用者从 `last_usage` 获得 `None`。相比之下，非流式和 `_stream_structured` 路径都正确捕获了 usage。

**失败场景**: 任何依赖 `backend.last_usage` 的流式调用路径 → 静默接收过时/None 数据 → Token 预算跟踪和成本估算错误。

---

### 16. `module/registry_helpers.py:36` — Beta URL 使用了错误的配置键

[Module/_common/registry_helpers.py](Module/_common/registry_helpers.py#L36)

`resolve_base_url` 在解析 `beta_url` 时的最终回退硬编码为 `provider_cfg.get("base_url", "")` 而非 `provider_cfg.get("beta_url", "")`。这意味着 `providers.yaml` 中的 `beta_url` 键从未被查阅。

**失败场景**: 用户在 `providers.yaml` 中设置 `beta_url` → URL 被忽略 → 仅 `DEEPSEEK_BETA_URL` 环境变量生效 → Beta 客户端静默不创建 → 前缀续写失败或回退到常规端点。

---

### 17. `embedding/registry.py:23-59` — Factory 在 close() 后返回已关闭实例

[Module/embedding/registry.py](Module/embedding/registry.py#L23)

`EmbeddingFactory._instances` 是类级缓存字典。调用 `close()` 后实例对象仍然留在缓存中。后续 `create()` 返回已关闭的实例，对该实例调用会得到 `RuntimeError("Embedding model is closed")`。

**失败场景**: 代码创建嵌入后端 → 使用 → close() → 后续再次创建"相同"后端 → 获得已关闭实例 → 崩溃。唯一恢复方法是 `EmbeddingFactory.shutdown()` 清空全局缓存。

---

### 18. `system_prompt/sections/memory.py:20-28` — 长期记忆搜索静默吞连接失败

[Module/system_prompt/sections/memory.py](Module/system_prompt/sections/memory.py#L20)

```python
try:
    results = await self._long_term.search(...)
except Exception:
    return ""
```

静默吞掉了所有异常——包括连接错误、超时错误和编程错误。调用者看到空字符串，无法区分"没有搜索结果"和"向量数据库宕机"。

**失败场景**: Milvus 在会话中途不可达 → Memory section 返回空字符串 → LLM 在无记忆上下文下继续运行 → 无日志、无告警 → 可能持续整个会话。

---

## 四、前端 Bug

### 19. `chat.js:631` — `renderImages is not defined` ReferenceError

[static/js/chat.js](static/js/chat.js#L631)

`renderArchiveMessage()` 调用 `renderImages(m.content)`，但 `renderImages` 仅在 `debug.html` 中定义为非模块脚本的全局函数。`chat.js` 作为 ES module 加载，`renderImages` 不在作用域内。

**失败场景**: 用户向上滚动加载历史消息，任何存档消息包含图片内容 → `ReferenceError` 被 try/catch 捕获 → 历史加载静默失败 → 显示"点击重试"错误条，图片永不渲染。

---

### 20. `main.js:176-179` — 发送冷却与 WebSocket 断开竞态

[static/js/main.js](static/js/main.js#L176)

```javascript
btn.disabled = true;
setTimeout(() => { btn.classList.remove('sent'); btn.disabled = false; }, SEND_COOLDOWN_MS);
```

800ms 冷却期间 WebSocket 断开 → `ws.js:onclose` 正确禁用按钮 → 冷却定时器触发并重新启用按钮 → 用户点击发送 → 消息静默丢弃在 `ws.js:82` 处。

**失败场景**: 用户在 800ms 内发送消息 → WebSocket 断开 → 按钮自动重新启用 → 用户再次点击发送 → 消息静默丢弃，无错误反馈。

---

### 21. `mic.js:47` — `audioCtx.close()` 异步但未等待

[static/js/mic.js](static/js/mic.js#L47)

`state.audioCtx.close()` 返回 Promise，但代码立即设置 `state.audioCtx = null` 而不等待。快速切换麦克风（开 → 关 → 开）时，新 AudioContext 在旧 Context 仍异步关闭时创建。

**失败场景**: 快速开关麦克风 → 旧 AudioContext 处于 closing 状态时创建新 Context → 浏览器控制台警告或麦克风捕获静默失败。

---

## 五、资源泄漏

### 22. `task_scheduler.py:387-390` — Checkpoint 临时文件跨文件系统泄漏

[core/task_scheduler.py](core/task_scheduler.py#L387)

`os.replace(tmp, self._checkpoint_path)` 仅在源和目标同文件系统时是原子的。跨文件系统（如 Docker volume 挂载）时，`os.replace()` 抛出 `OSError`，外层 `except Exception` 捕获但 **不清理 tmp 文件**。

**失败场景**: 容器化部署中 `/data/scheduler_checkpoint.json` 是 volume 挂载，与默认 temp 目录不在同一文件系统 → Checkpoint 保存失败 → 孤儿 `.tmp` 文件随时间累积。

---

### 23. `session.py:87-90` — Fire-and-forget MongoDB 写入无限并发

[core/memory/session.py](core/memory/session.py#L87)

```python
task = asyncio.ensure_future(self._msg_repo.append(self._session_id, msg))
task.add_done_callback(_archive_done)
```

每次 `add_message()` 创建一个新的 asyncio Task 用于 MongoDB 写入。没有并发限制、没有信号量、没有背压。`_archive_done` 回调静默吞所有异常。

**失败场景**: 一个工具轮次产生 10 个工具结果消息 → 10 个并发 MongoDB 写入 → MongoDB 负载下 Task 堆积 → 潜在连接池耗尽或内存压力。

---

### 24. `llm/openai_compat.py:222-228` — LLM 重连时 HTTP 客户端关闭失败静默吞

[Module/llm/openai_compat.py](Module/llm/openai_compat.py#L222)

`reconnect()` 中 `await self._client.close()` 和 `await self._beta_client.close()` 被 `except Exception: pass` 包裹，无日志。底层 `httpx.AsyncClient` 关闭失败（挂起的连接无法排空）完全不可见。

**失败场景**: LLM 服务重启触发重连 → 旧 httpx 连接无法干净关闭 → 异常被吞 → socket 描述符泄露 → 反复重连下导致 "too many open files"。

---

## 六、设计问题

### 25. `cognitive/tools.py:157-158` — 跨越模块边界访问私有 `_infra` 属性

[core/cognitive/tools.py](core/cognitive/tools.py#L157)

```python
global_constraints=(self._task_system._infra.constraints if self._task_system else None),
```

直接访问 `TaskSystem._infra.constraints`——另一个模块类的私有属性。没有公共 API 或 property。如果 `TaskSystem` 重构将 `_infra` 重命名，此行在运行时抛出 `AttributeError`。

---

### 26. `idle_monitor.py:46-48` — 取消的 Task 永远不被等待

[core/idle_monitor.py](core/idle_monitor.py#L46)

```python
async def stop(self) -> None:
    self._running = False
    if self._task:
        self._task.cancel()
        self._task = None
```

`cancel()` 将 `CancelledError` 注入 `_loop()` 协程但 `stop()` 立即返回而不等待。进程退出时事件循环发出 "Task was destroyed but it is pending!" 警告。如果空闲监控持有资源，这些资源不会被清理。

---

### 27. `agent.py:258-284` — 死代码循环条件 + 过宽异常处理

[core/agent/agent.py](core/agent/agent.py#L258)

`while (event := mailbox.get_nowait()) is not None:` —— `asyncio.Queue.get_nowait()` 在队列为空时抛出 `asyncio.QueueEmpty`，永远不返回 `None`。`is not None` 守卫是死代码。更重要的是，`except Exception: pass` 意在捕获 `QueueEmpty`，但实际上也静默吞掉：`AttributeError`（事件缺少 type）、`TypeError` 等。

---

### 28. `character_config.py:48` — `PermissionError` 未被处理

[core/character_config.py](core/character_config.py#L48)

```python
if not path.exists():
    raise FileNotFoundError(...)
raw = yaml.safe_load(path.read_text("utf-8")) or {}
```

`path.exists()` 在文件无读权限时返回 `True`。`path.read_text("utf-8")` 抛出未被捕获的 `PermissionError`。只有 `FileNotFoundError` 场景有错误处理。

---

### 29. `config/_utils.py:12-18` — `safe_get_type_hints` 静默吞所有异常

[Module/config/_utils.py](Module/config/_utils.py#L12)

```python
def safe_get_type_hints(cls: type) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls)
    except Exception:
        return {}
```

当目标类型包含无法解析的前向引用时，**所有**类型信息静默丢失。验证器跳过整个数据类的类型检查，可能静默接受错误类型的值。

---

### 30. `tools/safety.py:155-171` — Session ID 净化不一致

[Module/tool/safety.py](Module/tool/safety.py#L155)

`_resolve_sandbox_path` 在构建候选路径时使用净化后的 `safe_sid`（line 155），但在构建包含检查的预期写入目录时使用了**原始未净化**的 `session_id`（line 166-168）。包含边界路径基于与候选路径不同的值计算。

---

## 总结

| 类别 | 数量 | 最高严重度 |
|------|------|------------|
| 严重 Bug（崩溃/数据丢失） | 8 | CRITICAL |
| 安全漏洞 | 5 | HIGH |
| 静默数据丢失 | 5 | HIGH |
| 前端 Bug | 3 | MEDIUM |
| 资源泄漏 | 3 | MEDIUM |
| 设计问题 | 6 | LOW-MEDIUM |

**总发现数**: 30 条

**建议优先修复**: 问题 #1-#8（严重 Bug）、#9-#10（安全认证/CORS）、#14-#16（静默数据丢失）。

---

*本报告由 9 个并行审查 agent 生成，覆盖 ~1.4M 行代码。所有发现均已读取对应源文件并验证。*
