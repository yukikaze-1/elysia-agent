# 日志系统审查报告 — 2026-08-04

## 审查概览

- **主题**：全仓库日志系统专项审查——基础设施（`core/utils/log_setup.py` + `Module/logger/`）的正确性、配置与文档一致性、调用点使用质量。
- **范围**：核心文件 `core/utils/log_setup.py`（215 行）、`Module/logger/factory.py`（247 行）、`Module/logger/tests/test_logger.py`；全仓库 110 个使用 `logging.getLogger` 的文件（约 520 处 logger 调用点、303 处 except 块）。
- **方法**：3 个并行审查 agent（核心文件深度 bug 审查 / 调用点使用审计 / git 历史 + 既往审查记录）→ 交叉验证 → 逐项读源码实证复核 → 过滤误报。
- **审查基线**：commit `94c0b8f`（工作树仅 .gitignore 修改）。

## 结论摘要

| 判定 | 数量 | 说明 |
|------|------|------|
| 真实问题（核心配置） | 7 | 含 1 处与架构文档矛盾的传播配置、1 处幂等性缺陷、2 处日志关联丢失 |
| 真实问题（调用点） | 9 | 静默吞错 3 处、PII 超通道落盘 1 处、error/warning 缺 exc_info 1 组、级别过低 4 处 |
| 测试卫生 | 1 | 测试污染运行日志目录 |
| 已排除 / 已修复 | 若干 | 见第七节 |

**总体判断**：日志系统**主体设计清晰、分层合理**（库日志隔离、双文件输出、stdout 保留、请求追踪），单次 `setup()` 下无同文件双写、无 f-string 拼接、无 API key 直接打印。核心问题集中在三处：**① `core.memory.long_term` 的 `propagate=False` 使 LTM 的 WARNING/ERROR 在主日志与文档描述均矛盾地不可见；② `setup()` 非幂等（重复调用对 mongo/milvus logger 双写）与 unconfigured 库日志经 `lastResort` 泄漏到 stderr；③ request_id 不传播到 TaskScheduler worker 任务导致工具日志无法按轮次串联**。调用点侧最值得处理的是 delegation 完整对话历史 + 用户画像 DEBUG 落盘（PII）、scheduler 生产路径工具失败零日志、token 持久化失败仅 DEBUG。

---

## 一、日志系统架构现状

```
core/utils/log_setup.py:  setup() 由 app.py:115 在进程启动时调用一次
├── root logger          → handlers.clear()（无 handler），WARNING
├── 库日志（pymongo/pymilvus/httpx/httpcore/openai/urllib3/
│        sentence_transformers/transformers/huggingface_hub/torch/motor）
│                        → _file_logger() 分流到 mongo/milvus/http/embedding/torch.log，propagate=False
│   └── core.memory.long_term（特殊：被当作库日志 → embedding.log DEBUG）
├── 应用日志 core/Module/asyncio logger（setLevel(DEBUG), propagate=True）
│                        → stderr（INFO/--verbose DEBUG）+ app.log（INFO, 10MB×3）
│                          + app-debug.log（DEBUG, 20MB×5），均挂 _RequestIdFilter
├── DB 操作日志（core.storage.* / core.memory.session / token_tracker）
│                        → addHandler(mongo.log DEBUG)
├── VectorDB 操作日志（core.memory.long_term / core.storage.memory_repo）
│                        → addHandler(milvus.log DEBUG)
└── 对话日志 core.conversation → addHandler(conversation.log)，propagate=False

Module/logger/factory.py:  LoggerFactory（plain/JSON 双格式 + 模块差异化级别 + root 兜底）
└── get_caller_logger()   → 单跳 f_back 取调用方模块名（m14 已修复）
    └── 仅 Module/output/manager.py + websocket.py 使用 get_caller_logger；
        LoggerFactory 类本身仅在 __main__.py / 测试中实例化（生产不激活）
```

请求追踪：`_request_id_var`（ContextVar，默认 `"-"`）→ `_RequestIdFilter` 注入 LogRecord → 各 handler Formatter 输出 `[%(request_id)s]`。`set_request_id` 在 `core/agent/agent.py` 事件分发处按轮设置。

---

## 二、核心配置问题（log_setup.py / factory.py）

### 2.1 【中】`core.memory.long_term` 的 `propagate=False` 使 LTM 操作告警在主日志不可见

