# 兜底机制审查报告 — 2026-08-04

## 审查概览

- **主题**：全仓库「兜底」（fallback / 降级 / 回退 / 容错）机制专项审查——哪些地方有兜底、为什么兜底、是否有必要兜底。
- **范围**：`core/`（cognitive / adapter / memory / storage / personality / config / schema / agent / input / monitoring / task / tools / hitl / text）、`Module/`（llm / tts / stt / embedding / output / config / logger / database / tool / _common / workflow_engine / workflow_contract）、`frontend/src/`。排除 tests、docs。
- **方法**：7 个并行审查 agent 按子系统分片 → 结构化发现（位置 / 兜底内容 / 原因 / 必要性评估）→ 对高风险项逐一读源码实证复核。
- **审查基线**：commit `3139d11`（工作树干净）。

## 结论摘要

| 判定 | 数量 | 说明 |
|------|------|------|
| 合理防御（必要） | ~180 | 设计内降级，保护「不丢记忆 / 不丢句 / 用户始终看到回应 / 可用性优先」等不变式 |
| 静默吞错 / 掩盖错误 | 12 | 无日志或日志级别过低，掩盖真 bug / 真数据丢失（详见第三节） |
| 过度防御 / 应改快速失败 | 8 | 把「不应发生」的分支降级为静默语义转换而非崩溃暴露（详见第四节） |
| **合计识别** | **~200** | 全仓库兜底点 |

**总体判断**：该代码库的兜底体系是**刻意设计且大部分合理**的，不是随手加的防御。三条设计主线贯穿始终：**① 可用性优先于强一致**（DB/服务不可用时降级而非崩溃）；**② 核心不变式绝不妥协**（记忆不丢、句子文字不丢、用户始终收到回应）；**③ 错误回流给 LLM 而非崩溃**（ReAct 工具层标准模式）。真正需要处理的是 **12 处静默吞错**（掩盖真实错误或数据丢失）和 **8 处过度防御**（应改为快速失败）。未发现「掩盖真 bug 到不可诊断」级别的系统性错误兜底。

---

## 一、兜底的定义与分类

本次审查覆盖的「兜底」分为四类：

1. **异常降级**：`except` → 返回默认值 / 空值 / 替代行为。
2. **主路径失败回退**：retry、重试仍失败后的兜底回复、TTS 失败改发文字、MongoDB 失败回退 JSONL 等。
3. **三态 / 可选值默认回退**：`None → config` 默认值（如 `_stt_enabled` / `_tts_enabled`）。
4. **配置层级回退**：`env var > YAML > dataclass default`（`Module/config` 核心设计）。

---

## 二、按子系统的兜底清单

### 2.1 `core/cognitive/` + `core/adapter/`（LLM / TTS 主链路）

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `loop.py:68-75, 808-821` | LLM 200 但无文本/无 tool_calls → nudge 重试一次；仍空 → 硬编码兜底回复「抱歉，我刚才走神了…」并同步填 `all_sentences` | 保护「用户始终看到回应」不变式；同步填 `all_sentences` 避免 `build_parts([])` 空 parts 次生 bug |
| `loop.py:288-292` | 主动搭话无 request_id → 自增 `P{seq:03d}` | 日志/trace 串联需要非空 id，纯日志标识符 |
| `loop.py:692-695` | 整轮未解析出 `<mood>` → 调 `character_engine.analyze_user` 关键词情绪检测，同步 `set_emotion` | `<mood>` 是可选标签，C3/C4 依赖 mood 状态 |
| `loop.py:700-703` | `public_text` 非空但无 SENTENCE 事件 → 整段文本一次性 `output.send` | 保证文本在「无句可流」边界情况仍送达（如纯 code/markdown 块） |
| `loop.py:864-895` | `_after_response` 各 hook（consolidator / character buffer / memory_reflector / session.save）独立 try/except，失败仅 log 后继续 | 记忆沉淀/性格缓冲是后台非关键路径，单失败不拖垮整轮 |
| `loop.py:910-916` | after-response task 超时（5s）→ cancel，异常 → warning | shutdown 时防僵尸 task 阻塞退出 |
| `loop.py:201-222` | `_phases`/`_status` 未注入时懒加载构造 FSMBacked 实例 | 支持 CognitiveLoop 独立构造（测试/无 Agent 场景） |
| `loop.py:972-983` | TTS 不可用时 SENTENCE 只发空串结束气泡、文本走 TEXT_DELTA 增量广播 | TTS 通道降级为纯文字，避免整句重复显示 |
| `loop.py:654-660` | `_executing_tools.remove` 不存在时 `except ValueError: pass` | debug 面板追踪 best-effort，重复执行同名工具可能已清 |
| `tools.py:68-77` | 工具异常 → `ToolResult.fail`（CancelledError → 用户取消），失败结果回喂 LLM 自恢复 | ReAct 标准错误恢复；有 warning 日志非静默 |
| `tools.py:25-32, 258` | `_user_profile` 为 None → 空 `UserProfile` | submit_task 委派需要参数但注入可能缺失（v1 占位） |
| `tools.py:270` | 澄清建议缺省 → 「能再具体一点吗？」 | LLM 未给建议时给通用提示 |
| `delegation.py:240-246` | usage None/缺字段 → 空 dict / 0 | token 统计是可选元数据 |
| `delegation.py:285-328` | LLM 幻觉工具名与实际工具集求交集剔除；`ok` 但筛后为空 → 降级 `no_matching_tools` | **防御 LLM 不可靠输出的核心闸门**，Planner 不会引用不存在工具 |
| `delegation.py:303-304` | `llm_tools` 非 list → 空列表 | LLM 可能输出 `"tools": "search"` 等畸形值 |
| `delegation.py:487-491` | aclose 失败 → pass | shutdown 时 close 失败不应阻塞退出 |
| `llm_adapter.py:47` | model None → `deepseek/deepseek-chat` | 适配器可无参构造 |
| `llm_adapter.py:119-144` | 429/500-504 指数退避重试 ≤3 次（1s/2s/4s）；流已开始不重试；耗竭 re-raise | **本仓库 retry 标杆实现**——有界、有退避、失败向上传播不静默 |
| `llm_adapter.py:129-132` | 无 `stream_think` 后端 → 整段逐字符 yield 模拟流 | 适配统一流式接口（性能差但兼容） |
| `tts_adapter.py:122-125, 299-300` | TTS 禁用/无后端 → no-op | 句子文本改由 loop TEXT_DELTA 路径纯文字送达 |
| `tts_adapter.py:132-134` | 预处理后为空 → 跳过合成 | 空句送 TTS API 会 400 |
| `tts_adapter.py:250-260, 284-298, 369-383` | `set_enabled(False)` → per-task `flush_on_cancel`：取消时把排队未播 `display_text` 经 `on_text_delta` 广播（`text_broadcast` 防双播） | **「TTS 关闭不丢句」核心不变式**；竞态分析准确 |
| `tts_adapter.py:384-397` | TTS 合成/播放失败 → warning(exc_info) + 文字兜底广播 | 云服务失败不能让句子彻底消失，文字是 TTS 最低保证 |
| `tts_adapter.py:398-405` | finally 释放 `seq+1` 门控 event（不存在则创建） | **防死锁**——句 N 失败若卡住会让后续所有句子永久卡死 |
| `tts_adapter.py:359` | `stream_format` 默认 "wav" | 后端未暴露格式时给默认值（恒非 None，纯防御） |
| `tool_call_parser.py:59, 62-64, 73-74` | 畸形 tool_call → warning 后 skip；arguments 非 str → dumps/置空 | 流式增量不完整/坏数据不应崩整轮 |
| `tool_adapter.py:59` | `services or {}` | 可选依赖注入 |
| `message_builder.py:49, 52, 84-93, 116-121` | 历史字段 getattr 默认；图片 → `describe_image` 文本提示；工具摘要参数解析失败 → 仅列 name | 多模态能力差异桥接 + 旧格式兼容 |
| `prompt_assembler.py:119-120, 159` | mood/speaking_style 默认 `neutral`/`""`；content 缺省回退 user_message | 可选依赖安全默认 |

