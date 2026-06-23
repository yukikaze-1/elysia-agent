# Git 提交历史清理 — 设计文档

**日期**: 2026-06-23
**范围**: 最近 ~200 个提交 (从 `3275406` 到 `8eb5f82`)
**目标**: 200 提交 → ~50-60 提交，按功能/主题合并

---

## 总体策略

- **操作方式**: `git rebase -i HEAD~200`，相邻同类提交用 `fixup`/`squash` 合并
- **安全措施**: 操作前建 `backup/pre-rebase` 分支指向当前 HEAD
- **不合并**: 跨功能领域的提交、间隔 >5 个不相关提交的补丁
- **提交消息**: 保留最重要那条或综合重写，前缀保持 `feat:`/`fix:`/`docs:`/`refactor:`/`test:`/`chore:`

---

## 合并方案（从旧到新，即 rebase 方向）

以下按 git 拓扑顺序排列。每组标题为合并后的提交消息。

### 1. `feat(debug): 任务 tab + SSE DAG 进度 + 路由注册`
- `feat(server): 注册 /debug/api/tasks 和 /debug/api/tasks/stream 路由`
- `feat(debug): debug.html 新增「任务」tab——SSE 实时 DAG 进度`
- `fix(debug): 任务 tab 按钮 data-tab 缺失导致页面空白`
- `docs(debug): 更新状态——后端+前端已实现，待 TaskSystem 接入应用层`
> 4→1

### 2. `feat(task_system): stop()/pause() 实现 + 单元测试 + 测试目录拆分`
- `feat(task_system): stop() 实现 graceful shutdown`
- `feat(task_system): pause() 实现——设置 exec_state.pause_requested`
- `test(task_system): stop() + pause() 单元测试（10 tests）`
- `refactor(tests): 拆分 unit/ 和 integration/ 目录`
> 4→1

### 3. `fix+test(task_system): IDE 类型错误消除 + 集成测试补充 + 清理`
- `test(task_system): 生命周期集成测试——stop after completion + reject after shutdown`
- `fix: 消除 IDE 类型检查报错（生产代码 + 测试真实问题）`
- `test(task_system): 补充 3 条集成测试——pause / 并发 / goal_failed→REPLAN`
- `chore(e2e): 删除 _run_task 未使用的 tight_constraints 参数`
> 4→1

### 4. `docs+fix(task_system): 项目进度总览 + 全量审查修复 + 6个正确性bug`
- `docs(task_system): 项目进度总览——v1 完成 / 未完成 / v2 规划`
- `fix(task_system): 6 个正确性 bug 修复`
- `refactor(orchestrator): _exec_state 初始化为 None（__init__）`
- `docs(task_system): 全量审查——修复 21 处文档与代码不一致`
> 4→1

### 5. `docs(token_tracking): 设计文档 + 实施计划`
- `docs(token_tracking): TaskSystem token 追踪设计文档`
- `plan(token_tracking): TaskSystem token 追踪实施计划`
> 2→1

### 6. `feat(token_tracking): TokenLedger + Planner/Executor/Evaluator 接入 + 查询 API`
- `feat(token_tracking): TokenLedger — write-through token 账本 + 单元测试`
- `feat(token_tracking): TaskSummary 加 token_total 字段`
- `feat(token_tracking): Planner 支持 TokenLedger 注入 + _call_llm 后 record`
- `feat(token_tracking): Executor 支持 TokenLedger 注入 + _match_tool 后 record`
- `feat(token_tracking): Evaluator 支持 TokenLedger 注入 + _call_llm_for_judge 后 record`
> 5→1

### 7. `refactor(token_tracking): 组件 per-task 化 + DelegationPreparer + 查询 API`
- `refactor(task_system): 组件 per-task 化——Planner/Executor/Evaluator/... 改由 SubAgent.run() 内部创建`
- `feat(token_tracking): 实现 get_token_usage / get_token_stats 查询 API`
- `feat(token_tracking): DelegationPreparer.prepare() 独立返回 token_usage`
> 3→1

### 8. `fix+test(token_tracking): 接线 + 测试适配 + 文档同步`
- `test(token_tracking): 适配 per-task 组件重构——更新所有测试`
- `docs(token_tracking): 更新 PROGRESS——token 追踪已完成`
- `fix(token_tracking): list_active_tasks() token_usage 接线——读 TaskState 实时数据`
- `docs(task_system): 同步 architecture.md + api.md——TokenLedger + per-task 重构`
> 4→1