- **位置**：`core/utils/log_setup.py:94`（`_file_logger("core.memory.long_term", "embedding.log", logging.DEBUG)`）→ `:215`（`propagate = False`）；`:167-169` 随后又加 milvus handler
- **问题**：`_file_logger` 把 `core.memory.long_term` 当库日志隔离（`propagate=False`），随后挂 milvus handler 时**没有重新开启 propagate**。结果 LTM 的日志只进 `embedding.log` + `milvus.log`，**不进 `app.log` / `app-debug.log` / stderr**。
- **与文档矛盾**：`docs/architecture.md:952` 明确写 "core.memory.long_term … propagate=True — 主日志保留副本"；`log/README.md` 设计原则 #2 写 "app.log 中也有副本"。其它 6 个 DB logger（`log_setup.py:151-153`）和 `core.storage.memory_repo` 未被 `_file_logger` 处理，propagate 保持默认 True，行为符合文档——**唯独 LTM 是例外**。
- **触发场景**：`core/memory/long_term.py:108` "从 Milvus 加载失败"、`:193` "Milvus insert failed, falling back to in-memory"、`:508` "Milvus search failed" 等静默降级，全部在主日志和 stderr 中消失，运维 tail app.log 完全看不到 LTM 故障。
- **来源**：代码阅读 + 与两处文档比对 + 历史（`2c407e0` "route library logs to files" 引入，原意是把 LTM embedding DEBUG 噪音隔离出主日志，副作用是连同 WARNING/ERROR 一起隔离）。
- **置信度**：92

### 2.2 【低·潜在】`setup()` 非幂等：重复调用对 mongo/milvus logger 双写

- **位置**：`core/utils/log_setup.py:151-155`（`db_logger.addHandler(_mongo_handler)` 无 `clear()`）、`:167-169`（`milvus_logger.addHandler(_milvus_handler)` 无 `clear()`）。对比 `:134`（core/Module/asyncio 段 `handlers.clear()`）与 `:190`（`_conv_logger.handlers.clear()`）。
- **问题**：同一进程内二次调用 `setup()` 会再挂一个全新 handler，旧的移除不掉 → 每条记录在 mongo.log/milvus.log **写两次**。单次 setup 内无此问题（各写入者在不同 logger 命名空间，无父子重叠，已逐一核对）。
- **触发场景**：生产 `setup_logging` 仅在 `app.py:115` 调用一次（`/restart` 是进程内重跑 server loop，不重调 setup），故当前仅理论风险；测试 / hot-reload 场景可触发。
- **附带**：`handlers.clear()` 只摘引用不调 `close()`。CPython 下 handler 被 GC 时 stream 随引用计数关闭，**不是永久 fd 泄漏**；但旧 handler 未 flush/close 属不严谨。
- **来源**：代码阅读 + 历史 agent F4（同源）。置信度：88（机制），低（当前无触发）

### 2.3 【中】unconfigured 库 logger 的 WARNING+ 经 `logging.lastResort` 泄漏到 stderr

- **位置**：`core/utils/log_setup.py:80`（`root.handlers.clear()` 后 root 无任何 handler）
- **问题**：不在 `_file_logger` 名单里的第三方库（**aiohttp 实际在用、未配置**；另有 websockets / PIL / matplotlib 等）若输出 WARNING+，沿 propagate 链到 root 找不到 handler 时触发 `logging.lastResort` → 直接写 stderr。已 grep 确认 aiohttp 在 `log_setup.py` 与 `core/input/` 均无日志配置。
- **后果**：与「库日志不污染主日志 / 终端干净」设计意图相悖——未列入清单的库告警会从 stderr 漏出，且不带 `[request_id]`、无格式化。
- **置信度**：85（机制确定；影响取决于实际运行的库）

### 2.4 【中】request_id 不传播到 TaskScheduler worker 任务（日志关联丢失）

- **位置**：`core/utils/log_setup.py:21-23`（ContextVar）· `core/task/scheduler.py:82`（worker 在 `scheduler.start()` 时 `asyncio.create_task` 创建）
- **机制**：TaskScheduler 是 **asyncio worker pool**（非线程池）。asyncio task 在**创建时**快照当前 context，而 worker 在启动时就创建——此时 request_id 为默认 `"-"`。每轮的 `set_request_id`（`agent.py:422-424` 等）只改动 `_process_events` 事件任务自身 context，**worker 任务 context 永久停在启动值**。
- **结果**：worker 里执行工具的所有日志（`scheduler.py:347` 任务失败、`:444` 重试、以及 `core/tools/*` 内工具代码日志）request_id 恒为 `"-"`，无法与轮次关联。认知路径正确（`_process_events` 设值后再 `create_task` 正确继承；`asyncio.to_thread` 在 3.9+ 正确拷贝 context）。
- **另一处丢失**：`core/memory/embedding_provider.py:57` 用裸 `loop.run_in_executor(None, ...)` 不拷 context → request_id `"-"`。
- **影响**：纯关联性丢失，无功能破坏。置信度：85

