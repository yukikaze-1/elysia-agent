# 待做

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争 |
| 中 | **TTS 播放高亮不同步** | 语音还在前一句，下一句气泡已经渲染并高亮；有时前几句无高亮只有后面有 |
| 低 | **消息气泡尾部偶现空行** | 文本/emoji 后面出现空白行，可能是 `send("")` 导致的 |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |

## 功能改进

- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **EmotionEngine 升级**: 生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充
- **STT 情绪→mood 映射升级**: 生理状态调制 + 性格调制 + 时间衰减
- 唤醒词检测（openWakeWord）
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **多角色 prompt 模板**: `PromptAssembler._prompt` 当前指向 `prompt/`，多角色时改为 `character/<name>/`
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader

### 前端体验优化

| # | 任务 | 说明 |
|---|------|------|
| 3 | 消息悬停复制 | hover 气泡出现复制按钮 |
| 4 | 发送状态反馈 | send 后 input 区显示勾动画 |

### 后台任务系统 — 后续迭代

后台任务分级 + DAG 依赖解析（预留 `run_in_background` / `depends_on` 字段）。

## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 高 | `Module/schema/` 479行预留设计 | 多层架构预留，保留参考 |
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行，保留参考 |

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