### 9. `feat(web_search): DuckDuckGo → Tavily Search API`
- `feat(web_search): DuckDuckGo → Tavily Search API 作为默认搜索后端`
> 1→1（保持不变）

### 10. `refactor(planner): 使用 infra.llm 替代自建 AsyncOpenAI 客户端`
- `refactor(planner): 使用 infra.llm 替代自建 AsyncOpenAI 客户端`
- `docs: 同步文档——Planner 已改用 infra.llm，不再硬编码 DeepSeek 客户端`
> 2→1

### 11. `fix(token_tracking+executor): COMPLETED token 携带 + 竞态修复 + 测试`
- `test(token_tracking): 集成测试补 token 追踪验证`
- `fix(executor): 修复两个问题——token 竞态 + _match_tool 对汇总节点误选工具`
- `fix(token_tracking): COMPLETED 事件携带 token 数据——解决 asyncio 清理竞态`
> 3→1

### 12. `feat(task_system): execution_mode 字段 + 查询 API`
- `feat(execution_mode): DAGNode 加 execution_mode 字段——Planner 标注，Executor 按标注执行`
- `feat(task_system): 实现查询 API——get_task_status/get_progress/list_agents`
> 2→1

### 13. `fix(tests): 修复 6 个集成测试失败 + 文档同步`
- `fix(tests): 修复 6 个集成测试失败`
- `docs: 同步 PROGRESS——日期/commit/测试计数 + 查询 API + 集成测试修复记录`
> 2→1

### 14. `docs(cancel): spec + plan + schema`
- `docs(spec): TaskSystem cancel() 设计文档`
- `plan(cancel): TaskSystem cancel() 实施计划——7 任务 20 步骤`
- `feat(cancel): schema——ExecutionState 加 cancel_requested，OrchestratorRunStatus 加 CANCELED`
> 3→1

### 15. `feat(cancel): TaskSystem.cancel() 实现——SubAgent + Orchestrator`
- `feat(cancel): 实现 TaskSystem.cancel()——幂等软取消，支持 RUNNING/PAUSED`
- `feat(cancel): Orchestrator 循环顶检测 cancel_requested → save checkpoint → CANCELED`
- `feat(cancel): SubAgent 分发 CANCELED → _handle_cancelled 推送 CANCELLED 事件`
> 3→1

### 16. `test(cancel): 单元测试 + 集成测试`
- `test(cancel): 6 个 cancel() 单元测试`
- `test(cancel): cancel 集成测试——运行时取消 + 验证 CANCELLED 事件`
- `docs(cancel): 同步 PROGRESS + api.md——cancel() 已实现`
> 3→1

### 17. `docs(resume): spec + plan`
- `docs(spec): TaskSystem resume() 设计文档`
- `docs(spec): resume——幂等语义更新 + pause 待修复标注`
- `plan(resume): TaskSystem resume() 实施计划——8 任务 25 步骤`
> 3→1

### 18. `feat(resume): TaskSystem.resume() 完整链路实现`
- `feat(resume): _run_and_cleanup——PAUSED 跳过清理，保留 checkpoint/mailbox/semaphore`
- `feat(resume): SubAgent 保存 _context/_mailbox + resume_from_pause + _execute resume_from 参数`
- `feat(resume): 实现 TaskSystem.resume()——CAS + 幂等 + _run_resume`
- `feat(resume): Orchestrator 启动时 RESUMING→RUNNING 转换`
> 4→1

### 19. `test(resume): 单元测试 + 集成测试（pause→resume 完整链路）`
- `test(resume): 5 个 resume() 单元测试`
- `test(resume): 2 个 resume_from_pause 单元测试`
- `test(resume): pause→resume 集成测试——完整恢复链路`
- `docs(resume): 同步 PROGRESS + api.md——resume() 已实现`
> 4→1

### 20. `test(resume): 覆盖缺口——llm_direct/哨兵/PAUSED/并发隔离/cancel PAUSED`
- `test(resume): 2 个高优先级集成测试——cancel PAUSED + resume 幂等`
- `test(executor): llm_direct 模式 + __llm_direct__ 哨兵路径集成测试`
- `test: 覆盖缺口补全——llm_direct/哨兵/PAUSED可见性/并发隔离`
- `fix: 修复 code review 发现的问题 (#1-#10 高/中优先级)`
> 4→1