### 2.5 【低·潜在 footgun】`LoggerFactory.get()` 短路：已有 handler 的 logger 静默不应用配置 → ✅ 已随整类删除关闭（2026-09-12）

- **位置**：`Module/logger/factory.py:149-150`（`if logger.handlers: return logger`）
- **问题**：若 logger 已被 `log_setup` 配置过（如名字叫 "core"/"Module"，或与现有 logger 重名），factory 会静默不应用 `module_levels`、`formatter`、`setLevel`。且 factory 新建的 handler 挂 **stdout**（`:214`、`:242`），违反 `log_setup`「stdout 保留给对话输出」不变式；经 propagate 到 "Module" 还会再写一份到 app.log → 重复。
- **附带**：`set_level`（`factory.py:161-172`）遍历 `logger.handlers` 全部 `setLevel`，会改到非本 factory 创建的 handler（如 log_setup 的 stderr/app/app-debug 共享 handler）。
- **生产使用**：已确认 `LoggerFactory` 仅在 `Module/logger/__main__.py:22` 和测试实例化，生产路径不触发。属潜在坑。
- **置信度**：90（机制）、80（当前影响）。历史 agent F5（stdout 冲突）同源
- **✅ 处置（2026-09-12）**：本条建议「修默认值」，复核后判定**该修的是存在性而非默认值**。
  进一步核实：`LoggerFactory` 不是「未被采纳的重复实现」，而是**被 `log_setup` 取代的旧设计**，
  两者在策略层不可调和——本类把应用日志送 **stdout**（`:214`/`:242`），而本仓 stdout 保留给
  对话流（`Module/output/console.py:31` 流式 `print`）；且按 logger 名建文件，110+ 个 logger
  会造出 200+ 个 `logger_*.log`。故**整个 `Module/logger/` 包已删除**，唯一活着的
  `get_caller_logger()` 迁至 `Module/_common/registry_helpers.py`。2.8/3.1 随之关闭。

### 2.6 【低】`_JsonFormatter` 不含 request_id

- **位置**：`Module/logger/factory.py:81-89`
- **问题**：JSON 行只含 time/level/logger/message/exc_info，无 `request_id`（`log_setup` 的明文格式都有 `[%(request_id)s]`）。且 factory 的 handler 从不挂 `_RequestIdFilter`。
- **置信度**：95（缺失确定）；低影响（生产不用 factory）

### 2.7 【低】`log_conversation` 未 setup 时静默 return

- **位置**：`core/utils/log_setup.py:47-48`
- **问题**：未调用 `setup()` 时 `log_conversation()` 直接返回无任何日志。生产路径 `app.py:115` 启动必调 setup，故当前无实际影响；但若未来在未 setup 的上下文直接使用 `CognitiveLoop`（`loop.py:334/754/757` 三个调用点）将静默丢对话日志且不可诊断。建议至少 debug 级提示。
- **置信度**：80（生产无触发）

### 2.8 【低】`LoggerFactory` 默认 `log_dir="log"` 仍是 cwd 相对路径（900253c 修复漏网） → ✅ 已随整类删除关闭（2026-09-12）

- **位置**：`Module/logger/factory.py:111` vs `core/utils/log_setup.py:18`（`LOG_DIR = ProjectPath.LOG.resolve()`）
- **问题**：commit `900253c`「消除 cwd-relative 路径 → ProjectPath」只改了 log_setup，未同步 LoggerFactory 默认值。当前 app 走 log_setup 不实例化 LoggerFactory，影响暂有限（2.9 的 0 字节残留正是此默认值的直接后果）；若未来在非项目 cwd 下实例化，日志会写到错误目录。项目已明确 cwd 相对路径为反模式。
- **置信度**：90

---

## 三、测试卫生

### 3.1 【低】Module/logger 测试污染运行日志目录（15 个 0 字节 `logger_*.log`） → ✅ 已随整包删除关闭（2026-09-12）

