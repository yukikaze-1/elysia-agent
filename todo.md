# 待做

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争 |
| 中 | **`PromptAssembler._prompt` 死代码** | `prompt_assembler.py:41` 构造的 `PromptManager` 从未使用 |
| 低 | **`SentenceBuffer._skip_dot_and_retry()` 始终返回 None** | `sentence_buffer.py:146` — 代码路径不可达 |
| 低 | **`_transition_idle()` 无条件发 SPEAKING** | `cognitive_loop.py:431` — 仅 tool_calls 时也会发状态变更 |
| 低 | **`ToolContext.user_id` 硬编码为 "妖梦"** | `agent.py:127` — 应使用 `config.agent.user_name` |
| 低 | **filesystem `.env` 拒绝模式过于宽泛** | `filesystem.py:19` — 子串匹配拒绝 `environment.yaml` 等 |

## 功能改进

- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **EmotionEngine 升级**: 生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充
- **STT 情绪→mood 映射升级**: 生理状态调制 + 性格调制 + 时间衰减
- 唤醒词检测（openWakeWord）
- 性能 profiling + 优化

### 前端体验优化

| # | 任务 | 说明 |
|---|------|------|
| 1 | 当前播放气泡高亮 | TTS 播到哪句，对应气泡边缘微光 |
| 2 | 新消息提示箭头 | 上翻历史时底部浮出 "↓ 新消息" |
| 3 | 消息悬停复制 | hover 气泡出现复制按钮 |
| 4 | 发送状态反馈 | send 后 input 区显示勾动画 |

### 后台任务系统

用户说一句 → AI 回一句的串行模型缺少的三种能力：

| 能力 | 复杂度 | 状态 |
|------|--------|------|
| 任务队列（多请求排队） | 低 | — |
| 并行执行（asyncio.gather） | 中 | done (2026-05-29) |
| 后台任务（脱离对话流的长时执行） | 高 | — |

**阶段 2 — TaskQueue + TaskWorker**: 新增 `core/task_queue.py`，长任务 spawn 为独立 asyncio.Task。EventBus 新增 `TASK_PROGRESS` / `TASK_COMPLETE`，FSM 叠加态。1 个 worker 足够。

**阶段 3 — 并行 Worker Pool + DAG**（大改，先看阶段 2 够不够用）

## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 高 | `Module/schema/` 479行预留设计 | 多层架构设计，当前简化架构未使用 |
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88` |
| 中 | `StreamingSentenceBuffer` 未使用 | `sentence_buffer.py:155` |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` API key 需要轮换 | `DEEPSEEK_API_KEY` / `MILVUS_TOKEN` |
| 中 | `EmotionEngine._schedule` 无异常边界 | fire-and-forget task 异常静默丢失 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | Consolidator 每轮调 LLM | `_after_response` 无节流/批处理 |

## 测试补充 (~15 个)

- `core/log_setup.py` (3), `core/memory/long_term.py` (3), `core/cognitive_loop.py` (5)
- `Module/input/audio_buffer.py` (3), `Module/input/server.py` (4), `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时