#### 值得注意（见第三/四节详述）

- `loop.py:623-625` — gather 结果中 `BaseException` 直接 `continue`，**无日志**（静默吞错）。
- `llm_adapter.py:75-82` — `think()` JSON 解析失败 `pass` 后当纯文本返回，**无任何日志**（静默吞错，工具 JSON 可能被当回复发给用户）。
- `loop.py:336-349` — `process()` 顶层 `except Exception` 捕获 *所有* 异常（含代码 bug）却统一标注「LLM 调用失败」发给用户（归因错误，有误导）。
- `loop.py:808-821` — 空响应兜底只记 warning，**无计数器/指标**——系统性 bug 会持续发「走神」回复而不升级告警。
- `delegation.py:364-374` — 自注「这不应该发生」的分支返回 `NO_MATCHING_TOOLS` 而非 `raise`（过度防御）。
- `llm_adapter.py:190-193` — `switch_model` 同步上下文 `create_task` 抛 RuntimeError → `pass`，旧后端连接池不关闭（泄漏隐患）。
- `tts_adapter.py:407-423` — `synthesize_legacy`（legacy/test 路径）异常仅 `logger.debug`，默认配置不可见（最接近纯吞错）。

### 2.2 `core/memory/` + `core/storage/`（持久化链路）

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `session.py:245-285` | `save()` MongoDB 失败 → JSONL 落盘（tmp + `os.replace` 原子替换） | 保护「session 跨重启不丢」；崩溃一致性 |
| `session.py:317-391` | `load()` MongoDB 失败/无结果/异常 → JSONL 恢复（merge 模式按 message_id 去重） | 与 save 对称，跨重启会话内容不因单存储故障丢失 |
| `session.py:353-355` | JSONL 解析失败 → warning + False，不清空现有内存 | 恢复失败不破坏已加载内存状态 |
| `long_term.py:84-91` | Milvus 连接失败 → 纯内存 + 字符重叠搜索 | 记忆系统四级降级矩阵（设计核心，`docs/database-storage.md:33`） |
| `long_term.py:107-118` | 启动从 Milvus 加载失败 → warning + 0 条；entry_id 缺失 → `new_message_id()` | 加载失败不阻塞启动 |
| `long_term.py:176-196` | 嵌入为空/Milvus 写入失败 → 内存 + `_dirty_ids` 标记，条目总在 `_entries` | 保护「插入记忆绝不丢失」，dirty 供 `sync_dirty_entries` 补写 |
| `long_term.py:350-378` | `update_text` 嵌入失败 → dirty 保留旧向量，不静默用旧向量绑新文本 | 防「重启后旧文本静默回滚」语义错乱 |
| `long_term.py:462-554` | search 多级降级链：向量路径 → 字符重叠 → rerank 相似度 1/(1+d) / fb_score / 默认 0.3 | Milvus/嵌入全挂时语义检索仍能召回 |
| `embedding_provider.py:47-73, 86-92, 106-124` | 后台线程加载 + 10s 超时 → text-only；OpenAI 失败 → HuggingFace `bge-large-zh` → None | 分层后备 + 超时守护，空向量是明确的「无嵌入」信号 |
| `memory_reflector.py:46-51, 127-129, 164-165, 202-221, 285-300` | 模板缺失 → 内置文案；反射锁占用 → 跳本轮；单组失败 → continue；KMeans 失败 → 按 type 分组；fence 剥离 + 正则兜底 | 后台反射不拖垮主流程 |
| `memory_reflector.py:325-329` | merge 目标不存在 → 跳过且**不** mark_absorbed | **「拒绝降级」保护数据完整性的正面例子** |
| `consolidator.py:246-255` | 提取 LLM 抛异常 → 返回 [] 且保留缓冲区（仅成功后 clear） | 缓冲保留是重试前提，防数据丢失 |
| `consolidator.py:277-286, 339-350` | 首次整合跳过比对直接 insert；compare 失败 → 全量 insert-all 回退（`_parse_compare_actions` 显式 count 校验，空数组也 raise→回退） | 「不丢记忆」核心不变量：容忍重复优于丢失 |
| `storage/*` 各 repo | `connect()` 失败 → `_db=None` + warning，操作前置 `if _db is None: return 空/0/None` | 与调用方降级链闭环 |
| `session_repo.py:152-156` | load 缺省字段（cs/rs/ss 缺失 → 默认实例） | 旧文档向后兼容 |
| `character_version_repo.py:148-153, 190-194` | content_type 缺失 → 视为 l2 | 旧文档兼容 |
| `token_repo.py:92-95` | 逐文档 ValidationError → continue | 旧文档跳过，不迁移自然老化 |
| `schema/adapters/message_adapter.py:49-64` | 新模型校验失败 → 剥除旧 `token_usage` 键重试一次；仍失败 → **re-raise 原异常** | 范围最规范的兜底：单键、单次重试、失败不静默 |
| `schema/memory.py:26, 45-56` | `poignancy` 默认 5（合法域内）；Update/Merge 的 type/poignancy 可选 | 防 LLM 偶发缺字段丢整条记忆 |

#### 高风险（见第三节详述）

- `session.py:66-69` + `message_repo.py:99-100` — **永久消息归档静默丢失**（最高风险）。
- `message_repo.py:88` — **无重连逻辑**，与 session_repo/memory_repo 不对称。
- `character_version_repo.py:53-57, 66-69` — 重复文档检测失败 → 整个 repo 降级不可用，掩盖数据完整性问题。
- `consolidator.py:116-123, 246-255` — `_extract_json_array` 解析到空 == 成功 → 缓冲区被清，整合机会静默丢失（已源码复核：`_parse_extraction` 不抛异常时 `[]` 走「成功」分支 → L255 `_buffer.clear()`）。

