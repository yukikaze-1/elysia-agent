# 阶段 6 工具双轨展示帧执行记录（2026-08-19/20）

## 批次状态

| 项 | 值 |
|---|---|
| 上游决策 | `docs/review/2026-08-19-阶段6-工具双轨统筹决策记录.md`（D6-01~08 方案 B + §7 未拍板项） |
| 前端对照 | elysia-frontend 阶段 6 前端批次（F-6 镜像决策记录；契约合并后前端 pull 才见新帧计数） |
| spec | `docs/superpowers/specs/2026-08-19-stage6-tool-双轨展示帧-design.md`（commit `c16e27d9`，**单一 spec commit**） |
| plan | `docs/superpowers/plans/2026-08-19-stage6-tool-双轨展示帧.md`（commit `75e21991`，**单一 plan commit**） |
| 执行 | 8 个 subagent 任务（T1 契约 / T2 输出层 / T3 发射点 / T4 submit_task / T5 desktop ID / T5b scheduler / T6 文档；T2+T5、T3+T5b、T4+T6 两两并行），每任务独立评审均 Approved |
| feature commits | `86fdd5aa`(T1) → `93061c0e`(T2) → `374eabd9`(T5) → `a9e5cdbf`(T5b) → `7c7744ec`(T3) → `88bf2695`(T6) → `6a54fc25`(T4) |
| 门禁 | test-quick **3652 passed**（基线 3631，+21）/ test-normal **3760 passed**（基线 3739，+21）/ ruff All passed / pyright **0 errors** / check-types 全绿（golden `ws-v2-tool.jsonl` **预期更新** D-GL-01 + 错误码单源 41 码） |

## 决策点定案

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批范围 | **A 后端 B-6-01~09 全落地** | 契约→输出→发射→ID→文档→门禁完整链路单批；前端 F-6 契约合并后并行 |
| D-GL-01 golden | **A 预期更新 `ws-v2-tool.jsonl`** | `sourceCallId` 必填使 golden 增字段（**overturn 决策记录 §3.5「零 churn」措辞**）；`--check` 验证一致性 |
| D-§7-1 outputSummary | **2000 字符 + truncated=true** | 记录建议默认；绝不塞完整文件/网页正文 |
| D-§7-2 inputSummary | **`_safe_arg_summary`（key+类型/长度）** | 记录建议默认 |
| D-§7-3 reason | **P0 缺省** | 权限窗用工具说明+输入摘要+数据流向替代 |
| D-§7-4/5 | **前端批决策** | desktop.file.read 大结果路径 + action 兼容帧 = 前端 F-6 项，不阻塞后端批 |
| D-runtime | **`ToolManager.runtime_of` + `ToolAdapter.runtime_for` 轻量反查** | 标准 definitions() 无 runtime；`registry_metadata()` 热路径昂贵，直读 `_plugins[name].runtime` |
| D-status-runtime | **`ToolStatusMessage.runtime` 保持 str** | 记录只收敛 ToolRequestMessage；status 为 v2/Electron 回传，收敛扩大 churn |
| D-截断 | **`_truncate_output_summary` 独立 helper** | v1 content/truncated 与 v2 outputSummary/truncated 语义独立，v1 逐字节不变 |
| **D-ERROR** | **v2 error=dict `{code,message}`，v1 error=str** | `ToolResult.error` 是 str；str 直塞 dict 会 pydantic 校验失败静默丢帧（spec 评审 CRIT-1） |
| **D-v1-suppress** | **`tool_started` 加 `v1_suppress`；submit_task 用 True** | submit_task v1 从未发 tool_executing，加帧破坏 v1 帧集不变（spec 评审 CRIT-2） |
| **D-scheduler** | **Task 5b 补 scheduler call_id 线程化** | Task 5 评审 CONFIRMED 生产 `ctx.call_id` 恒空串（真实 Agent 经 scheduler worker 用共享 ctx）→ `sourceCallId` 恒 `""`，D6-07 不达；补 `new_tool_call(call_id)` + `_execute_tool` 每调用 `clone_for_call` |
| D-新 golden 帧 | **不新增 tool.started/completed golden 帧** | 三层单测覆盖；避免 golden trace 过度重构 |

## Review 记录