- **位置**：`Module/logger/tests/test_logger.py:15-21`（`lf` fixture：`LoggerFactory(setup_root=False)` 使用默认 `log_dir="log"`）
- **问题**：fixture 系测试（TestGet/TestClose/TestCritical/TestConcurrency/TestModuleLevel）调用 `lf.get("X")` 时，`_make_file_handler`（`factory.py:219-236`）执行 `mkdir(exist_ok=True)` 并以 append 模式打开文件 → 立即创建 0 字节 `log/logger_{name}.log`；`factory.close()` 只关 handler 不删文件。已实测确认 `log/` 下 15 个文件与「不传 logdir 的 fixture 测试」逐一精确对应（`logger_{TestModule,TestInfo,SameName,NoDup,CloseTest,CloseSet,test_crit_set,concurrent_test,ExactName,L1.BrainLayer,SensorLayer,RuntimeLevel,IntLevel,LoggerA,LoggerB}.log`）；传 `logdir`（tmp_path）的测试无任何残留。非 app 运行产生（app 走 log_setup，只生成 app.log/conversation.log 等，从不生成 `logger_*.log`）。
- **影响**：`log/` 已被 gitignore（`.gitignore:6`），不进仓库；但污染本地运行日志目录。根因同 2.8（cwd 相对默认 log_dir）。修复方向：fixture 默认指向 tmp_path + 2.8 的默认值改 `ProjectPath.LOG.resolve()`。
- **置信度**：95（实测确认）

---

## 四、调用点问题（静默吞错 / 级别过低 / PII / exc_info 缺失）

### 4.1 【中】DelegationPreparer 完整对话历史 + 用户画像 DEBUG 落盘（PII）

- **位置**：`core/cognitive/delegation.py:168-170`
- **问题**：`logger.debug("DelegationPreparer user prompt:\n%s", user)` 与 `logger.debug("...LLM 响应:\n%s", raw)` 打印完整 prompt。`_build_user_prompt` 内含 `_format_session_messages`（**整个会话历史**，每条至多 2000 字符）与 `user_profile`（用户画像/个人偏好）。这是完整对话原文 + 画像写盘，走的是 `app-debug.log`（20MB×5 轮转文件），**而非设计内的 `log_conversation` 通道**（conversation.log）。
- **触发场景**：每次 `submit_task` 委派（LLM 判定可行并准备上下文时）。
- **严重程度**：中（DEBUG 级、仅 debug 文件，故未判高；但量大且含画像，属超设计通道的 PII 落盘）。置信度：92

### 4.2 【中】生产路径工具失败静默（scheduler 非重试失败零日志）

- **位置**：`core/task/scheduler.py:421-425`（`_execute_tool` 的 `except Exception` → `ToolResult.fail` 直接返回，无任何日志）
- **问题**：生产路径 `agent.py:470/495` 注入的 `dispatch_tool_calls` 走这条链：`_execute_tool` 不 raise（结果包进 `ToolResult.fail`），`_worker_loop` 的 `except Exception`（`scheduler.py:346`）**永远不会为此触发**（只接 raise 的异常）。最终失败仅在前端 `tool_done` 卡片可见，后端日志零记录。对比直接执行路径 `core/cognitive/tools.py:76` 有 `logger.warning`，两条路径行为不一致。
- **触发场景**：任何工具插件抛异常（web 抓取失败、插件 bug、参数错误）。仅重试型错误（`:444`）有 info 日志。
- **严重程度**：中。置信度：85（已读源码复核）

### 4.3 【中】TokenTracker MongoDB 持久化失败仅 DEBUG——静默降级

- **位置**：`core/monitoring/token_tracker.py:172-173`
- **问题**：`_persist()` 中 `except Exception:` 后仅 `logger.debug("MongoDB 不可用，token 记录写入 %s", ...)`。对比同层 `SessionMemory.save()`（`core/memory/session.py:265`）同场景是 `logger.warning`。MongoDB 故障期间 token 统计全部静默落入 pending JSONL，运维零可见。
- **触发场景**：MongoDB 下线时每次 `record()`（每轮 LLM 调用）。
- **严重程度**：中。置信度：90

### 4.4 【中低】WS 连接初始化广播失败降级 DEBUG（含 HITL 审批重播丢失）

- **位置**：`core/input/ws_handler.py:103`（capabilities）、`:114`（mood）、`:133`（**pending HITL 审批重播**）
- **问题**：建连后发送 capabilities / 心情 / 待审批卡片的失败全部 `logger.debug`，而同一连接建立阶段的会话回放失败（`:95`）是 `logger.warning`，级别不一致。特别是 `:133`——前端重连后待审批的 HITL 卡片静默丢失，用户可能永远看不到审批弹窗，默认配置（stderr/app.log INFO）下无任何痕迹。
- **严重程度**：中低。置信度：78