### 21. `docs+feat(submit_approval): v1 桥接 spec + 实现`
- `docs(spec): TaskSystem submit_approval() v1 设计——桥接 ToolManager`
- `feat(submit_approval): v1 桥接——agent_id 作为 request_id 委托给 ToolManager.submit_approval()`
> 2→1

### 22. `fix(pause+cancel+planner): SubAgent 层拦截 + 幂等 + Replan 修正`
- `fix(pause): 幂等——移除 RuntimeError/KeyError，所有路径静默成功`
- `fix(planner): Replan prompt 补充 execution_mode 规则——防止 compile/summarize 节点误标 tool_call`
- `fix(cancel): SubAgent 层拦截 cancel_requested——_handle_replan 前 + _execute 循环顶`
- `fix(pause): SubAgent 层拦截 pause_requested——_handle_replan 前 + _execute 循环顶`
- `fix(plan_validator): required 节点 output_as=null 自动填充 output:<node_id>`
- `fix(resume): 清零 checkpoint 的 pause/cancel_requested 标志避免立即重触发`
> 6→1

### 23. `test(e2e): cancel 测试改进 + 新增 llm_direct/cancel/pause_resume`
- `test(e2e): cancel 测试改用 3 节点任务 + 验证未完成节点数`
- `test(e2e): cancel 用 progress 计数验证取消生效`
- `test(e2e): 新增 3 个 e2e 测试——llm_direct/cancel/pause_resume`
- `docs: 同步 PROGRESS——测试计数/最新修复/取消前文档清理`
> 4→1

### 24. `refactor: 拆分 cognitive_loop.py + 修复 IDE 类型错误`
- `refactor: 拆分 cognitive_loop.py + 修复 IDE 类型错误`
> 1→1（保持不变，大型重构独立保留）

### 25. `docs: CognitiveLoop 异步任务支持——spec + plan 修正`
- `docs: CognitiveLoop 异步任务支持设计文档`
- `docs: 根据 review 意见修正设计文档`
- `docs: CognitiveLoop 异步任务支持实施计划`
> 3→1

### 26. `feat(cognitive_loop): 异步任务——FSM + ToolResult + submit_task 拦截 + 唤醒`
- `feat: FSM 新增 IDLE_BACKGROUND_TASK + EventBus 新增 TASK_RESULT`
- `chore: 修正 AgentState 注释（五→状态机）`
- `feat: ToolResult 加 async_task_id + TaskSystem 加 get_task_mailbox`
- `feat: CognitiveLoop submit_task 拦截框架 + setter + function definition`
- `feat: CognitiveLoop 渐进式唤醒——is_task_result 参数 + 唤醒模板`
- `feat: Agent TaskSystem 生命周期 + mailbox 轮询 + TASK_RESULT 处理`
> 6→1

### 27. `fix(cognitive_loop): 异步任务 bug 修复——submit_task/assistant_entry/解析/memory`
- `fix: UnboundLocalError — datetime 模块名被局部导入遮蔽`
- `fix: SessionMemory.messages → recent_messages()`
- `fix: Brain.j2 ToolUsageGuidance 加入 submit_task 使用指引`
- `fix: _handle_submit_task user_profile 为 None 时用默认 UserProfile`
- `fix: submit_task 拦截后 assistant_entry 保留完整的 tool_calls（含 submit_task）`
- `fix: Planner._parse_response output_as=None 时 .strip() 崩溃`
- `fix: assistant_entry 先于 submit_task tool result 追加到 messages`
- `fix: Debug WebUI TaskSystem 路由激活——create_app 接受 task_system 参数`
- `fix: Executor tool matching max_tokens 2048→4096，避免大文件写入时 JSON 截断`
- `fix: COMPLETED 事件 result 字段加入节点产出摘要，改善唤醒通知质量`
> 10→1

### 28. `test(cognitive_loop): submit_task 拦截 + 唤醒模板单元测试 + 杂项`
- `test: submit_task 拦截 + 唤醒模板单元测试`
- `chore: 移除未使用的 MessageObj import`
- `docs: 同步 CognitiveLoop 异步任务支持进度`
- `test: 更新 _finalize 测试断言以匹配新 result 格式`
> 4→1

### 29. `docs: 同步 PROGRESS + architecture——Debug WebUI/e2e/asyncio.create_task`
- `docs: 同步 PROGRESS.md + architecture.md——Debug WebUI 激活、e2e 调试修复、asyncio.create_task 新增点`
- `docs: 更新日期/状态 + orchestrator._handle_completion 注释`
> 2→1

