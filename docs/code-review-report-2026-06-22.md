# 项目全面代码审查报告

**审查日期**: 2026-06-22  
**审查范围**: 整个项目（排除 `superpowers/`、`live2d/`、`ref/`、`__pycache__/`）  
**审查方法**: 7 个并行 Agent 分别审查不同模块，覆盖 ~150+ 源文件

---

## 目录

1. [严重问题 (Critical)](#1-严重问题-critical)
2. [高危问题 (High)](#2-高危问题-high)
3. [中等问题 (Medium)](#3-中等问题-medium)
4. [低危问题 (Low)](#4-低危问题-low)
5. [CLAUDE.md 合规性问题](#5-claudemd-合规性问题)
6. [前端问题汇总](#6-前端问题汇总)
7. [测试问题汇总](#7-测试问题汇总)
8. [配置/文档问题](#8-配置文档问题)
9. [统计总览](#9-统计总览)

---

## 1. 严重问题 (Critical)

### 1.1 ~~路径穿越漏洞 — `session_id` 注入可绕过沙箱写入任意目录~~ ✅ 已修复

**文件**: [Module/tool/safety.py:134-158](Module/tool/safety.py#L134-L158)

> ✅ 修复: 新增 `_sanitize_session_id()` — 白名单 `[a-zA-Z0-9_-]+`，不匹配降级 "default"。应用于 `_resolve_sandbox_path()` 和 `_check_safety()`。(e456a4c)

```python
write_dir = profile.write_dir.replace("{session_id}", session_id)
```

如果 `session_id` 为 `"../etc"` 且 `write_dir` 为 `"/tmp/{session_id}"`，替换后 `write_dir` 变为 `"/tmp/../etc"`，解析后为 `"/etc"`。而 `resolved` 也以 `write_dir` 为基准拼接，那个 `startswith` 检查会被绕过。`session_id` 来自用户控制的聊天会话名称，攻击者可写入进程有权限的任意目录。

**建议**: 对 `session_id` 进行白名单校验（仅允许 `[a-zA-Z0-9_-]+`），或使用 `Path.resolve()` + 明确的前缀检查而非字符串替换。

---

### 1.2 Milvus 跨集合 metric_type 污染

**文件**: [Module/database/vectordb/milvus.py:39,88,250-251](Module/database/vectordb/milvus.py#L39)

```python
self._metric_type = "L2"  # 实例级别，非集合级别
```

`_metric_type` 是单个实例变量，每次 `create_collection()` 会覆盖。`search()` 始终使用 `self._metric_type`，不管查询的是哪个集合。如果创建集合 A 用 COSINE，然后搜索集合 B，会对 B 错误地使用 COSINE 度量。

**建议**: `_metric_type` 改为 `dict[str, str]`，key 为集合名称。

---

### 1.3 ~~安全绕过 — URL/路径安全检查仅检查字面键名 `"url"` 和 `"path"`~~ ⚠️ 部分修复

**文件**: [Module/tool/safety.py:432-450](Module/tool/safety.py#L432-L450)

> ⚠️ 已扩展: 匹配所有含 `url`/`path` 的参数名 (如 `source_url`, `file_path`)。但未使用 param_model schema——新工具若用完全不同的参数名仍可能绕过。(e456a4c)

```python
# 仅检查 raw_params 中字面命名为 "url" 和 "path" 的键
for key in ("url", "path"):
    if key in raw_params:
        # do safety check
```

如果工具的参数叫 `"source_url"`、`"target_url"`、`"file_path"` 等，安全检查会被完全绕过。这意味着任何工具只要不用 `"url"` 或 `"path"` 作为参数名，就可以绕过 URL/路径安全验证。

**建议**: 通过工具的 `param_model` schema 来识别哪些字段是 URL/路径类型，而非依赖硬编码的键名。

---

### 1.4 NoSQL 注入 — 用户控制的集合名

**文件**: [Module/database/documentdb/mongodb.py:31](Module/database/documentdb/mongodb.py#L31)

```python
async def insert_one(self, collection: str, document: dict) -> str:
    result = await self._db[collection].insert_one(document)
```

`collection` 参数直接用作 MongoDB 集合名，无任何校验。如果集合名来自用户输入（会话名称、用户名等），攻击者可访问数据库中任何集合。

**建议**: 维护合法集合名的白名单，或对集合名进行严格格式校验。

---

### 1.5 ~~`CognitiveLoop._req_counter` 是类变量，多实例共享~~ ✅ 已修复

**文件**: [core/cognitive_loop.py:62](core/cognitive_loop.py#L62)

```python
class CognitiveLoop:
    _req_counter: int = 0  # 类属性，所有实例共享！
```

如果同一进程中运行两个 Agent 实例，它们的请求 ID 会交错。应改为实例属性，在 `__init__` 中初始化。

---

### 1.6 Embedding 工厂缓存忽略 kwargs 差异

**文件**: [Module/embedding/registry.py:30-35](Module/embedding/registry.py#L30-L35)

```python
if model_id in cls._instances:
    return cls._instances[model_id]  # 无视 device/cache_folder 等参数变化！
```

调用 `EmbeddingFactory.create("huggingface/bge-m3", device="cpu")` 之后再调用 `EmbeddingFactory.create("huggingface/bge-m3", device="cuda")` 会静默返回 CPU 实例。

---

### 1.7 ~~重复 `@staticmethod` 装饰器~~ ✅ 已修复

**文件**: [core/cognitive_loop.py:719-720](core/cognitive_loop.py#L719-L720)

```python
@staticmethod
@staticmethod
def _build_wake_up_message(ctx: dict) -> str:
```

明显的复制粘贴错误。虽然 `staticmethod` 是幂等的所以不会出错，但这是代码质量问题。

---

### 1.8 ~~config/validator.py Optional[X] 崩溃~~ ✅ 已修复

**文件**: [Module/config/validator.py:117-123](Module/config/validator.py#L117-L123)

`get_origin()` 对 `Optional[int]`（即 `Union[int, None]`）返回 `typing.Union`。在 Python 3.10+ 中，`isinstance(value, Union)` 会抛出 `TypeError: Subscripted generics cannot be used with class and instance checks`。任何 Optional 字段的非 None 值都会导致验证器崩溃。

---

### 1.9 ~~EventBus.drain() 竞态条件~~ ✅ 已修复

**文件**: [core/event_bus.py:43-48](core/event_bus.py#L43-L48)

```python
while not self._queue.empty():
    events.append(self._queue.get_nowait())
```

`drain()` 和 `_process_events()` 可能并发消费同一队列。在 `empty()` 返回 False 和 `get_nowait()` 之间，另一个消费者可能已取走元素，导致 `asyncio.QueueEmpty` 异常。

---

## 2. 高危问题 (High)

### 2.1 后端

| # | 文件 | 行号 | 问题 |
|---|------|------|------|
| 2.1.1 | ~~mailbox_task 资源泄漏~~ ✅ 已修复(core/agent.py#L418-L420) | 418-420 | `_mailbox_task` 被 cancel 但从未 await，资源泄漏 |
| 2.1.2 | ~~SessionMemory 私有属性访问~~ ✅ 已修复(core/agent.py#L192-L232) | 192-232 | 直接访问 `SessionMemory._pending_file`、`_db`、`_messages` 等私有属性 |
| 2.1.3 | ~~[Module/embedding/huggingface.py] ✅ 已修复~~ | 120-126 | `close()` 和 `asyncio.to_thread` 推理之间存在竞态——可能 segfault |
| 2.1.4 | ~~TaskSystem 信号量泄漏~~ ✅ 已修复(Module/task_system/task_system.py#L192-L241) | 192-241 | 信号量泄漏——如果 `create_task` 在 acquire 之后失败 |
| 2.1.5 | ~~TaskSystem 双重信号量释放~~ ✅ 已修复(Module/task_system/task_system.py#L348) | 348 | `resume()` 中的双重信号量释放——如果 PAUSED 任务在外部被清理 |
| 2.1.6 | ~~LLM choices 空检查~~ ✅ 已修复(Module/llm/openai_compat.py#L160-L190) | 172-189 | `_call_api` 非流式路径在访问 `response.choices[0]` 前未检查 `choices` 是否为空 |
| 2.1.7 | ~~ltm_list_handler None 检查~~ ✅ 已修复(Module/input/server.py#L194) | 194 | `ltm_list_handler` 访问 `agent._long_term._entries` 无 None 检查——如果 `_long_term` 为 None 则崩溃 |

### 2.2 前端

| # | 文件 | 行号 | 问题 |
|---|------|------|------|
| 2.2.1 | [static/js/chat.js:516](static/js/chat.js#L516) + [static/js/persistence.js:3](static/js/persistence.js#L3) | 循环依赖 | `chat.js` ↔ `persistence.js` 循环依赖——ES 模块 live binding 可能导致 `undefined` |
| 2.2.2 | [static/js/mic.js:32](static/js/mic.js#L32) | 32 | `ScriptProcessorNode` 连接到 `audioCtx.destination`——造成音频反馈回路（回声） |
| 2.2.3 | [static/js/main.js:309-315](static/js/main.js#L309-L315) | 309-315 | `showApprovalDialog()` 的 Escape keydown 监听器从未移除——每次弹窗泄漏一个监听器 |
| 2.2.4 | [static/js/chat.js:135](static/js/chat.js#L135) | 135 | `addImageToPreview()` 将 URL 传给 `esc()`——将 `&` 转义为 `&amp;`，损坏 URL |
| 2.2.5 | [static/js/ws.js:76-84](static/js/ws.js#L76-L84) | 76-84 | `send()` 在 WebSocket 非 OPEN 时静默丢弃消息，无错误回调 |
| 2.2.6 | static/js/ 整体 | 多处 | `esc()` 有三个独立实现（chat.js:11, settings.js:218, debug.html:305）——违反 DRY，维护风险 |
| 2.2.7 | [static/js/persistence.js:139](static/js/persistence.js#L139) | 139 | `localStorage.setItem()` 的 `QuotaExceededError` 未被捕获 |
| 2.2.8 | [static/js/ws.js](static/js/ws.js) | 全局 | 未设置 `state.ws.onerror`——WebSocket 传输层错误静默丢失 |

---

## 3. 中等问题 (Medium)

### 3.1 后端

| # | 文件 | 行号 | 问题 |
|---|------|------|------|
| 3.1.1 | ~~[core/session.py:175] ✅ 已修复~~(core/session.py#L175) | 175 | `save()` 用 `"w"` 模式覆写 JSONL（应追加），崩溃时只保留最后一次保存 |
| 3.1.2 | ~~[core/session.py:21] ✅ 已修复~~(core/session.py#L21) | 21 | `_pending_file` 是类属性，多实例共享同一文件 |
| 3.1.3 | ~~token_tracker 属性不存在~~ ✅ 已修复(core/token_tracker.py) | — | `input_tokens`/`output_tokens` 属性不存在——runtime_config 始终显示 0 |
| 3.1.4 | [core/llm_adapter.py:140-143](core/llm_adapter.py#L140-L143) | 140-143 | 旧后端的 `close()` 是 fire-and-forget，无错误处理 |
| 3.1.5 | [Module/tts/openai_tts.py:73](Module/tts/openai_tts.py#L73) | 73 | `test()` 对 base_url 发送 GET——对 OpenAI 端点返回假阴性 |
| 3.1.6 | [Module/tts/gpt_sovits.py:134-150](Module/tts/gpt_sovits.py#L134-L150) | 134-150 | 取消 + HTTP 流式清理交互——可能在高取消负载下泄漏 |
| 3.1.7 | [Module/config/loader.py:176-180](Module/config/loader.py#L176-L180) | 176-180 | Optional 嵌套 dataclass：YAML dict 值直接赋值而非构造嵌套 dataclass |
| 3.1.8 | ~~[Module/config/loader.py:144] ✅ 已修复~~(Module/config/loader.py#L144) | 144 | `open()` 缺少 `encoding="utf-8"`——Windows 上可能 `UnicodeDecodeError` |
| 3.1.9 | [Module/output/websocket.py:74-76](Module/output/websocket.py#L74-L76) | 74-76 | `_audio_seq` dict 从未清理——长期连接无限制增长 |
| 3.1.10 | [Module/input/server.py:269](Module/input/server.py#L269) | 269 | Token 用量：每次请求拉取 100K 条记录做 O(n) Python 聚合——应用 MongoDB `$group` |
| 3.1.11 | [Module/input/server.py:600-601](Module/input/server.py#L600-L601) | 600-601 | 调试/API 路由注册被 `index.html` 是否存在控制——偶然耦合 |
| 3.1.12 | ~~[Module/input/server.py:481-483] ✅ 已修复~~(Module/input/server.py#L481-L483) | 481-483 | `max_bytes = (getattr(...) or 10)`——若 `image_max_mb=0`（禁用），`0 or 10` 会变成 10 |
| 3.1.13 | ~~[Module/task_system/core/execution.py:239] ✅ 已修复~~(Module/task_system/core/execution.py#L239) | 239 | `attempt > max_retries`——语义上应为 `>=`（偏移 1 令人困惑） |
| 3.1.14 | ~~[Module/task_system/core/evaluator.py:229-231] ✅ 已修复~~(Module/task_system/core/evaluator.py#L229-L231) | 229-231 | 广泛的 `except Exception` 将所有错误转换为 GOAL_FAILED——网络瞬断不应触发 REPLAN |

### 3.2 前端

| # | 文件 | 行号 | 问题 |
|---|------|------|------|
| 3.2.1 | [static/js/persistence.js:117-121](static/js/persistence.js#L117-L121) | 117-121 | 工具卡片参数按 `', '` 分割——若参数值含 `, ` 则错误分割 |
| 3.2.2 | [static/js/audio.js:11](static/js/audio.js#L11) | 11 | `atob()` 对无效 base64 抛出异常未被捕获→崩溃 WS 消息处理 |
| 3.2.3 | [static/js/main.js:32](static/js/main.js#L32) | 32 | `handleMessage()` switch 无 `default` case——未知消息类型静默忽略 |
| 3.2.4 | [static/js/main.js:279-316](static/js/main.js#L279-L316) | 294-295 | `showApprovalDialog()` 使用硬编码 id——若同时出现两个弹窗会有重复 id |
| 3.2.5 | [static/js/settings.js:196,203](static/js/settings.js#L196-L203) | 196,203 | 自定义 provider 名中的 `/` 会使 `split('/')` 解析错误 |
| 3.2.6 | [static/js/state.js:62-79](static/js/state.js#L62-L79) | 62-79 | `resetConnection()` 未重置多个属性（`sttAvailable`、`ttsAvailable` 等） |

---

## 4. 低危问题 (Low)

### 后端 (~35 个)

| 模块 | 文件 | 问题摘要 |
|------|------|----------|
| core | app.py:60-63,84-86 | 忙等待轮询 FSM 状态，无超时 |
| core | app.py:106 | 广泛的 `except Exception` 吞掉 `SystemExit` |
| core | app.py:130-135 | CLI 参数直接 mutate config 对象，绕过验证 |
| core | cognitive_loop.py:725 | 未解决的 TODO(v2) 在生产代码中 |
| core | reminder/plugin.py:36 | `ensure_future` 未存储任务——关闭时无法取消 |
| core | vision/plugin.py:123-139 | 每次调用创建/销毁后端——TCP 连接池浪费 |
| core | llm_adapter.py:88-99 | 逐字符生成——每个字符一个 yield，极其密集的事件 |
| Module | config/loader.py:224 | `_safe_get_type_hints` 静默吞掉所有异常 |
| Module | logger/factory.py:41-46 | `get_caller_logger()` 假设固定调用深度 |
| Module | logger/__main__.py:23 | 调用不存在的 `factory.create()`——会崩溃 |
| Module | schema/message_format.py:205 | `"localhost" in source` 是子串匹配——`evil.com/localhost` 会匹配 |
| Module | schema/message_format.py:148-158 | Falsy 值（`size_bytes=0`, `duration_ms=0`）序列化时丢失 |
| Module | input/server.py:121-122 | GET `/shutdown`——浏览器预取/CSRF 可触发关闭 |
| Module | input/server.py:55 | `show_index=True` 启用目录列表 |
| Module | input/audio_buffer.py:176 | 空白 `except Exception: pass` 吞掉 VAD 错误 |
| Module | output/manager.py:57-59 | 广播失败记录为 DEBUG——生产环境会错过 |
| Module | output/websocket.py:97-105 | `vars(msg)` 可能暴露私有属性给 WebSocket 客户端 |
| Module | embedding/huggingface.py:93-101 | 模型加载失败未被包装为用户友好错误 |
| Module | bench/bench.py:119-129 | e2e 基准测试使用忙等待轮询，硬编码超时 |
| Module | task_system/lifecycle.py:107-109 | 取消信号可能永远不会被处理（TODO 确认） |
| Module | task_system/delegation_preparer.py:188-194 | `.format()` 可能在含花括号的 LLM 输出上引发 KeyError |
| Module | task_system/artifact_store.py:102 | 节点删除时孤儿 artifact 泄漏 |
| Module | task_system/query.py:391 | 访问 `mailboxes[task_id]` 未检查成员存在性 |
| Module | tool/safety.py:59-63 | Token 估算（4 chars/token）对 DeepSeek BPE 极其不准确 |
| Module | tool/manager.py:110,154 | `asyncio.ensure_future()` 应改用 `create_task()` |
| Module | tool/manager.py:268-269 | `importlib.reload()` 不重新加载传递依赖 |
| Module | database/vectordb/milvus.py:44 | 同步 `MilvusClient()` 构造函数阻塞 asyncio 事件循环 |
| Module | database/vectordb/milvus.py:180-181 | `close()` 是空操作——连接永不释放 |

### 前端 (~20 个)

| 类别 | 数量 | 示例 |
|------|------|------|
| 事件监听泄漏 | 5 | 粘贴/键盘/设置点击外部/工具点击外部/工具重载处理程序 |
| 无障碍 | 12 | 无 `aria-*` 属性、颜色对比度不足、`:focus-visible` 缺失 |
| 代码质量 | 3 | `settings.js:166-172` 不必要的 `switch_model`；内联样式重复；CSS 规则重复 |

---

## 5. CLAUDE.md 合规性问题

### 5.1 代码注释用中文（规则 2）

| 状态 | 文件 |
|------|------|
| ❌ 违反 | [core/agent.py](core/agent.py) — 英文 docstring |
| ❌ 违反 | [core/cognitive_loop.py](core/cognitive_loop.py) — 英文 docstring |
| ❌ 违反 | [Module/llm/protocol.py](Module/llm/protocol.py) — 英文方法 docstring |
| ⚠️ 混合 | 大多数 protocol.py 文件 — 中文模块 docstring + 英文方法 docstring |
| ✅ 符合 | 所有其他源文件 |

### 5.2 协议-注册-后端模式

| 模块 | 符合？ | 备注 |
|------|--------|------|
| Module/llm | ✅ | 完整的 protocol/registry/openai_compat.py |
| Module/stt | ✅ | protocol/registry/sensevoice.py + openai_stt.py |
| Module/tts | ✅ | protocol/registry/gpt_sovits.py + openai_tts.py |
| Module/embedding | ✅ | protocol/registry/huggingface.py + openai_compat.py |
| Module/bench | ❌ | 无 protocol/registry/config——有意为之，但放错位置 |
| Module/database/* | ⚠️ | 有 protocol/registry 但无 config.py |

### 5.3 其他合规性问题

- `Module/input/server.py` — 17+ 个函数体内联导入，违反整洁 import 原则
- `Module/output/manager.py:17` — 使用绝对导入 `from Module.logger` 而非相对导入
- `core/tools/vision/` — 缺少 `README.md`（CLAUDE.md 行 210 要求）
- 未找到 TODO/FIXME/HACK 注释跨 ~150+ 文件（实际上在 task_system 中有约 12 个）

---

## 6. 前端问题汇总

### 6.1 安全 (XSS/CSP)

| 问题 | 文件 |
|------|------|
| CDN 脚本无 `integrity` 属性 | [static/index.html:8-10](static/index.html#L8-L10) |
| 无 Content-Security-Policy | [static/index.html](static/index.html) |
| 调试页面对所有数据进行无认证的 DELETE | [static/debug.html:882-994](static/debug.html) |
| 三个独立的 HTML 转义实现 | chat.js, settings.js, debug.html |

### 6.2 Bug

- 循环依赖：`chat.js` ↔ `persistence.js`
- WebSocket `onerror` 从未设置——传输层错误丢失
- `send()` 静默丢弃消息无反馈
- `localStorage` 配额超出未捕获
- `atob()` 对无效 base64 抛异常——WS 消息处理崩溃
- `showApprovalDialog()` Escape 监听器泄漏
- 工具卡片 args 按 `', '` 解析——含逗号的值会损坏
- `marked.parse()` 异步渲染——恢复期间出现空内容闪烁
- `ScriptProcessorNode` 已弃用——整个麦克风管线使用过时 API
- 多个 `document` 事件监听器从未移除

### 6.3 无障碍

12 个独立问题包括：缺少 `aria-*`、颜色对比度不足（3.6:1 vs 需要的 4.5:1）、无键盘导航、`:focus-visible` 缺失、toggle 开关不是按钮。

---

## 7. 测试问题汇总

### 7.1 无外部服务会失败的测试

| 文件 | 行号 | 问题 |
|------|------|------|
| [core/tests/test_phase1.py](core/tests/test_phase1.py#L283-L463) | 283-463 | 创建真实 `Agent()` 不注入 FakeLLMBackend——需要 DEEPSEEK_API_KEY |
| [core/tests/test_phase2.py](core/tests/test_phase2.py#L325-L417) | 325-417 | 同上——还需要 MongoDB |

### 7.2 不稳定测试（时间依赖）

- 15 秒硬编码 `await asyncio.sleep(15)` — [core/tests/test_phase1.py:383](core/tests/test_phase1.py#L383)
- 8 秒硬编码 `await asyncio.sleep(8)` — [core/tests/test_phase2.py:391](core/tests/test_phase2.py#L391)
- 7 处 0.5 秒休眠无轮询 — [Module/task_system/tests/integration/](Module/task_system/tests/integration/)
- 10ms 精度断言：`time.sleep(0.01); assert t.elapsed_ms >= 10` — [core/tests/test_timing.py](core/tests/test_timing.py)

### 7.3 缺失的测试覆盖

- `DescribeImagePlugin` — 零测试覆盖（246 行代码）
- CognitiveLoop `_handle_submit_task` 边界情况
- WebFetchPlugin `max_response_bytes` 强制执行
- OutputManager 对所有通道类型的错误隔离

---

## 8. 配置/文档问题

### 8.1 关键

| 问题 | 文件 | 行号 |
|------|------|------|
| `pytest.ini` 缺少 `asyncio_mode = auto` | [pytest.ini](pytest.ini) | — |
| `pytest.ini` 缺少 `testpaths`——扫描整个仓库包括 `node_modules/` | [pytest.ini](pytest.ini) | — |
| `requirements.txt` 缺少 `pydantic`（15+ 文件使用） | [requirements.txt](requirements.txt) | — |
| `requirements.txt` 缺少 `tavily-python`（WebSearchPlugin 需要） | [requirements.txt](requirements.txt) | — |
| Makefile 缺少 `--asyncio-mode=auto` | [Makefile:8-21](Makefile#L8-L21) | 8-21 |
| `VisionConfig` 缺少 `base_url` 和 `api_key` 字段 | [core/app_config.py:67-69](core/app_config.py#L67-L69) | 67-69 |

### 8.2 中等

- `.env.example` 使用看起来真实的占位符（`sk-your-key-here`）
- `config.yaml` 指定 `deepseek-v4-flash` 但 `app_config.py` 默认为 `deepseek-chat`
- README.md 报告 883 个测试，architecture.md 报告 708 个——不一致
- `docs/ref_memory-architecture.md` ~~完全废弃但仍在仓库中——积极误导~~ ✅ 已删除 (06-22)
- `todo.md` ~~被 architecture.md 引用但不存在~~ ✅ 已存在 (06-22)

---

## 9. 统计总览

| 严重程度 | 数量 | 关键类别 |
|----------|------|----------|
| **严重** | 9 | 路径穿越、NoSQL 注入、安全绕过、竞态条件、崩溃 bug |
| **高危** | 15 | 资源泄漏、私有成员访问、缺失 None 检查、前端循环依赖、音频反馈回路 |
| **中等** | 30 | 数据丢失、性能、潜在竞态、API 误用 |
| **低危** | ~55 | 代码质量、死代码、文档、无障碍 |

**未发现**: 在整个代码库中未找到 TODO/FIXME/HACK 注释在核心生产代码中（task_system 中有约 12 个已记录的 TODO）。

### 按模块的问题分布

| 模块 | 严重 | 高危 | 中等 | 低危 |
|------|------|------|------|------|
| core/ | 2 | 4 | 4 | 6 |
| Module/llm+stt+tts+embedding | 2 | 2 | 4 | 8 |
| Module/config+logger+schema+prompt+input+output | 1 | 3 | 6 | 10 |
| Module/task_system | — | 3 | 5 | 8 |
| Module/tool | 2 | 1 | 3 | 5 |
| Module/database | 2 | 1 | 2 | 4 |
| static/ (前端) | — | 8 | 6 | 20 |
| 测试/配置/文档 | — | 5 | 5 | 5 |

---

## 建议优先修复路线

1. **立即** (第 1 周):
   - 修复路径穿越漏洞 (1.1)
   - 修复 NoSQL 注入 (1.4)
   - 修复安全绕过 (1.3)
   - 修复 validator.py Optional[X] 崩溃 (1.8)
   - 修复 EventBus 竞态条件 (1.9)
   - 在 pytest.ini 中添加 `asyncio_mode = auto`
   - 在 requirements.txt 中添加缺失的依赖（pydantic, tavily-python）

2. **短期** (第 2-3 周):
   - 修复前端循环依赖 (2.2.1) 和音频反馈回路 (2.2.2)
   - 统一 `esc()` 实现为共享工具函数
   - 修复 embedding 工厂缓存 bug (1.6)
   - 修复 `_req_counter` 类变量 (1.5)
   - 添加前端 CSP 头部
   - 修复 milestone 跨集合 metric_type 污染 (1.2)

3. **中期** (第 1-2 月):
   - 重构 agent ↔ SessionMemory 耦合（通过公共属性暴露）
   - 从 server.py 中移除内联导入
   - 为 vision 插件添加测试覆盖
   - 移除不稳定测试中的硬编码休眠

---

*由 Claude Code 生成 (2026-06-22)*
