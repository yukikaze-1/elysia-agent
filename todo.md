# 待做

> **CI 配置**：`Module/task_system/` 尚未完善、非当前开发重点，`test-quick` 和 `test-normal` 已通过 Makefile 跳过该目录（`--ignore=Module/task_system/tests`）。完整跑 `test-full` 或手动指定路径时仍会执行。

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **LLM 有时不输出 <mood> 标签** | deepseek-v4-flash 遵循结构化输出不稳定，部分轮次 `<mood>` 缺失，回退到关键词 fallback。需强化 prompt 或在 F2 加更强制性的约束 |
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 低 | **`test_integration_global_constraints_respected` 偶发失败** | `Module/task_system/tests/integration/test_delegation_preparer.py`: 全量跑偶发 FAIL，单独跑 PASS，疑似 TaskSystem 集成测试时序/资源竞争 |
| 低 | **`test_multi_step_task` 偶发失败** | `Module/task_system/tests/integration/test_planner.py`: DeepSeek API 偶发返回截断 JSON (`Unterminated string`)，LLM 输出不稳定导致 `PlanParseError` |
| 中 | **TTS 密集短句导致 wait_all 阻塞** | TTS 连续 5+ 短句密集入队时 MiniMax 部分请求无响应，`_transition_idle()` → `wait_all()` 永久等待，FSM 卡在 SPEAKING。已缩短 read timeout 120s→30s 缓解，根因（wait_all 无超时）待重现后修复。 |
| 低 | **`flush_after_response` 超时层不匹配** | 内层 `loop.py:875` 每 task 5s，外层 `lifecycle.py:170` 整体 10s。3+ task 同时超时可能被外层截断。实际 pending 典型 0-2 个、亚秒级完成，impact 极低。修法：外层 timeout 改为 `max(10.0, len(pending) * 5.0 + 1.0)` |
| 低 | **`ConfirmModal` @keydown 在不可聚焦 `<div>`** | `frontend/src/debug/components/shared/ConfirmModal.vue:35` — overlay div 无 `tabindex`，依赖按钮聚焦后事件冒泡。弹窗内只有文字+两个按钮，点击文字不会失焦，现有行为已可靠。加固：加 `tabindex="0"` |

## 来自 code-review-2026-07-31 的遗留问题

已修复 41/44（1 Critical + 20 Major + 20 Minor），3 项遗留。详见 [docs/code-review-2026-07-31.md](docs/code-review-2026-07-31.md)。

### 已修复（2026-07-31）

