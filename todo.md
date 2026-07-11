# 待做

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 中 | **无去重机制** | 延后 — 现有测试确认重复文本+不同元数据是设计意图。需独立设计去重方案 |
| 中 | **`emotion_engine.py` `_push_expression()` 缺失 6 种 mood 映射** | `emotion_engine.py:197-206`: `_push_expression` 仅映射 happy/excited/sad/curious 4 种 mood，STT 路径检测到的 warm/concerned/calming/reassuring/understanding 全部 fallback 到 `("neutral", 0.5)`，声学情绪检测结果在 expression 层面无法体现。 |
| 中 | **`reminder/plugin.py` coroutine 被提前 await 后传入 payload** | `plugin.py:37`: `_remind()` 被调用生成 coroutine 对象后传入 `dispatch_background`。调度器 checkpoint 恢复时 payload 中的 coroutine 不可 JSON 序列化（不持久化），且 `from_dict` 重建后 `hasattr(coro, "__await__")` 为 False。边缘场景，当前提醒工具很少跨重启执行。 |
| 高 | **TTS 密集短句导致 wait_all 永久阻塞** | TS 连续 5+ 短句在 400ms 内密集入队时，MiniMax 后端部分请求无响应，`_transition_idle()` → `wait_all()` 永久等待，FSM 卡在 SPEAKING 状态。需在 TTS stream 层加超时机制，或在 `wait_all` 加整体超时后 cancel 剩余任务。 |

## 功能改进

- **README 截图**: 添加聊天界面 / debug 面板 / 启动流程截图，让新用户第一眼就知道项目长什么样
- **Live2D 集成**: Cubism SDK v5 需 TypeScript + npm + bundler 构建，当前仅 SDK 文件到位、默认关闭。需写完整的加载页面 (`static/live2d-demo/index.html`) + 模型渲染管线

- **poignancy 时间衰减**: rerank 含时效衰减但 poignancy 本身不变，长期不访问的记忆应逐渐降级
- **MiniMax SSE 真流式**: 已尝试并回退 (2026-07-10)。MiniMax SSE 每个 event 是独立可播 mp3（非增量片段），拼接导致重复，只取最后 event 又等于同步下载。待 MiniMax API 支持增量 SSE 或换方案（WebSocket）
- **MiniMax 异步长文本合成**: `t2a_async_v2`，适合 >3000 chars。需创建任务 → 轮询状态 → 下载文件
- **记忆去重**: insert 时检查相似内容，避免重复条目（延后）
- **按 ID 更新/删除记忆**: search() 返回记忆 ID，LLM 可引用修改；新增 update/delete API
- **记忆导出/备份**: 支持 JSON 导出和恢复
- **记忆统计**: 按类型/年龄/poignancy 分布统计，接入 DebugInfo 仪表板
- **冲突检测**: 检测矛盾记忆 (如 "最喜欢蓝色" vs "最喜欢绿色")，提示 LLM 确认
- **Claude 原生持久记忆**: `.claude/projects/-home-yomu-Agent/memory/` 目录为空，从未启用
- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **EmotionEngine 升级**: 生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充
- **STT 情绪→mood 映射升级**: 生理状态调制 + 性格调制 + 时间衰减
- 唤醒词检测（openWakeWord）— 已完成实现，待办：
  - 测试唤醒词开关：启动服务端，打开前端设置面板，点击开关确认 ON/OFF 切换正常
  - 训练自定义唤醒词模型 (.onnx)：录制每个唤醒词 100-500 样本 → 用 openWakeWord Colab 训练 → 将 .onnx 文件导入 `model_paths`
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **角色切换**: 加载不同角色配置（名字、性格、发音人、prompt 模板），需独立 spec。涉及 CharacterConfig 重新加载、TTS voice lookup 逻辑修复（`voice` 字段当前是死代码）、CognitiveLoop 角色引用更新。`gpt_sovits_voices.yaml` 目前只有 `Elysia` 一个发音人
- **多角色 prompt 模板**: `PromptAssembler` 通过 `SystemPromptManager` + 11 `PromptSection` 渲染，多角色时加载不同 `CharacterConfig` 即可切换 section 数据源
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。已加开关可关闭释放资源，开启时仍存在。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + SenseVoice + GPT-SoVits）。数据库已完成 docker-compose.yml（MongoDB + Milvus）。
- **C3 情绪传染规则动态化** → 后续 PersonalityEngine Phase 2
- **C6 说话风格动态化** → 后续 PersonalityEngine Phase 2
- **C7 主动性动态化** → 后续 PersonalityEngine Phase 2
- **F2 代码块格式重新启用**: `f2_output_format.j2` 中 `<|code|>` 和 `<|markdown|>` 格式已注释。等需要代码/Markdown 渲染功能时取消注释即可启用
- **C5 输入来源标注**: prompt 中当前无法区分用户是用文字还是语音输入。需在 sensory_ctx 中增加 `input_source` 字段（text/voice），模板中显示 "输入方式: 文字 / 语音"
- **PersonalityEngine Phase 2**:
  - AwarenessTrigger: L2/L3 语义偏差检测（向量相似度替代轮次冷却）
  - L3→L2 梯度压缩自动化（LLM 总结+去噪+合并）
  - ExpressionAdapter 完善版：阻尼机制移入 C6 SpeakingStyle
  - L4→L3 情绪汇聚统计（需 EmotionEngine 改接口）
  - L2 永久存储（独立 MongoDB 集合 / 文件，替代 session TTL 风险）
  - C3/C6/C7 与 PersonalityEngine 动态联动
  - 用户话题检测触发 C9（目前只看状态存在+冷却）