### 4.5 【低中】error/warning 级日志缺 `exc_info`（统一模式，一组）

- **位置**：
  - `core/task/scheduler.py:346-347`（worker 任务失败）
  - `core/cognitive/tools.py:75-76`（工具失败）
  - `core/agent/server.py:130`（switch_llm）、`:182`（switch_stt）、`:231`（switch_tts）——error 级却只记 `str(e)`
  - `core/storage/memory_repo.py:53`、`token_repo.py:48`、`session_repo.py:68`、`message_repo.py:72`、`character_version_repo.py:75`（connect 失败）
- **问题**：均为 error/warning 级却未传 `exc_info=True`，只记 `str(e)` 无 traceback。热切换失败来源多样（配置解析 / 后端创建 / 连通性超时）、worker 崩溃、repo 连接失败（auth/DNS/超时堆栈）都无迹可循。
- **严重程度**：低中。置信度：80-85

### 4.6 【低中】Consolidation 比对失败仅 DEBUG——重复记忆静默堆积

- **位置**：`core/memory/consolidator.py:356-358`
- **问题**：`_compare_and_resolve` 的 `except Exception` 只 `logger.debug("...falling back to insert all", exc_info=True)`。比对 LLM 调用持续失败 → 每次回退全量 insert → 重复记忆在 Milvus 静默堆积。同文件提取失败（`:268`）是 `logger.warning`，级别不一致。
- **置信度**：70

### 4.7 【低中】MemoryReflector 单组审查失败仅 DEBUG

- **位置**：`core/memory/memory_reflector.py:164-165`
- **问题**：`except Exception: logger.debug("MemoryReflector: group review failed", exc_info=True)`——LLM 审查一组记忆失败被静默跳过，该组永远得不到去重；若 LLM 持续失败，每次反射周期全部组被跳过而外观正常。
- **置信度**：65

### 4.8 【低】运行时 embedding 失败仅 DEBUG

- **位置**：`core/memory/embedding_provider.py:90-91`
- **问题**：`embed(text)` 的 `except Exception: logger.debug("Embedding failed", exc_info=True)`。加载不可用是 INFO（可见），但运行时单次嵌入失败（网络抖动等）仅 DEBUG，且后果是 LTM 插入静默只标脏（`long_term.py:194` 的 warning 又不含本轮信息）。嵌入系统间歇性故障在默认配置下完全不可见。
- **置信度**：65

### 4.9 【低·待定】`clear_all` 静默清空失败（Milvus 残留，重启复活）

- **位置**：`core/memory/long_term.py:429-437`
- **问题**：`clear_all()` 的 `except Exception: pass`（temp_repo 路径）——内存降级模式下 Milvus 清空失败被静默吞掉，持久层数据保留，重启后经 `_load_from_milvus` 全部复活；前端显示"已清空"但实际残留。
- **触发**：Milvus 宕机时在 debug 面板执行清空 LTM（需降级模式 + 运维误操作叠加）。置信度：55（边缘，附列）

---

## 五、未报告的良性模式（评估后排除）

| 位置 | 判定 |
|------|------|
| `tts_adapter.py:296/396`、`loop.py:374` 的 `except Exception: pass` | 二级兜底，主错误已在上级 `logger.warning/exc_info` 或 `logger.exception` 记录 |
| `debug/info.py:175/238/247/301/322` | DebugInfo 聚合面板无 logger，诊断工具独立降级设计 |
| `wakeword.py:138`、`embedding_provider.py:47/72` | 高频热路径，日志会刷屏，静默合理 |
| `loop.py:880`、`llm_adapter.py:139` | 异常向上传播（re-raise），堆栈由顶层 `loop.process` 的 `logger.exception` 捕获，未丢 |
| repo `health_check()` 静默 `return False` | designed 契约（调用方据此切换 fallback），非吞错 |
| `loop.py:333` 用户输入以 INFO 落 stderr/app.log（截断 200 字符） | 与 `log_conversation` 重复但截断，可能是有意的观测设计 |

---

## 六、修复建议（按优先级）