### 2.3 `core/personality/` + `core/config/` + `core/schema/`（性格 / 配置 / 状态）

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `emotion.py:153-154` | 无关键词命中 → `neutral`（mood 兜底链末端） | 完整链：`<mood>` 标签（loop.py）→ keyword 检测 → neutral 无更新 |
| `engine.py:166, 203-205, 320-327, 341-346` | L2 空渲染空串；空触发 return；state_history 截断同步调减压缩计数；`_safe_buffer` 捕获 fire-and-forget 异常 | 状态一致性 + 后台任务不崩溃（带 exc_info） |
| `reflector.py:137-139, 199-206` | buffer 不足 10 对跳过；首次反思用 10 对阈值替代时间触发 | 少量对话反思质量低；首轮上下文不足 |
| `reflector.py:307-312` | LLM 调用失败 → 记 `_last_success=False`、推进时间戳 | 避免高频重试 + 调试面板可见失败状态 |
| `awareness.py:105-112` | L2 为空 → 降级：不调 LLM，直接缓存 L3 原文 | 无 L2 无比较对象，LLM 判定无意义 |
| `awareness.py:125-142, 148-158` | 模板/LLM/解析失败 → 保留旧缓存；**CancelledError 正确重抛** | 失败不破坏已批准文本 |
| `compression.py:81-97, 102-104, 139-148, 201-219` | pm None 守卫（4 处）；压缩锁跳本轮；条目不足 3 跳过；模板/LLM 失败 return 下轮重试 | 压缩全链路降级 + 防并发 |
| `compression.py:243-252` | `version_repo` None → 跳过版本历史写入 | 版本仓库是可选依赖 |
| `compression.py:264-274` | C6 首次无基准 → 渲染 L1 默认模板作基准 | 首次刷新需要起点文本 |
| `character_config.py:68, 73-92` | 空 YAML → `{}`；全字段 `raw.get(key, default)` | dataclass 默认值语义 |
| `character_config.py:141-149` | 无 L1 文件 → `personality_block()` 降级为 traits+speaking_style 紧凑描述 | 旧 YAML 向后兼容 |
| `app_config.py` 全文件 | `env > YAML > default` 末级 dataclass 默认 | 配置系统标准层级 |
| `service_state.py:13-14` | `stt_enabled/tts_enabled: bool|None`，None = 用户未显式改 | 三态语义区分「未设置」与「显式关闭」，重启后仍可区分 |
| `message_adapter.py:167, 172-202` | 未知扩展名 → PNG；图片源多级解析失败 → 原样透传 src | 本地文件找不到保留引用，远程 URL 直通 |
| `_utils.py:47-48` | `_schedule` 无事件循环 → pass | 测试环境兼容（有注释） |

#### 值得注意

- `character_config.py:127-131` — L1 Core 文件缺失 → warning + `return ""`（正式角色应 fail-fast，见第四节）。
- `engine.py:109-113` / `reflector.py:67-71` — `_pm = None` 使压缩/C9/C6 三条功能线整体静默禁用，掩盖模板缺失（建议启动日志提级）。
- `compression.py:116-117` — flush 通用异常仅 `logger.debug`，生产近似静默（建议提 warning）。
- `reflector.py:240-246` — 时间戳 `datetime.fromisoformat` 对畸形串抛 ValueError 未捕获，中断本轮反思。
- `compression.py:223-229` — 无 `<new_self>` 仍推进计数/时间（防无限重试，但等效丢弃该批 L3 变更，权衡可接受）。

### 2.4 `Module/` 后端（llm / tts / stt / embedding / output / config / logger / database / tool / _common）

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `config/loader.py:192-224` | 无 yaml 路径/key → 空 dict；YAML null 视为未设置不覆盖默认 | env > YAML > default 核心设计 |
| `config/loader.py:252-255` | env coerce 失败 → `ConfigEnvFormatError`（快速失败） | env 层无静默降级 |
| `config/field.py:101-112` | metadata 缺 key → 默认（False/None/[]） | 字段元数据缺省 |
| `logger/factory.py:200-246` | 模块前缀无匹配 → 默认级别；root 无 handler → WARNING StreamHandler | 未配置 logger 的模块也有输出 |
| `output/manager.py:85-93, 300-317` | `_broadcast` return_exceptions；逐通道 try/except；close gather | 单通道失败不影响其他通道 |
| `llm/openai_compat.py:34-38, 155-156, 218-221` | usage 缺字段 → 0/None；空 choices chunk → continue | 跨 provider 响应结构差异大（MiMo 空 choices 表示流结束） |
| `llm/openai_compat.py:71` | key 占位 `"ollama"` | 本地端点接受任意非空值 |
| `llm/openai_compat.py:256-263` | `reconnect()` 旧 client 关闭失败 → warning 继续 | 重连不因旧连接关闭失败中断 |
| `embedding/openai_compat.py:38, 46` | key → env → 空串；`max_retries=2` | SDK 层标准重试；显式超时防上游挂起拖 10 分钟 |
| `embedding/huggingface.py:102-116` | cache_folder → env → 默认；本地模型查找 → 回退直传 model_name | 离线优先语义 |
| `embedding/huggingface.py:109, 132-137` | dimension 回退链 → 0 时 property 抛 RuntimeError | 使用时快速失败而非静默错维度 |
| `stt/sensevoice.py:47-50, 157-172` | 无情绪标签 → NEUTRAL；`<|nospeech|>` → 丢弃；无 text → warning + 空串 | 语义正确 + 噪音抑制 |
| `tts/openai_tts.py:45, stt/openai_stt.py:60` | key 占位 "tts"/"stt" | custom 自建端点常不校验 key |
| `tts/gpt_sovits.py:134-136, 176-187` | 预处理空 → 跳过；httpx 失败 → 异常对象入队由消费者 raise | 错误传播机制严谨（不静默无音频） |
| `database/vectordb/milvus.py:188` | 缺 `delete_count` → 按请求数 | 跨 pymilvus 版本兼容 |
| `database/vectordb/registry.py:63, 90-92` | token → env；空 base_url 不传让 create() 从 env 解析 | 配置层级正确性 |
| `database/documentdb/mongodb.py:63, 71` | `dict(document)` 拷贝防 motor 注入 `_id` 污染 | 防副作用 |
| `tool/safety.py:27-30, 189-190, 218-219, 413-419, 429-444, 543-544, 629, 765-793` | session_id 非法 → `default`；IP 解析失败 → 按主机名放行；路径解析失败 → 错误串；semaphore 超时 → RATE_LIMITED；执行超时/异常 → TIMEOUT/TOOL_ERROR；HITL 超时 → `(False,"expired")`；类型转换失败 → 错误串汇总 | 工具隔离 + fail-safe（审批缺失视为拒绝） |
| `tool/protocol.py:60-70, 549, 585-589` | DI 未注册 → 默认；summary 空 → 回退 content；无 error_type → 通用文案 | 工具契约降级 |
| `tool/manager.py:86-89, 71-74, 129, 477-493` | 插件 setup 失败 → warning 跳过；config 解析失败 → 默认 SafetyProfile；未注册工具 → UNKNOWN_TOOL | 一个坏插件不拖垮 agent |
| `_common/env.py:14-22` | dotenv 缺失/无 .env → 静默跳过 | 可选依赖（与 preflight 互补） |
| `_common/registry_helpers.py:25-55` | api_key/base_url 层级回退（`is not None` 区分未传与显式空串） | 统一配置层级 |
| `_common/test_helpers.py:22-24` | test() 中 health_fn 异常 → `{"ok":False,"error":str(e)}` | 连通性探测契约 |