### 30. `fix: IDE 类型检查错误 + code review #11/#13/#14/#16/#39`
- `fix: IDE 类型检查错误——Nullable 访问守卫 + 类型适配`
- `fix: code review #11 #13 #14 #16 #39——打字指示器/句子丢弃/ID碰撞/封装/重复函数`
- `docs: code-review-report 标注已修复项 #11 #13 #14 #16 #39`
> 3→1

### 31. `feat: IsoTimestamp 类型化——统一时间戳格式`
- `feat: IsoTimestamp 类型化——统一时间戳格式 + 消除样板代码`
> 1→1（独立功能）

### 32. `chore: 顶层文件归类 + 测试归位 + gitignore`
- `chore: 移动 core/ 中 3 个测试文件到 core/tests/ 归位`
- `chore: 顶层文件归类 + 清理 17 个空日志残留`
> 2→1

### 33. `refactor(planner): 拆分 planner.py 为 planner/ 包 + sub_agent 适配`
- `refactor: 拆分 planner.py 为 planner/ 包 (planner + replanner + prompts + parser)`
- `refactor: sub_agent 适配 planner 拆分——_planner + _replanner`
- `fix: review findings — 移除 dead NotImplementedError + 更新 Planner→Replanner 注释`
- `test: 适配 planner 拆分——集成测试改用 Replanner 实例调用 replan()`
- `fix: 移除 unused import + 更新 Planner.replan→Replanner.replan 注释`
> 5→1

### 34. `docs+plan(test): 集成测试稳定性改进——spec + plan + conftest + MockWebSearch`
- `docs: 集成测试稳定性改进设计文档`
- `docs: 根据 review 修复设计文档——conftest .env 加载、mock_replan 改用 patch 等`
- `plan: 集成测试稳定性改进实现计划`
- `feat: add MockWebSearchPlugin — deterministic test double for integration tests`
- `test: add conftest.py for integration test .env loading`
> 5→1

### 35. `refactor(test): 集成测试改用共享 helper + 移除重复 .env 加载`
- `test: add shared helpers — build_test_task_system, get_tools_from_manager, mock_replan_response`
- `test: refactor test_e2e_mvp.py to use shared helpers`
- `test: refactor test_task_system_lifecycle.py to use shared helpers`
- `test: remove .env loading from test_executor.py (handled by conftest)`
- `test: remove .env loading from test_delegation_preparer.py (handled by conftest)`
- `fix: review findings — _ensure_env parents path + dead imports`
> 6→1

### 36. `fix(e2e): _GENERAL_RESULTS 修正 + revert 真实 web_search + replan 修复`
- `fix: test_e2e_replan_recovery 使用真实 web_search (use_mocks=False) 以触发 replan`
- `fix: _GENERAL_RESULTS 改为通用研究类内容，避免 Evaluator 误判 goal_failed 触发不必要的 replan`
- `revert: e2e 集成测试恢复使用真实 web_search（use_mocks=False）`
> 3→1

### 37. `refactor: 拆分 schema.py → schema/ 包 (types + protocol + output + internal)`
- `refactor: 拆分 schema.py 为 schema/ 包 (types + protocol + output + internal)`
> 1→1（独立重构）

### 38. `refactor: 提取 query.py + lifecycle.py——TaskSystem 查询/生命周期模块函数`
- `refactor: 提取 query.py——TaskSystem 查询方法变为模块函数 + 薄封装委托`
- `refactor: 提取 lifecycle.py——pause/cancel/submit_approval 变为模块函数 + 薄封装委托`
> 2→1

### 39. `refactor: 拆分 orchestrator.py——提取 execution/finalize + 类型注解补全`
- `fix: review findings — restore missing return-type imports + update lifecycle log messages`
- `refactor: 拆分 orchestrator.py — 提取 execution.py + finalize.py`
- `fix: 补全 orchestrator 拆分后的类型注解 + 更新文档`
- `refactor: 拆分 Orchestrator.run() — 提取 _init_exec/_check_interrupts/_handle_no_ready/_filter_approval/_evaluate_and_dispatch`
- `docs: Orchestrator.run() 添加步骤注释 0-5`
- `refactor: 拆分 apply_evaluation — 按 action 提取 _handle_continue/_skip/_retry/_replan + _push_progress 共用 helper`
> 6→1