| 优先级 | 项 | 建议 |
|--------|----|------|
| P0 | 2.1 LTM propagate=False | 二选一：a) 在 `log_setup.py:167-169` milvus 块对 LTM 重新 `set_logger.propagate = True`（恢复主日志副本，符合文档）；b) 若坚持隔离，则同步修正 `architecture.md:952` 与 `log/README.md` #2 的文档描述 |
| P1 | 4.1 delegation PII 落盘 | `delegation.py:168-170` 的 DEBUG prompt 落盘改为截断（如 `[:500]`）或移除，对话历史走 `log_conversation` 单一通道 |
| P1 | 4.2 scheduler 工具失败静默 | `scheduler.py:421-425` 非重试失败补 `logger.warning`（与 `tools.py:76` 对齐），retryable 判定前先记一次 |
| P1 | 2.3 lastResort stderr 泄漏 | 将 aiohttp 等实际使用的库加入 `_file_logger` 名单，或给 root 挂一个 `propagate=False` 的 WARNING 文件 handler 收口未分类库日志 |
| P1 | 2.4 request_id 与 worker | 在 `scheduler.start()` 的 worker loop 每轮执行前 `set_request_id(task.parent_id or "-")`；`embedding_provider.py:57` 改 `asyncio.to_thread` |
| P2 | 4.3 token 持久化失败 | 提级为 `logger.warning`（对齐 `session.py:265`） |
| P2 | 2.2 setup() 幂等 | mongo/milvus 段补 `logger.handlers.clear()` 或引入「已 setup」守卫标志；顺带统一 `clear()` 前 `close()` |
| ~~P3~~ | ~~2.5 `LoggerFactory.get()` 短路~~ | **✅ 2026-09-12 关闭**：随整类删除 |
| P2 | 4.5 exc_info 缺失组 | scheduler/tools/server 三处 switch_* + 5 个 repo connect 统一补 `exc_info=True` |
| P3 | 4.4 HITL 审批重播 | `ws_handler.py:133` 提级 warning（与 `:95` 对齐） |
| ~~P3~~ | ~~3.1 + 2.8 测试污染 / cwd 相对 log_dir~~ | **✅ 2026-09-12 关闭**：改为删除整个 `Module/logger/` 包（根因消除，非改默认值） |
| P3 | 4.6/4.7/4.8 级别不一致 | consolidator 比对 / memory_reflector 单组 / embedding 运行时失败提级 warning，对齐同文件既有 warning 调用 |

---

## 七、已确认正常 / 已修复项

| 项 | 结论 |
|----|------|
| 07-31 审查 m14 `get_caller_logger` 双跳 bug（原 `factory.py:54`） | **已修复**：commit `a868102` 改单跳 `f_back` + 补测试（`test_returns_logger_with_module_name`）+ docstring 说明；当前代码核实到位 |
| 08-04 兜底审查 `logger/factory.py:200-246`（默认级别 + root WARNING handler） | 判定「合理防御」，非问题 |
| 兜底审查 12 项静默吞错（S1-S12） | 均不在日志系统核心（message_repo/loop/_poll_mailboxes/consolidator 等），修复 commit `55969b2`/`94c0b8f` 未触及 logger 文件 |
| 单次 `setup()` 内无同文件双写 | 各写入者在不同 logger 命名空间，无父子重叠（逐一核对） |
| `ws_handler.py` 全部 except 块 | 均有日志（warning/debug），无裸 `except: pass` |
| 无 f-string 拼接日志 | 520 处调用点零命中（`logger.xxx(f"`) |
| 无 API key / token 直接打印 | 扫描确认 |
| `log/` 目录 gitignore | `.gitignore:6`，测试污染不进仓库 |
| `asyncio` logger 以 DEBUG 挂 app handlers | asyncio 未处理 task 异常（ERROR）进 app.log 属获益；DEBUG 噪音（"Using selector" 等）仅进 app-debug.log，可接受 |

---

## 附录：审查方法与验证

- **Agent 1（核心文件深度审查）**：逐行核对 `log_setup.py` + `factory.py`，交叉验证 `app.py:115` 一次性调用、`_process_events` 事件循环、TaskScheduler 实现、LTM/memory_repo 日志调用、`log/README.md` 与 `architecture.md` 文档意图。
- **Agent 2（调用点审计）**：扫描 110 个 `logging.getLogger` 文件、520 处调用点、303 处 except 块；重点复核 10 个高风险文件 + storage repos + workflow_engine + 工具插件。
- **Agent 3（历史上下文）**：git log（三日志文件仅 8 次提交，最近 3 次为 `073902f`/`900253c`/`a868102`）+ 既往审查文档（07-31、08-04）+ `ls log/` 实测。
- **交叉复核**：对 2.1/2.3/4.2/4.1 及全部行号逐项读源码实证；对测试污染（3.1）做文件清单与 fixture 逐一对照实测。