#### 值得注意

- `llm/openai_compat.py:233-235` — 非流式空 choices → error 后 **return 空串**（过度防御，见第四节）。
- `tts/minimax_tts.py:312` — `base_resp` 缺 `status_code` → **当作成功**（过度防御，掩盖 API 契约变化）。
- `config/_utils.py:18-23` — type hints 解析失败 → 跳过类型校验（过度防御，错误类型静默通过加载）。
- `tts/minimax_tts.py:34-41` — 音色 YAML 缺失 → 返回 `{}` 无 warning（角色音色错乱难排查）。
- `tts/minimax_tts.py:258` — voice_map 未命中 → 默认音色（掩盖拼错角色名；与 gpt_sovits 的 fail-fast 行为不一致）。
- `embedding/huggingface.py:97-98` — CUDA 能力查询失败 → CPU 无日志。
- `output/websocket.py:60-63` — 发送失败仅 warning，无死连接检测（长会话大量 warning 用户无感）。
- `tool/manager.py:330-331` — HITL 审批广播失败静默（审批弹窗可能永不出现，见第三节）。
- `embedding/registry.py:93-94`、`database/vectordb/milvus.py:200-201` — close 失败静默（泄漏隐患）。
- **范围内无 embedding 后端级 fallback**（huggingface 失败不切 openai）——`core/memory/embedding_provider.py` 才做，行为 fail-fast 正确。

### 2.5 `Module/workflow_engine/` + `Module/workflow_contract/`（任务引擎）

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `workflow_engine.py:147-168` | stop() 等待超时 → cancel；cancel 后仍 pending → 强清注册表 | 优雅停机兜底，3s 上限 |
| `workflow_engine.py:239-251` | `_run_and_cleanup` 异常 → 补推 FAILED | SubAgent 前期抛异常时保证 mailbox 消费者不永久阻塞 |
| `workflow_engine.py:252-266` | PAUSED 任务 keep-alive；存储清理失败仅 log 仍释放槽位 | 暂停可恢复；清理非关键路径 |
| `workflow_engine.py:268-274` | create_task 失败 → 清理注册表 + release + re-raise | 不吞，补偿后冒泡 |
| `execution.py:103-117` | Plan 缺失 → `ABORT` + `logger.critical`（**不静默 CONTINUE**） | **「掩盖错误应改快速失败」的正面范例** |
| `execution.py:270-285` | required 节点被 SKIP → REPLAN（防死锁） | 契约防御性校验，有日志 |
| `execution.py:316-336` | 重试耗竭 → REPLAN（`>` 语义允许恰 max_retries 次） | REPLAN 熔断链路一环 |
| `execution.py:384-388` | TOOL_NOT_FOUND/CONTEXT_OVERFLOW 不重试直接 REPLAN | 结构性错误重试无意义 |
| `evaluator.py:164-171, 216-232, 234-249` | 未知错误码 → ERROR；LLM 空响应/缺键/未知枚举值/格式异常 → GOAL_FAILED | **fail-closed 方向正确**——空响应不会变成「判定通过」 |
| `evaluator.py:251-304` | LLM 调用重试 1 次，两次失败 → None → GOAL_FAILED | DeepSeek 偶发空 content，有重试/放弃日志 |
| `evaluator.py:284-285` / `parser.py:78-79` | TokenLedger 记账失败 → warning 跳过 | 非关键路径不阻断业务判定 |
| `parser.py:78-82` | LLM 空响应 → `raise PlanParseError`（**快速失败**） | 空响应无法产出 Plan |
| `parser.py:112-123, 193-234, 276-293` | 字段缺省：required 默认 True、mode 默认 TOOL_CALL、patch_type 默认 replace_nodes | **默认方向安全**（fail-closed 侧） |
| `replan.py:39-89` | REPLAN 熔断三重上限：版本 >5 / 耗时 / token，触发转 FAILED(FAILURE_MELTDOWN) | 防 REPLAN 死循环烧资源；`>` 精确允许 5 次 |
| `replan.py:167-168, 229-242, 280-298` | plan None → raise；checkpoint 缺失 → FAILED(INTERNAL_ERROR)；校验失败 → FAILED | 无快照/非法方案不能落地，转失败 |
| `replan.py:188-219` | Replan 前 cancel/pause 短路（getattr 兜底） | 避免浪费 LLM 调用 |
| `replan.py:258-270` | replan() 异常 → 降级 FAILED（不静默继续旧 DAG） | 无新方案就无路可走 |
| `execution_loop.py:64-95` | 循环顶复查 cancel/pause；NEEDS_REPLAN 循环重跑 + resume_from 合并 | 针对 exec_state 合并丢标志位 |
| `schema.py:168-221` | `WorkflowConstraints.resolve()`：任务级 None → 全局；超全局 → clamp + warning | **引擎最核心设计最清晰的兜底**——「任务级约束是全局子集天花板」，bug 不崩溃 |
| `schema.py:346` | HITLData.rbac_level 默认 MUTATION | 默认最高风险（fail-closed） |
| `types.py:42-53` | 时间戳 naive → UTC；空串直通；解析失败 raise | 容忍 + 快速失败 |
| `internal.py:262-264` | `max_retries=None → 全局`；execution_mode 默认 TOOL_CALL | 任务级 → 全局降级模式 |

#### 高风险（见第三/四节详述）

- `replan.py:106-107` — `_elapsed_seconds` 解析失败静默返回 0.0 → **时间熔断永不触发**（已复核）。
- `workflow_engine.py:388-397` — resume 路径 finally **缺 PAUSED keep-alive**，与 submit 路径不对称（已复核，重复暂停会删 checkpoint 使任务不可恢复）。
- `internal.py:488-491` — `NodeResultEvaluation` 三字段默认「成功」（建议改必填，防漏判定=静默成功）。
- `evaluator.py:390` — `_derive_action` 默认 CONTINUE 是死代码（5 状态全覆盖），未来新枚举会被静默当成功。
- `execution_loop.py:60-95` — `while True` 自身无上限，终止依赖 `check_meltdown` 约定（约定优于强制）。