- **spec**：code-review 2 轮——r1 **With fixes**（2 Critical + 2 Important + 7 Minor + 3 Rec 全消化）：CRIT-1 `ToolResult.error` str→dict 直塞静默丢帧 → D-ERROR；CRIT-2 submit_task v1 帧集 → D-v1-suppress；IMP-3 registry_metadata 热路径 → runtime_of 直读；IMP-4 `doc["source_call_id"]` KeyError → `.get`；MIN-5~11 + Rec（ServerMessage 行域、overturn 措辞等）。r2 **Yes**（3 Minor 措辞优化消化）。
- **plan**：code-review 2 轮——r1 **With fixes**（1 Critical + 5 Important + 5 Minor 全消化）：CRIT 测试裸名 NameError；IMP-2 gen_contract_frames pyright `Literal`→`ToolRuntime`；IMP-3 测试通道脚手架 + `ev[1][1]`；IMP-4 `_generation_ctx` 类型占位；IMP-5 `request_id or None` roundtrip；IMP-6 marker。r2 **Yes**（3 Minor，Task 4 marker 注记已补）。
- **任务评审**（8 个，均 Approved）：T1 契约（test_ws_message 成员数 39→41 偏离确认为机械必然）；T2 输出层（v1 逐字节比对）；T5 desktop ID（两 concern 均 CONFIRMED：requestId 实为 `msg_...`、**sourceCallId 生产空串缺口**→D-scheduler）；T5b scheduler（D6-07 生产非空实测 `call_llm_desktop`）；T3 发射点（call-site 推迟协调点确认）；T4 submit_task（call-site 已补 + conversationId 断言）；T6 文档（与契约逐字段一致）。
- **最终 whole-branch 评审**（Phase 8，opus）：（待填写）

## 执行偏差（已解决/Ruling）

- **T1 test_ws_message.py 成员数 39→41**（plan 漏列该文件）：加 2 ServerMessage 成员的机械必然，评审确认为正确 fixture 同步。
- **T1 bridge.py pyright 1 error**：plan 分 Task 5 的预期瞬态，T5 落地后归零。
- **T5 requestId 前提错误**：plan 假设 roundtrip trace_id=""，实际 `loop.py:355` 每回合设 `trace_id=new_message_id()` → requestId 为 `msg_...`；断言已正确更新（`startswith("msg_")`）。
- **T5 sourceCallId 设计缺口（D-scheduler）**：真实 Agent 路径 `TaskScheduler._worker_loop` 用共享 `_tool_ctx` 从不设 `call_id`（仅 `_execute_tools_directly` 克隆设，非生产路径）→ 生产 `sourceCallId=""`。补 Task 5b 线程化 `tc.id`：`ToolCallPayload.call_id` + `dispatch_tool_calls` 传 `tc.id` + `_execute_tool` 每调用 `clone_for_call(call_id=...)`（对齐 `_execute_tools_directly` 隔离惯例，5 worker 并发安全）。
- **T3 plan Step 5 call-site 推迟**：plan 把 `_run_submit_tasks` conversation_id 签名放 Task 4，T3 实现者正确推迟并注释；T4 已补（协调要点）。
- **T5 `.get() or ""`**：plan 的 bare `.get` 对必填 str 字段 pyright 报错，`.get() or ""` 保防御语义。
- **T5 插件测试路径**：plan 写 `core/tools/desktop/tests/` 不存在，实际在 `core/tools/tests/test_desktop.py`。

## 验收结论（Phase 10，subagent 独立验收）

（待填写）

## 后续衔接

- **前端 F-6 批**（elysia-frontend）：契约合并后 pull，跑 `check:contract`（WS 计数随新帧增长）；raw/领域批与后端 B-6-02~06 已可并行；D-§7-4（desktop.file.read 大结果路径）、D-§7-5（action 兼容帧保留/移除）在前端批定。
- **阶段 6 真实联调**：后端运行版本含 `tool.started/completed` + `sourceCallId`；前端含 Main 工具服务；桌面四工具真机验证需 Windows Electron（决策记录 §6）。通过前阶段 6 只可标「已开发/自动化已验证」。
- **遗留 Minor**（均 triage safe）：scheduler 空 `tc.id` 无 uuid 兜底（与 `_execute_tools_directly` 分歧，spec 合规）；failure-path 测试未断言 conversationId（happy path 已覆盖）；`error_type_str` 数据驱动 vs 旧硬编码 None（submit_task 恒 None 等价）；bridge `or ""` 缺键与真空串不可分；test_runtime_of 断言具体插件名 `get_time`。