### 后台任务系统 — 后续迭代

后台任务分级 + DAG 依赖解析（`core/task.py` 中预留了 `run_in_background` / `depends_on` 字段，当前未使用）。

- `depends_on: list[str]` — TaskScheduler 需在出队前检查依赖是否完成
- `run_in_background: bool` — Worker Loop 需对 BACKGROUND 类型任务走独立执行路径
- `progress: float` — Worker 执行期间更新进度，前端可轮询

## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 高 | `Module/schema/` 479行预留设计 | 已添加 deprecation 注释，保留参考 |
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行，保留参考 |
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |

## 多模态 Phase 2

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 中 | **AI → 用户图片输出** | 工具返回图片（WebFetch 截图、图片生成）+ LLM 生成图片 → OutputManager 广播 → 前端渲染。`ToolResult.images: list[MediaItem]` 已设计，需打通 CognitiveLoop → session → 前端链路 |
| 低 | **文件支持** | `type: "file"` content part 实现（上传 + LLM context + 前端渲染） |
| 低 | **音频/视频 Part** | `VideoPart`/`AudioPart` 已定义 dataclass，需实现全链路 |
| 低 | **content_type 迁入 content** | assistant 消息的 markdown/code block 索引迁入 content array，减少独立字段 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` API key 需要轮换 | 已有 `.env.example` + `setup.sh` 生成 `.env` + `app.py --check` 验证；实际轮换需在各 API 后台手动操作 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | 会话管理 | 无法新建/切换/列出对话。SessionMemory 的 MongoDB 持久化已为此做好准备，需前后端联动 |

## 来自 code-review-report-2026-06-22 的遗留问题

### 严重（需独立 spec）

| 问题 | 文件 |
|------|------|
| Milvus `_metric_type` 跨集合污染 | `database/vectordb/milvus.py:39` |
| NoSQL 注入 — MongoDB collection 名无校验 | `database/documentdb/mongodb.py:31` |
| Embedding 工厂缓存忽略 kwargs | `embedding/registry.py:30-35` |

### 需独立 spec

| 类别 | 数量 | 说明 |
|------|------|------|
| 无障碍 (a11y) | 12 | `aria-*`、颜色对比度、`:focus-visible`、键盘导航、toggle 非 button |
| CSP/安全头 | 2 | CDN `integrity`、Content-Security-Policy |
| ScriptProcessorNode→AudioWorklet | 1 | 麦克风管线换新 API |

### 中低危（待排期）

| 问题 | 文件 |
|------|------|
| Token 用量 O(n) 聚合（已降 limit 100k→2k，长期用 MongoDB `$group`） | `input/server.py:271` |
| llm_adapter close fire-and-forget | `llm_adapter.py:140-143` |
| openai_tts test() 假阴性 | `tts/openai_tts.py:73` |
| TTS 取消+HTTP 流清理交互可能泄漏 | `tts/gpt_sovits.py:134-150` |
| Optional 嵌套 dataclass | `config/loader.py:176-180` |
| `_audio_seq` 长期方案绑定 ws 生命周期（已加 32→16 cap 缓解） | `output/websocket.py` |
| 不稳定测试 ×N | 硬编码 `sleep(15)`、`sleep(8)`、10ms 精度断言 |
| 缺失测试覆盖 | DescribeImagePlugin (0)、CognitiveLoop 边界、OutputManager 错误隔离 |

## 测试补充

- `Module/input/audio_buffer.py` (3)
- `Module/input/server.py` (4)
- `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时

> 已完成的工作归档在 [docs/completed.md](docs/completed.md)。