### 2.6 `core/agent/` + `core/input/` + `core/monitoring/` + `core/task/` + `core/tools/` + `core/hitl/` + `core/text/`

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `agent.py:606-614` | `_stt_enabled/_tts_enabled` 三态：None → config | 跨重启热插拔核心契约（与 ServiceState 对齐） |
| `agent.py:706-714` | shutdown 窗口 STT backend None → 返回空串 | 关闭瞬间音频仍到达的竞态优雅降级 |
| `agent.py:301-304` | 消息队列满 → warning + 丢弃 | 有界队列防内存无界增长（显式告警） |
| `lifecycle.py:139-144` | LLM reconnect 失败 → STOPPED + raise（快速失败） | LLM 是核心依赖无降级意义 |
| `lifecycle.py:161-166` | 启动失败回滚 stop() → re-raise | 防僵尸 Agent + 重复后台循环 |
| `lifecycle.py:175-230` | stop() 逐步骤吞错继续；4 个 flush 超时/异常 warning | 关闭 best-effort，尽力 flush 记忆 |
| `_lifecycle_persistence.py:57-76` | version_repo 离线 → warning 继续；mongo/milvus 不可用 → 内存+JSONL / 纯本地匹配 | 设计内降级，有明确 warning |
| `_lifecycle_persistence.py:66-72` | JSONL 导入 MongoDB 失败 → 保留 JSONL 不删 | 离线期间快照不能丢 |
| `_lifecycle_services.py:74-75` | `_apply_service_state` 三态回退 | 同 agent 三态 |
| `_lifecycle_services.py:92-112` | TTS 创建/连通性失败 → `_tts=None` text-only | companion 应用不因语音挂掉拒绝启动 |
| `server.py:122-124, 167-169, 221-222` | 切换生效但持久化失败 → warning 不回滚 | 运行中后端是权威，重启可重演 |
| `server.py:145-183, 196-235` | switch_stt/tts 失败 → 关闭未挂载新后端 → 返回 ok:False，旧后端继续 | **标准 hot-swap：先建后关，成功才替换** |
| `server.py:330-334` | STT server-mode 启动失败 → `_stt=None` text-only | 失败复位防 stale |
| `server.py:269-270` | set_service 补建失败 → 返回当前有效开关 | 失败不改运行时槽位 |
| `input/server.py:136-148` | /health 内省失败 → 占位值（getattr 全兜底） | K8s 探针不应 500 |
| `ws_handler.py:33-48` | 会话回放**逐条**契约化，单条失败丢弃 | M2 加固：从「整段丢失」到「单条丢失」 |
| `ws_handler.py:95-134, 141-144, 161-171, 193` | 建连推送失败 debug/warning；wake_words 失败 → None；monitor 循环 debug 继续；STT 禁用不喂音频 | 握手非关键路径；关闭开关后边界守卫 |
| `api_routes.py:202-228` | provider/model 均缺省 → 回退当前模型（tts None 有显式守卫）；仅 provider 缺省 → custom；base_url/key 缺省 → custom_providers.yaml → env | 测试端点便利性；disabled 态守卫防 AttributeError |
| `api_routes.py:255-292, 383-386, 397-403` | test 超时/异常 → 结构化 ok:False；图片配置缺省 10MB/4 类型；limit 解析失败 → 50 并钳制；归档查询失败 → 结构化错误体 | 端点失败返回结构化结果而非 500 |
| `debug_routes/archive.py:151-236` | 列表空 → 返回内存 session；session_detail 三级降级链（archive → session 快照 → 内存） | debug 面板在 DB 挂掉时仍可用 |
| `token_tracker.py:135-176` | pending 文件导入失败 → 下次启动重试；旧格式行跳过计数；`_persist` Mongo 失败 → 本地 JSONL | token 统计降级到 JSONL（设计内持久化） |
| `task/scheduler.py:313-315, 415-449, 468-499` | worker 队列关闭 → 退出；工具超时/异常 → ToolResult.fail；可重试错误指数退避；checkpoint 失败 → warning | 单工具失败不杀 worker |
| `task/scheduler.py:181-190` | gather 单任务异常 → ToolResult.fail 其余继续 | 并行不拖垮整体 |
| `hitl.py:54-65, 75-85` | request_approval/cancel_approval 不抛异常（`logger.exception` 完整日志） | 通知失败不打断审批主流程（依赖 Mailbox 审计流/前端过期检测外部兜底） |
| `tools/web/plugin.py:135-141` | brave 无 key → 回退 tavily（**静默**，见第三节） | Tavily 默认后端保证搜索可用 |
| `tools/memory/plugin.py:41-42, 106-107` | memory None → 「记忆系统当前不可用」 | LTM 未注入优雅降级 |
| `tools/reminder/plugin.py:63-66` | bus None → warning + 丢弃提醒 | 提醒是内存态重启即失，有 warning |
| `tools/runtime_config/plugin.py` 多处 | psutil ImportError → HAS_PSUTIL=False；各 section 失败 → 跳过该节 | 工具输出容错 |
| `tools/vision/plugin.py:136-141, 291-298` | base64 解码失败 → 跳过缓存仍调 LLM；缓存损坏 → 回源 | 缓存是附加优化 |
| `text/stream_processor.py:162-169` | 未知 META emit → warning + 丢弃 | 注册表驱动容错 |
| `text/tokenizer.py` 大量回落 | 半截 PAUSE 剥离；未闭合 STAGE_CN/ANGLE/CODE/MARKDOWN → 回落 SPEECH | **「可见文本永不丢失」核心不变式**（明确被剥离的格式标记除外） |
| `text/splitter.py:178-190` | 尾句纯空白 → 整体丢弃 | 无语音内容 |

#### 高风险（见第三/四节详述）

- `agent.py:394-396` — `_poll_mailboxes` 外层 `except Exception: pass` **无日志**，且注释「get_nowait 抛异常=队列空」与代码不符（QueueEmpty 已在 L366 处理）——会静默吞掉 `event.data` 访问 / `TaskResultData` 构造 / `bus.post` 等真实错误（已复核）。
- `token_tracker.py:131-132` — `get_session_stats` `except Exception: pass` 后回退内存数据，无日志。
- `tools/web/plugin.py:135-141` — brave 缺 key 静默回退 tavily，配置错误被掩盖（建议 warning）。
- `_lifecycle_services.py:110-112` — TTS create 异常全吞（含 custom 配置错误），配置错误被伪装成「text-only」。
- `character.py:96-99` — L2 回滚后 C6 刷新失败静默 pass（可接受但建议 debug 日志）。
- `_lifecycle_persistence.py:109-110`、`idle_monitor.py:96-97` — 后台循环异常仅 debug 级，线上不可见（建议 warning）。

### 2.7 `frontend/src/`（Vue3 + TS）

#### 合理防御（必要）