### 40. `refactor: 拆分 sub_agent.py → sub_agent/ 包 + task_system 文档更新`
- `refactor: 拆分 sub_agent.py 为 sub_agent/ 包 (planning + execution_loop + result_handlers + replan)`
- `fix: sub_agent/ 包补全类型注解 + None 断言`
- `docs: 全面更新 TaskSystem 文档——修复过时引用和计数`
- `fix: agent.py 5 项 bug 修复`
> 4→1

### 41. `refactor: start()/stop()/_process_events() 拆分为私有子方法`
- `refactor: start() 拆分为 8 个私有子方法`
- `refactor: stop() 拆分为 11 个私有子方法`
- `refactor: _process_events() 拆分为 5 个事件 handler`
> 3→1

### 42. `fix: prompt 引导 submit_task + 唤醒时间上下文 + typo`
- `fix: docstring typo Memor → Memory`
- `fix: prompt 引导 LLM 对复杂任务直接 submit_task + 唤醒消息注入时间上下文`
> 2→1

### 43. `chore: PROGRESS + gitignore 更新`
- `chore: 更新 PROGRESS.md + gitignore 忽略 docs/superpowers/`
- `chore: 清理 execution.py 未使用的 import + 尾部空格`
- `fix: cognitive_loop.py 4 处 except Exception 加 CancelledError 守卫`
> 3→1

### 44. `feat(task_system): pause() 超时确认 + 测试`
- `feat: TaskSystem.pause() 增加超时确认——轮询等待 orchestrator 确认 PAUSED`
- `test: 新增 pause 超时确认 4 个测试用例（当前 FAIL——等待实现）`
> 2→1

### 45. `feat: check_meltdown 新增时间/Token 预算检查 + 测试`
- `test: 新增熔断预算检查 3 个测试用例（当前 FAIL——等待实现）`
- `feat: check_meltdown 新增时间和 token 预算检查`
- `refactor: _check_meltdown 和 _handle_replan 传入时间和 token 预算参数`
> 3→1

### 46. `feat: agent.py 注入 ToolManager/TokenTracker + plugin_list safety 扩展`
- `feat: plugin_list safety 子字典扩展 10 个字段（文件系统/网络/数据库）`
- `feat: agent.py 注入 ToolManager/TokenTracker/TaskSystem/启动时间到 ToolContext，修复 tool_names 时机`
> 2→1

### 47. `feat: get_runtime_config 重写——query 过滤 + 7 个 section + 测试`
- `feat: get_runtime_config 重写——支持 query 过滤 + 7 个 section 构建函数`
- `test: get_runtime_config 新增 7 个测试（query 路由 + 降级 + section 覆盖）`
> 2→1

### 48. `fix: 前端 bug 修复——Critical/High/Medium + 工具 bug + 文档修正`
- `fix: 修复 5 个工具 bug`
- `docs: 修复 5 个文档与源码不一致`
- `fix: 修复 8 个 Critical/High bug`
- `fix: 修复 7 个 Medium bug`
- `fix: 修复 4 个 High bug`
- `fix: 修复 14 个前端 bug`
- `fix: chat.js 重新导出 esc 修复 tools.js/persistence.js 导入断裂`
> 7→1

### 49. `fix: TTS/STT 连通性检查——/health 端点 + 日志降级 + debug 状态`
- `fix: TTS/STT 连接失败日志降级——ERROR→DEBUG，避免误导用户以为崩溃`
- `fix: TTS/STT 启动时测试连通性，不可用时正确报告未连接`
- `fix: debug 页面 System 新增 STT 状态显示`
> 3→1

### 50. `fix: 启动横幅——合并重复 + logger.info + 去除 stdout 双写`
- `fix: 合并重复启动横幅——start() 不再打印，由 run_server/run_repl 各自主控`
- `fix: 启动横幅改用 logger.info + print(flush=True)，修复 buffering 导致日志时序错误`
- `fix: 去掉 print() 避免 stdout+stderr 双写导致 tee 捕获重复`
> 3→1

### 51. `docs: TaskSystem debug DAG 图可视化——spec + plan`
- `docs: TaskSystem debug DAG 图可视化 spec`
- `docs: fix 4 design issues (node_id mapping, edges cache, SSE leak, freeze no-op)`
- `docs: fix setData使dagInstances.nodes引用变陈旧 + 类型注释补edges`
- `docs: TaskSystem debug DAG 图可视化实施计划`
> 4→1