| 问题 | 修复 |
|------|------|
| C1 `_poll_mailboxes` 自旋 | `continue`→`break` + `stop()` 清空 `_background_tasks` |
| M1 截断方向 | `conversation_text[-2000:]` |
| M3 Reflector buffer 竞态 | `_reflect()` 记录 snapshot，只清反思前累积 |
| M4 Gate 拒绝未恢复 THINKING | gate 拒绝时 `return_to_thinking()` |
| M5 text-only 双广播 | SENTENCE 分支去重，只保留 TEXT_DELTA 增量 + `send("")` 边界信号 |
| M6 usage chunk IndexError | `not chunk.choices` 先判空，usage 判断移到 `choices[0]` 前 |
| M7 `_format_tool_summary` 崩溃 | `isinstance(parsed, dict)` 检查（含 tool_executing emit 循环） |
| M10 `/debug/api` 认证绕过 | `startswith("/debug/api")` 覆盖精确路径 |
| M13 重连审批广播 | 补 `type` 字段 + 参数名对齐正常路径 |
| M14 历史分页重复气泡 | 游标取页尾最旧 + 按 message_id 去重 |
| M15 queued 气泡永久隐藏 | `addUserMessage` 兜底清除所有 queued 标记 |
| M16 唤醒词死 UI | `wakeEnabled` watch 发送 `wake_toggle` |
| M17 `python -m Module.system_prompt` 必崩 | `ConflictRulesSection` 传 `f1_conflict_rules.j2` |
| M18 `python -m Module.config` 必崩 | `__main__.py` 改用 `yaml.safe_load` |
| M21 模型切换双插 | 去掉乐观插入，只等后端结果 |
| M23 Escape 双重触发 | App.vue 检查 `approvalRequest` 非空时跳过 interrupt |
| m7 archive role 非法值 500 | `MessageRole` 解析加 try → 400 |
| m8 版本历史 `int()` 无保护 500 | 两处 GET handler 加 try → 400 |
| m10 WS 非 dict JSON 崩溃 | `isinstance(data, dict)` 检查 → ValidationError 走 400 路径 |
| m18 token_usage 字段名 | 前端兼容 `by_model`/`byModel` |
| m19 消息时间戳恒当前时间 | 6 处 `m.created_at`→`m.timestamp` |
| m21 模型切换双插 | 同上 M21 |
| m22 时间分隔符索引错位 | `shouldShowTimeDivider` 用 visibleMessages 前一条 |
| m23 审批弹窗 Escape 双重触发 | 同上 M23 |
| M9 gpt-sovits `_pull` 异常静默吞掉 | `except httpx.HTTPError`（覆盖 RequestError + HTTPStatusError）+ 异常入队，消费者检测后 raise |
| M11 `time_of_day` 去重键无日期 | 键加日期成分 `%Y-%m-%d %H:%M`，匹配用 `now[11:]` |
| M12 STT 语音输入不 touch | 语音路径投递事件前调用 `agent._idle.touch()` |
| m3 fire-and-forget consume_trigger 无异常处理 | reflector.py + engine.py 两处 `create_task` 加 try/except 包装 |
| m4 KMeans 同步阻塞事件循环 | `_cluster_entries` 改 async，KMeans 移入 `asyncio.to_thread` |
| m11 WS 无 heartbeat/接收超时 | `WebSocketResponse(heartbeat=30, receive_timeout=60)` |
| m13 OpenAI embedding 无超时 | `AsyncOpenAI(timeout=60.0, max_retries=2)` |
| m14 `get_caller_logger` 双跳 f_back | 改单跳 `frame.f_back`（调用点无 Factory.create 中间层） |
| m15 MilvusBackend.close() 空实现 | `asyncio.to_thread(self._client.close)`（pymilvus 有 close） |
| m17 `Module/system_prompt` eager import | `MessageBuilder` 改为 `__getattr__` 内延迟导入 |
| m20 SSE 重连退避失效 | `connect()` 不再清零 `reconnectDelay`，`onopen` 时重置为 1000 |
| m1 shutdown flush 与 cancel 竞态 | CognitiveLoop `mark_shutting_down()` 门禁——after_response 检查后提前退出 |
| m2 start() 失败僵尸 Agent | start() 幂等检查 + 失败时回滚（调 stop()） |
| m5 `update_text` embedding 不可用陈旧向量残留 | emb 为空时标记 `_dirty_ids`（对称 except 分支） |
| m6 图片上传 multipart DoS | `field.read(max_bytes + 1)` 限制读取，超大提前 413 |
| m9 `deafen(2.0)` 长回复回声 | SPEAKING 期间持续刷新 deafen，TTS 停止后自然过期 |
| m12 `flush_emojis()` 时序 | 从 `_finalize_turn` 移到 `finish_turn`（wait_all 之后） |
| m16 DelegationPreparer 连接池泄漏 | 加 `aclose()`，`_stop_task_system` 中调用 |
| M8 MiniMax 停顿标签剥离，F3 停顿失效 | StreamProcessor 拆分 `_strip_pause_tags`/`_strip_tail_pause_tags`——SENTENCE 保留完整标签送 TTS，TEXT_DELTA 剥离供前端；TTSAdapter `tts_text` 不再剥标签。验证 `<#0.5#>` 内小数点已被 `_is_dot_boundary` 数字保护，原剥离是过度防御。补 4 个测试 |