| 位置 | 兜底内容 | 为什么兜底 |
|------|---------|-----------|
| `audioStore.ts:38-50` | 无 chunk → return；TTS 关闭 → 清理后跳过；streamBubble 30s TTL 清理 | 空流不入队；关闭后静音；高亮防残留 |
| `connectionStore.ts:9-13, 41-52` | 指数退避重连 `min(1000·2^n, 30000)` ±20% jitter | WS 断线自愈（无最大重试上限，可接受） |
| `connectionStore.ts:102-111` | onmessage JSON.parse 失败 → warn + 丢帧不断连 | 单条坏消息不杀 socket |
| `chatStore.ts:44-59` | delta 无 assistant 消息 → 新建气泡承接 | 流式顺序不保证的容错核心 |
| `chatStore.ts:71-72` | 用户打断 → 循环清 queued | 防气泡永久隐藏的状态残留兜底 |
| `chatStore.ts:279, 303-305` | 历史加载失败 → 保持 hasMore 下次重试 | 无限滚动自愈 |
| `chatStore.ts:348-430` | convertServerMessage 各字段缺省（`Date.parse \|\| Date.now()` 得体） | 消息形状多变防渲染崩溃 |
| `settingsStore.ts:23-34, 82-128` | localStorage 缺省回退；capabilities 播种覆盖；setService 乐观更新 + 回滚；旧 TTS 模式迁移防误关 | 服务端权威 + 乐观闭环 |
| `settingsStore.ts:144-148` | switchModel 未连接 → 仅存本地 | 重连后 capabilities 下发实际模型 |
| `debugStore.ts:27-45` | 轮询失败指数退避到 30s；startPoll 防御性清理 | 后端不可用不停止轮询，错误头部展示非静默 |
| `useWebSocket.ts:65-143` | emoji 映射缺失回退；command 参数链式兜底；service_mode 失败用服务端权威回滚；LLM 恒启用忽略 | 契约漂移向后兼容 |
| `useImageUpload.ts:22-31` | 上传失败 → 用户可见状态消息 | 带反馈非静默 |
| `ChatBubble.vue:20-35` | 未知 content type → 纯文本渲染 | 安全降级 |
| `CodeBlock.vue:12-52` | 未知语言 → highlightAuto；clipboard 不可用 → execCommand 降级 | **复制兜底的正确范本** |
| `TasksTab.vue:25-71` | SSE onerror → 退避重连；每帧 JSON.parse 容错；快照非数组 → [] | 断线自愈 |
| `InputBar.vue:101-145` | 麦克风权限/设备/竞态三场景各自兜底（`audioCtx.close().catch`） | 竞态兜底防御性强 |
| `SettingsPanel.vue:45-74` | 重启/关闭时连接失败视为预期，60s 强制 reload | 服务即将不可达场景 |
| `ToolCard.vue:33-51` | args JSON 解析失败 → 显示原始串 | 非 JSON arguments 兜底 |
| `ImageLightbox.vue:24-40` | `<img @error>` → 「图片加载失败」 | **图片失败兜底正确姿势** |
| 各 Debug 面板 | catch → error 展示 + 重试按钮（非静默） | CRUD 失败可感知 |

#### 高风险（见第三节详述）

- `toolsStore.ts:32-34` + `ToolsPanel.vue:25-31` — **插件重载失败被兜成「重新加载成功」**（兜底反噬，已复核）。
- `toolsStore.ts:16-17` — 拉取失败 → 空列表，被渲染成「暂无工具插件」，掩盖网络故障。
- `audioStore.ts:92-100` — `a.play().catch()` 完全静默，无 warn/提示/重试。
- `ChatMessage.vue:45` — clipboard 被拒静默吞，与 CodeBlock 的 execCommand 降级不一致。

#### 轻微瑕疵

- `settingsStore.ts:23-34, 49-73` — localStorage 无 try/catch（配额/禁用场景）；volume `parseInt` 不防 NaN。
- `chatStore.ts:279` — `!resp.ok` 分支无日志。
- `connectionStore.ts:129-150` + `InputBar.vue:44` — 断线时用户文本静默丢弃，仅 ambient 状态提示。
- `SessionPanel.vue:56-59` / `ArchivePanel.vue:54-56` — 删除循环不校验响应状态。
- `VersionHistory.vue:33-40` — doRollback 无 try/catch。

---

## 三、高风险发现（静默吞错 / 掩盖错误 / 兜底反噬）

按严重度排序。以下均经源码复核（标记 ✓）。

### ⚠️ S1. 永久消息归档静默丢失（最高风险）【已复核 ✓】

- **位置**：`core/memory/session.py:66-69` + `core/storage/message_repo.py:99-100`、`88`
- **现象**：`_archive_done` 归档异常仅 `logger.debug`；`message_repo.append` 在 `_db is None` 时静默 `return`；`health_check` 无重连逻辑（`return self._db is not None`）。
- **后果**：MongoDB 故障期间对话消息**永久、不可见地**不落 `message_archive`（append-only 永久归档）；配合 session deque `maxlen=20` 窗口挤出，消息彻底丢失。且一次启动期故障即整进程失效（无重连），与 session_repo/memory_repo 的重连逻辑不对称。
- **修复建议**：`_archive_done` 与 `append` 降级路径日志提升到 warning；`message_repo.health_check` 补与 session_repo 一致的重连逻辑。

### ⚠️ S2. `_poll_mailboxes` 宽泛静默吞错，注释与代码不符【已复核 ✓】

- **位置**：`core/agent/agent.py:394-396`
- **现象**：外层 `except Exception: pass` 注释称「get_nowait() 抛异常 = 队列空」，但 `asyncio.QueueEmpty` 已在 L366 显式处理。外层实际捕获的是 `event.data` 访问、`TaskResultData(...)` 构造、`bus.post` 等真实错误。
- **后果**：某事件数据损坏时该后台任务 mailbox 轮询被静默杀死，任务永久滞留 `_background_tasks` 且无日志。
- **修复建议**：收窄 except 到 `asyncio.QueueEmpty`（或至少 `logger.warning(..., exc_info=True)`）。

### ⚠️ S3. 插件重载失败被兜成「重新加载成功」（兜底反噬）【已复核 ✓】

- **位置**：`frontend/src/stores/toolsStore.ts:32-34` + `frontend/src/components/panels/ToolsPanel.vue:25-31`
- **现象**：`reloadPlugin` catch 完全静默；`handleReload` 失败后仍 `toolsStore.plugins.find(...)` → 列表未变 → `ok: !!plugin` 恒 true → 显示「重新加载成功」。
- **后果**：插件 reload 失败对用户显示成功，错误信号被反转。
- **修复建议**：`reloadPlugin` 抛出或返回状态，`ToolsPanel` 据此显示失败。

### ⚠️ S4. `_elapsed_seconds` 静默返回 0.0，时间熔断永不触发【已复核 ✓】