### 52. `feat(debug): DAG 替换为 vis-network 交互式有向图 + 修复 5 个 Minor 问题`
- `feat(debug): 引入 vis-network CDN + STATUS_COLORS/DAG_OPTIONS/initDAG`
- `feat(debug): TaskSystem DAG 替换为 vis-network 交互式有向图`
- `fix(debug): 修复 5 个 Minor 问题 (CSS残留/network.destroy/重复映射/title丢失/空数组guard)`
> 3→1

### 53. `fix+chore: read_dirs 相对路径解析 + .gitignore + code review 报告重命名`
- `fix: read_dirs 相对路径解析缺少 session_id 导致读文件找不到`
- `chore: 更新 .gitignore + 重命名 code review 报告`
> 2→1

### 54. `config: session_max_messages 20→200`
- `config: session_max_messages 20→200，测试时可见完整对话历史`
> 1→1（配置变更独立保留）

### 55. `docs: 前端接入 API 文档——WS 协议 + REST 端点 + .NET 指南`
- `docs: 前端接入 API 文档——WS 协议 + REST 端点 + .NET 接入指南`
- `docs: 补全前端 API 文档——session消息完整格式/音频格式/二进制帧/图片约束/Provider`
> 2→1

### 56. `docs: 更新 architecture + database-storage + 删除过期文档`
- `docs: 删除过期文档 (ref_memory-architecture + test_tool_parallel)`
- `docs: 更新 architecture.md + database-storage.md 过时信息`
> 2→1

### 57. `fix: STT/TTS test() 改用 GET /health 替代 GET /`
- `fix: STT/TTS test() 404 误判为不可用——status_code<400→<500`
- `fix: STT/TTS test() 改用 GET /health 替代 GET /`
> 2→1

### 58. `docs: frontend-api 补全——分句策略/流式TTS/工具执行/音频格式/音字同步`
- `docs: frontend-api 补充分句策略和流式 TTS 同步`
- `docs: frontend-api 补全工具执行状态和结构化块`
- `docs: fix 3 frontend-api.md 错误`
- `docs: frontend-api 补全音频流式播放和音字同步实现细节`
- `docs: 澄清音频块格式——仅首块含WAV头，后续为裸PCM续流`
> 5→1

### 59. `chore: 归档已完成任务 + 清理 todo.md`
- `chore: 归档已完成任务到 completed.md，清理 todo.md`
> 1→1（保持不变）

### 60. `docs: Brain.j2 prompt 修复——spec + plan`
- `docs: Brain.j2 prompt 修复 spec——Identity/情绪Protocol/时间戳`
- `docs: fix spec——补 fallback 双重'你是'修复`
- `docs: Brain.j2 prompt 修复实施计划`
- `docs: fix plan——Brain.j2 行号 252-254 为未改动的 Output Format`
> 4→1

### 61. `fix: Brain.j2 prompt 修复——Identity/情绪Protocol/时间戳/Perception/Interaction + 测试`
- `fix: Brain.j2 prompt——Identity/情绪Protocol/时间戳/Perception`
- `chore: 划掉 todo.md 中已修复的 4 条 Brain.j2 问题`
- `fix(test): BrainSystemPrompt 测试补缺失的 name/user_name/user_preferences 参数`
- `fix: Brain.j2 Interaction——禁止第三人称叙述用户，强制直接对话`
> 4→1

---

## 合并统计

| 指标 | 值 |
|------|-----|
| 原始提交数 | 200 |
| 合并后提交数 | **61** |
| 减少比例 | 69.5% |
| 平均每组合并 | 3.3 → 1 |

---

## 验证

0. `git status --porcelain` 确认工作区干净（无脏文件被 rebase 误吞）
1. `git log --oneline` 确认提交数为 61 + (前 524 个未改动提交)
2. `git diff backup/pre-rebase..main` 应无差异（最终树内容完全一致）
3. 运行 `python -m pytest Module/task_system/tests/unit/ -v` 确认测试通过
4. 检查 `git log --oneline --stat` 确认每个合并提交的文件变更合理

---

## 注意事项

- 操作不可逆，`backup/pre-rebase` 分支是唯一的回滚手段
- 合并后的提交失去原始 SHA，如需引用请在操作前记录
- 此方案可随时调整——rebase 过程中可以修改分组