### 遗留（3 项）

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **中** | **M2 `_sync_dirty_entries` 死代码，Milvus 失败记忆永不重试** | [long_term.py:282-315](core/memory/long_term.py#L282)。insert 失败入 `_dirty_ids` 但无调用者同步；重启后脏条目彻底丢失。需接线到 `_db_health_loop` |
| **中** | **M19 resume 后再次 pause 被销毁** | [task_system.py:384-393](Module/task_system/task_system.py#L384)。缺 PAUSED early-return 保护，mailbox 消费者永久阻塞 |
| **中** | **M20 cancel 在规划阶段不推终态事件** | [lifecycle.py:157-167](Module/task_system/lifecycle.py#L157) + [planning.py:143-145](Module/task_system/core/planning.py#L143)。`cancel()` 尚无调用方接线，随任务系统接入时修复 |

> 已过滤 3 条（1 误报：重试 off-by-one；2 条置信度 50）。M2 需设计，M19/M20 随 TaskSystem 接入。修复优先级建议见源文档末尾。

## 功能改进

- **README 截图**: 添加聊天界面 / debug 面板 / 启动流程截图，让新用户第一眼就知道项目长什么样
- **Live2D 集成**: Cubism SDK v5 需 TypeScript + npm + bundler 构建，当前仅 SDK 文件到位、默认关闭
- **poignancy 时间衰减**: rerank 含时效衰减但 poignancy 本身不变，长期不访问的记忆应逐渐降级
- **MiniMax SSE 真流式**: 已尝试并回退 (2026-07-10)。MiniMax SSE 每个 event 是独立可播 mp3（非增量片段），拼接导致重复，只取最后 event 又等于同步下载。待 MiniMax API 支持增量 SSE 或换方案（WebSocket）
- **MiniMax 异步长文本合成**: `t2a_async_v2`，适合 >3000 chars。需创建任务 → 轮询状态 → 下载文件
- **记忆导出/备份**: 支持 JSON 导出和恢复
- **记忆统计**: 按类型/年龄/poignancy 分布统计，接入 DebugInfo 仪表板
- **Claude 原生持久记忆**: `.claude/projects/-home-yomu-Agent/memory/` 目录为空，从未启用
- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **CharacterEngine mood 升级**（原 EmotionEngine 升级）: LLM `<mood>` 标签驱动已实现。生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充等后续 Phase 待做。
- 唤醒词检测（openWakeWord）— 已完成实现，待办：
  - 测试唤醒词开关：启动服务端，打开前端设置面板，点击开关确认 ON/OFF 切换正常
  - 训练自定义唤醒词模型 (.onnx)：录制每个唤醒词 100-500 样本 → 用 openWakeWord Colab 训练 → 将 .onnx 文件导入 `model_paths`
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **角色切换**: 加载不同角色配置（名字、性格、发音人、prompt 模板），需独立 spec。涉及 CharacterConfig 重新加载、TTS voice lookup 逻辑修复（`voice` 字段当前是死代码）、CognitiveLoop 角色引用更新。`gpt_sovits_voices.yaml` 目前只有 `Elysia` 一个发音人
- **多角色 prompt 模板**: `PromptAssembler` 通过 `SystemPromptManager` + 12 `PromptSection` 渲染，多角色时加载不同 `CharacterConfig` 即可切换 section 数据源
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。已加开关可关闭释放资源，开启时仍存在。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + SenseVoice + GPT-SoVits）。数据库已完成 docker-compose.yml（MongoDB + Milvus）
- **F2 代码块格式重新启用**: `f2_output_format.j2` 中 `<|code|>` 和 `<|markdown|>` 格式已注释。等需要代码/Markdown 渲染功能时取消注释即可启用
- **意图路由 + 渐进式工具披露**: 当前 13 个工具全量暴露给 LLM，每人次 ~1,643 schema tokens。大部分对话只用 2–4 个工具，~70% schema token 是浪费的。评估结论：13 个工具未到临界点，暂不做。触发条件：工具数 > 15 考虑规则分类，> 20 必须引入分类层，或累计 ≥ 3 次工具误用事件。推荐方案：关键词匹配分组（filesystem/web/memory/utility），零延迟零 token 成本，无命中则全量兜底。
- **CharacterEngine Phase 2**（原 PersonalityEngine Phase 2）:
  - CharacterReflector.j2 模板 ~140 行偏长，LLM 注意力可能被稀释，后续观察产出质量决定是否精简
  - C3 情绪传染规则动态化
  - C7 主动性动态化
  - L4→L3 情绪汇聚统计（需 CharacterEngine 改接口）
  - 用户话题检测触发 C9（目前只看状态存在+冷却）


## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行，保留参考 |
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |
| 低 | **FilePart/AudioPart/VideoPart 预留代码** | `part.py` ~70 行 + `message_adapter.py` 3 个 helper + `Part` union。`message_adapter.py:188` 注释明确 Phase 2。暂保留 |

## 模块化改进

| 优先级 | 目标 | 说明 |
|--------|------|------|
| ~~低~~ ❌ | **`Module/tool/safety.py`（618 行）** | 评估结论：不拆 |

## 多模态 Phase 2

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 中 | **AI → 用户图片输出** | 工具返回图片（WebFetch 截图、图片生成）+ LLM 生成图片 → OutputManager 广播 → 前端渲染。`ToolResult.images: list[MediaItem]` 已设计，需打通 CognitiveLoop → session → 前端链路 |
| 低 | **文件支持** | `type: "file"` content part 实现（上传 + LLM context + 前端渲染） |
| 低 | **音频/视频 Part** | `VideoPart`/`AudioPart` 已定义 dataclass，需实现全链路 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` 仍含真实 API Key | DeepSeek、MiMo、Tavily、MiniMax 的真实 key 仍在 `.env` 文件中。已有 `.env.example` + `setup.sh` + `app.py --check`，但 `.env` 不应提交真实密钥。建议：将 `.env` 中 key 替换为占位符，真实 key 通过 shell 环境变量或密钥管理器注入 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | 会话管理 | 无法新建/切换/列出对话。SessionMemory 的 MongoDB 持久化已为此做好准备，需前后端联动 |
| **高** | **无数据库迁移机制** | MongoDB 索引在 `connect()` 时 ad-hoc 创建，Milvus collection 硬编码创建。若字段/索引/维度需变更，无 upgrade path。`memory_repo.clear_all()` 是破坏性操作无安全守卫 |
| 低 | **`character/cyrene/` 不可用** | 缺少 `config.yaml` 和 `personality.md`，仅含 GPT 参考材料，无法作为角色加载 |
| 低 | **依赖无版本上限** | `requirements.txt` 中 pydantic、openai、torch 等均无上限约束，上游 breaking change 可导致不可预期问题。`torch` 是 2GB+ 重依赖 |
| 低 | **`# type: ignore` + `Any` 类型清理** | **# type: ignore**：生产代码 32 处→已消除 9 处→剩余 23 处（二/三梯队）。测试文件 IDE 类型错误已批量修复 ~155 处（2026-07-29/30：NewType 包装、ToolAdapter 替换、type: ignore 补充）。**`Any` 类型替换**：core/ 下 63 处→已消除 ~44 处（属性声明/task/emit 签名/mailbox/_process_events）。**剩余不可消除**：`dict[str, Any]` JSON 容器（~12 处）、流式协议 `**raw_params: Any`+`AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处）。详见 `docs/completed.md` 2026-07-29 第一、三条 + 2026-07-30 第一条。 |


## 来自 code-review-report-2026-06-22 的遗留问题

### 需独立 spec

| 类别 | 数量 | 说明 |
|------|------|------|
| 无障碍 (a11y) | 12 | `aria-*`、颜色对比度、`:focus-visible`、键盘导航、toggle 非 button |
| CSP/安全头 | 2 | CDN `integrity`、Content-Security-Policy |
| ScriptProcessorNode→AudioWorklet | 1 | 麦克风管线换新 API |

### 中低危（待排期）

| 问题 | 文件 |
|------|------|
| llm_adapter close fire-and-forget | `core/adapter/llm_adapter.py:140-143` |
| openai_tts test() 假阴性 | `tts/openai_tts.py:73` |
| TTS 取消+HTTP 流清理交互可能泄漏 | `tts/gpt_sovits.py:134-150` |
| 缺失测试覆盖 | DescribeImagePlugin (0)、CognitiveLoop 边界、OutputManager 错误隔离 |

## 测试补充

- `core/input/audio_buffer.py` (3)
- `core/input/server.py` (4)
- `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时
- 上述覆盖率缺口中标记为"关键"和"高"的路径

> 已完成的工作归档在 [docs/completed.md](docs/completed.md)。