- **位置**：`Module/workflow_engine/core/sub_agent/replan.py:106-107`
- **现象**：`created_at` 解析失败静默返回 `0.0`（无日志），等价于 elapsed=0。
- **后果**：若 `created_at` 因 bug/数据损坏畸形，任务**失去时间上限保护**，REPLAN 可无限烧资源直至版本/额度熔断。
- **修复建议**：解析失败至少 `logger.error`；理想是 fail-closed（按已超时处理或直接熔断）。

### ⚠️ S5. `think()` JSON 解析失败无日志，工具 JSON 可能当回复发给用户【已复核 ✓】

- **位置**：`core/adapter/llm_adapter.py:75-82`
- **现象**：`json.loads(response)` 失败 → `pass` → `ThoughtResult(text=response)`，纯静默。
- **后果**：后端本应返回 tool_calls JSON 却因 bug 返回畸形/截断 JSON 时，JSON 字符串被原样当回复文本发给用户（前端显示一堆 JSON），且无 warning。
- **修复建议**：fallback 前记 debug/warning，区分「本就纯文本」与「疑似工具 JSON 解析失败」。

### ⚠️ S6. `_run_resume` 缺 PAUSED keep-alive，任务不可恢复【已复核 ✓】

- **位置**：`Module/workflow_engine/workflow_engine.py:388-397`（对比 submit 路径 `255-257`）
- **现象**：resume 路径 finally 无条件 `_cleanup_task` + `_cleanup_storage`，无 submit 路径的 PAUSED 保活分支。
- **后果**：resumed 任务执行中再次被暂停 → checkpoint 删除 + 槽位释放 → 任务从此不可再 resume。
- **修复建议**：补与 submit 相同的 PAUSED keep-alive 分支。

### ⚠️ S7. `character_version_repo` 重复检测失败 → 整个 repo 降级，掩盖数据完整性问题

- **位置**：`core/storage/character_version_repo.py:53-57, 66-69`
- **现象**：aggregate 失败 → `dups=[]` → 假设无重复 → 建唯一索引失败 → 外层 except → 整个 repo 标记不可用（`_db=None`）。
- **后果**：本应「有重复 → 非唯一索引」的正规路径被跳过，版本历史静默丢失；可恢复的数据完整性问题被伪装成可用性问题。
- **修复建议**：区分 aggregate 网络失败（重试/保守建非唯一索引）与真实重复。

### ⚠️ S8. `toolsStore.loadPlugins` 失败 → 空列表掩盖网络故障

- **位置**：`frontend/src/stores/toolsStore.ts:16-17`
- **后果**：拉取失败被渲染成「暂无可用工具插件」，误导用户；`plugins` 有 loading 态但无 error 态。
- **修复建议**：失败保留上次数据 + 错误标记，或快速失败显示错误。

### ⚠️ S9. consolidator 解析到空 == 成功，缓冲区被清【已复核 ✓】

- **位置**：`core/memory/consolidator.py:116-123, 246-255`
- **现象**：`_extract_json_array` 返回 `[]` 不抛异常 → 走「成功」分支 → `_buffer.clear()`。
- **后果**：LLM 输出不可解析文本时本轮对话的整合机会静默丢失（对话仍在 session/archive，非 LTM 级丢失）。
- **修复建议**：区分「合法空」与「解析失败」（解析失败时以异常/标志让调用方保留缓冲）。

### ⚠️ S10. `audioStore.play().catch()` 完全静默

- **位置**：`frontend/src/stores/audioStore.ts:92-100`
- **后果**：autoplay 被拦/格式错误时语音静默少一句，无 warn/提示/重试。队列推进本身必要，但失败不可诊断。
- **修复建议**：至少 `console.warn` + 状态消息。

### ⚠️ S11. `tool/manager.py:330-331` HITL 审批广播失败静默

- **位置**：`Module/tool/manager.py:330-331`
- **后果**：OutputManager 长期故障时审批弹窗永不弹出且无人知晓，需审批工具等到 60s 超时。
- **修复建议**：至少 `logger.warning`。

### ⚠️ S12. `tools/web` brave 缺 key 静默回退 tavily

- **位置**：`core/tools/web/plugin.py:135-141`
- **后果**：配置 brave 但缺 key 的配置错误被静默掩盖，无声走 tavily。
- **修复建议**：该分支加 `logger.warning`。

### 其他低危静默点

`llm_adapter.py:190-193`（switch_model 同步上下文跳过 close，泄漏旧后端连接池）、`tts_adapter.py:407-423`（synthesize_legacy debug 级吞错）、`embedding/huggingface.py:97-98`（GPU 查询失败 → CPU 无日志）、`embedding/registry.py:93-94` / `milvus.py:200-201`（close 失败静默）、`output/websocket.py:191-193`（close 静默）、`_lifecycle_persistence.py:109-110` / `idle_monitor.py:96-97` / `character.py:96-99`（后台循环/辅助路径 debug 级）、`token_tracker.py:131-132`（get_session_stats 静默回退内存）、`ChatMessage.vue:45`（clipboard 静默）。

---

## 四、过度防御（应改为快速失败）

### O1. `delegation.py:364-374` 自注「不应发生」的分支返回降级结果

- 代码自己声明此分支不可能进入，却返回 `NO_MATCHING_TOOLS` 而非抛异常。若未来 `_validate_output` 逻辑被改，这条防御会掩盖契约破坏，让错误以「无匹配工具」形式出现而非崩溃暴露。
- **建议**：改为 `raise AssertionError`（日志已在，fail-fast 更诚实）。

### O2. `llm/openai_compat.py:233-235` 空 choices → 空串回复

- agent 主循环里 LLM 返回空回复却静默降级为空串，产生一轮「空白回复」，比显式抛错更难排查；调用方（CognitiveLoop）无法感知并重试。
- **建议**：返回特殊哨兵或抛异常让上层决策（与 `registry.py:49-50, 60-61` 的 fail-fast 一致）。

### O3. `tts/minimax_tts.py:312` 缺 `status_code` → 当作成功

- API 契约若改字段名，这里会静默放行无效响应，把问题推迟到解析 audio 时。
- **建议**：缺 status_code 时快速失败。

### O4. `config/_utils.py:18-23` type hints 解析失败 → 跳过类型校验

- 跳过类型校验让错误类型静默通过加载，问题推迟到运行时。warning 易被忽略。
- **建议**：限定具体异常类型（如 NameError）而非 `Exception` 全吞。

### O5. `internal.py:488-491` `NodeResultEvaluation` 三字段默认「成功」

- 所有当前构造点都显式赋值，默认值未实际生效；但「成功默认」是危险方向——未来任何漏传判定的构造路径会**静默以成功继续**（垃圾数据沿 DAG 传播）。
- **建议**：三个字段改必填（去掉默认值），强制显式判定。

### O6. `character_config.py:127-131` L1 Core 文件缺失 → 返回空串

- 路径多级回退本身合理，但末级 `return ""` 让 Agent 以空 L1 core 运行，C1 无核心性格。对正式角色配置应 fail-fast。
- **建议**：正式部署校验 L1 文件存在；至少把 WARN 提到启动阶段醒目级别。

### O7. `loop.py:336-349` `process()` 顶层宽 except 归因错误

- `except Exception` 捕获 `_think_loop` 内**所有**异常（含代码 bug），却统一标注「LLM 调用失败」发给用户——归因错误误导用户（`logger.exception` 保留了 traceback，但前端消息误导）。
- **建议**：收窄 except 范围，或至少区分 LLM 异常与代码 bug 分类标注。

### O8. `evaluator.py:390` `_derive_action` 默认 CONTINUE 是死代码

- 5 个 `ExecutionStatus` 值已被上方分支全覆盖，默认分支不可达；若枚举新增第 6 个状态忘记加分支，会被静默当作成功继续。
- **建议**：改为显式 raise 或至少留 warning。

### 其他建议提级

- `compression.py:116-117` — flush 通用异常 `logger.debug` → 提 warning（与 timeout 同级）。
- `loop.py:808-821` — 空响应兜底补累计计数器，超阈值升 error（防系统性 bug 持续发「走神」回复不升级告警）。
- `_lifecycle_services.py:110-112` — TTS create 异常区分配置错误（fail-fast）与连通性失败（text-only）。
- `message_repo.py:88` — 补重连（同 S1）。
- `execution_loop.py:60-95` — `while True` 加防御性最大迭代保险或显式注释约束。
- `replan.py:89` — docstring「version 7 触发熔断」与实现（version 6 即触发）不符，修文档。

---

## 五、为什么有这些兜底 —— 设计主线

兜底不是随机防御，而是围绕几条**明确不变式**展开的：

1. **用户始终看到回应**（`loop.py` 空响应兜底 + nudge 重试）：LLM 返回 200 但无可见文本时整轮不能静默吞掉。
2. **句子文字不丢**（`tts_adapter` flush_on_cancel / 合成失败文字兜底）：TTS 是增强通道，文字是信息的最低保证。
3. **记忆不丢**（`consolidator` insert-all / buffer 保留；`long_term` dirty 标记；`session` MongoDB→JSONL 双写）：可用性优先于强一致，「容忍重复优于丢失」。
4. **可见文本永不丢失**（`tokenizer` 回落 SPEECH）：流式解析器正确性根基。
5. **可用性优先于崩溃**（DB/服务不可用降级内存/text-only/纯本地）：companion 应用不因单服务故障停摆。
6. **错误回流给 LLM 而非崩溃**（`ToolResult.fail`、工具错误字符串）：ReAct 标准恢复模式。
7. **fail-closed 方向**：workflow 判定空响应 → GOAL_FAILED 而非通过；HITL 超时 → 拒绝；rbac_level 缺省 → MUTATION；constraints clamp 到更严格侧。
8. **向后兼容**：旧文档/旧格式/旧字段缺省（`session_repo`/`token_repo`/`message_adapter`/`message_builder`）。

这几条不变式绝大多数兜底都服务于正确目的，是**必要**的。

---

## 六、改进建议汇总（按优先级）

### 应立即处理（掩盖错误 / 数据丢失 / 安全）

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| 1 | `session.py:66-69` + `message_repo.py:88,99-100` | 归档静默丢失 + 无重连 | 日志提 warning + 补重连 |
| 2 | `agent.py:394-396` | `_poll_mailboxes` 宽 except 静默吞真错误 | 收窄到 QueueEmpty / 加 warning |
| 3 | `replan.py:106-107` | 时间熔断静默失效 | 解析失败按超时 / 至少 error |
| 4 | `workflow_engine.py:388-397` | resume 缺 PAUSED keep-alive | 补保活分支 |
| 5 | `toolsStore.ts:32-34` + `ToolsPanel.vue:29` | 重载失败显示成功 | 失败状态回流 |
| 6 | `llm_adapter.py:75-82` | think() JSON 失败无日志 | 记 warning |
| 7 | `loop.py:623-625` | gather BaseException 静默跳过 | 加 warning |
| 8 | `character_version_repo.py:53-57` | dup 检测失败整体降级 | 区分网络失败 vs 真重复 |
| 9 | `consolidator.py:116-123` | 解析空 == 成功清缓冲 | 区分合法空与解析失败 |
| 10 | `toolsStore.ts:16-17` | 拉取失败渲染成空列表 | 失败保留数据 + 错误标记 |

### 应改为快速失败（过度防御）

`delegation.py:364-374`（raise AssertionError）、`llm/openai_compat.py:233-235`（返回哨兵/抛错）、`minimax_tts.py:312`（缺 status_code fail-fast）、`config/_utils.py:18-23`（收窄异常类型）、`internal.py:488-491`（字段改必填）、`character_config.py:127-131`（正式角色校验 L1 存在）、`loop.py:336-349`（收窄 except / 分类标注）、`evaluator.py:390`（默认 CONTINUE 改 raise）。

### 应提级 / 补指标

- `loop.py:808-821` 空响应兜底补计数器、`compression.py:116-117`、`_lifecycle_services.py:110-112` 区分配置错误、`_lifecycle_persistence.py:109-110`、`idle_monitor.py:96-97` 提 warning、`tools/web/plugin.py:135-141` 补 warning、`tool/manager.py:330-331` 补 warning、`audioStore.ts:92-100` 补 warn、`ChatMessage.vue:45` 复用 CodeBlock 降级。

### 可接受（知悉即可）

- `embedding_provider` 迟到就绪（超时后线程仍加载完，下次生效）——良性。
- `compression.py:223-229` 无 `<new_self>` 仍推进计数——防无限重试与丢变更的权衡。
- `connectionStore` 永久重连无上限——聊天应用可接受。
- `minimax_tts.py:258` voice 未命中回退默认音色——接口设计，但需知悉会掩盖角色名拼错。

---

## 附录：审查方法与复核记录

- 7 个子系统分片：① cognitive+adapter（LLM/TTS）② memory+storage ③ personality+config+schema ④ Module 后端 ⑤ workflow_engine+contract ⑥ agent+input+monitoring+task+tools+hitl+text ⑦ frontend。
- 高风险项逐一读源码实证复核（✓ 标记）：S1/S2/S3/S4/S5/S6/S9 及 O7、`consolidator.py` 缓冲区逻辑、`_poll_mailboxes` 注释与实现差异、`_run_resume` 与 `_run_and_cleanup` 不对称、`toolsStore`+`ToolsPanel` 兜底反噬、`_elapsed_seconds` 0.0 路径。
- 设计意图佐证：`docs/database-storage.md`（Milvus/MongoDB 降级矩阵）、`docs/superpowers/plans/2026-08-02-provider-availability.md`（test 端点缺省回退）、git log（`950f9f3` 空响应守卫、`f532ca8` TTS 禁用兜底、`db48875` 加载点降级保护等为刻意提交）。
