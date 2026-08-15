# 阶段 4 联调前整改执行记录（2026-08-15）

## 批次状态

| 项 | 值 |
|---|---|
| 上游需求 | `docs/review/2026-08-15-阶段4联调前后端整改清单.md`（BE-S4-INT-P0-01/P0-02/P1-01/P2-01） |
| spec | `docs/superpowers/specs/2026-08-15-stage4-联调前整改-design.md`（commit `bd614239`，**单一 spec commit**） |
| plan | `docs/superpowers/plans/2026-08-15-stage4-联调前整改.md`（commit `867d3f9b`，**单一 plan commit**） |
| 执行 | 4 个 subagent（general-purpose）并行执行 Task 1/2/3/4（文件互不重叠），主审查跑全量门禁 |
| feature commits | `36f11449`（Task1 P0-01 单源）、`b17fa478`（Task2 golden 样例）、`7cc352eb`（Task3 smoke）、`0d5fef91`（Task4 todo.md） |
| 收尾 commits | `d3f03d3a`（smoke pyright type: ignore）、`a7b96d56`（文档同步）、`dfedfd51`（final review 补子场景 + 2 LOW） |
| 门禁 | test-quick **3617 passed** / test-normal **3725 passed**（基线 3600/3708）/ ruff All passed / pyright **0 errors** / check-types 全绿 / smoke **24 检查全 PASS 退出 0** |

## 决策点定案（D0-D6）

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批组织 | **A 合并单批** | 4 项同挡阶段 4 联调准入门、共处一份整改清单、共用 §9 完成定义；一次 9 阶段流程成本更低，P0-02 与 P1-01 共享契约/桩基建 |
| D1 P0-01 默认地址归属 | **B 工厂层回填共享常量** | `sensevoice.py` 定义 `DEFAULT_BASE_URL`，构造函数/`STTConfig`/`STTFactory` 三处引用真单源；兜底在 custom 分支后（custom 仍 raise）；一处修复覆盖启动/切换/听写快照/Profile 测试 4 调用点 |
| D2 P0-02 产物组织 | **B 按流程拆独立 JSONL** | 9 类缺帧 + 5 REST 文件归独立 golden，既有 `ws-v2-voice.jsonl` 原样保留；全部进 `_ARTIFACTS` → `--check` 自动漂移门禁 |
| D3 P0-02 REST 样例形态 | **A 裸 wire 响应对象** | 每行即真实响应形状；模型映射靠每文件单模型 + `rest-envelopes.jsonl` 文档化行→模型判别 |
| D4 P1-01 smoke 形态 | **A 独立 Python 脚本** | `scripts/stage4_smoke.py` 进程内 `create_app` + 桩，默认确定性、失败非零退出、`STAGE4_SMOKE_LIVE=1` opt-in |
| D5 P2-01 文档范围 | **A 只修 todo.md 7 行** | 文档债不扩 |
| D6 跨仓边界 | **A 本批只做后端产物** | 前端 checker 消费提升为 error 属 elysia-frontend 另行推进（清单 §4.4）；记录新产物 SHA 供前端消费 |

## Review 记录

- **spec**：code-review 1 轮（general-purpose agent）结论 **yes（附条件）**——3 MED + 4 LOW 全消化：
  **MED-1** `from_config()` 零生产调用（启动走 `server.py:422` `STTFactory.create` + `resolve_model_params` 内置返回 `{}`，与 Profile 同源 bug）→ 改为"4 生产调用点同源、工厂兜底一处修四点"；**MED-2** `environment.lock.yml`「115 pip 包版本+hash」数字错（实为 conda 30 + pip 133 全 `==`、无逐包 hash）；**MED-3** §7.7 grep 与 D5 矛盾（todo.md:31 历史引用）→ 限定表格区。LOW×4（桩 agent 补 session/llm/tts 面、生成器→文件映射、场景 3 背压+TTS 归属、端点级 body base_url 覆盖测试 T8）。
- **plan**：code-review 1 轮结论 **no**（2 HIGH + 3 MED + 2 LOW）——**HIGH-1** `ServerMessage.model_validate` 不可调用（Annotated 类型别名）→ `TypeAdapter(ServerMessage).validate_python`；**HIGH-2** smoke `main()` 缺 `agent.start()`（`state: idle` 由主循环 RUNNING 态发出）→ start 后注入桩 + try/finally stop；**MED-1** `voice.discard` RECORDING 态 raise → 改 stop 后 discard；**MED-2** `voice_session.ack` 首帧 1s 后才发 → 等 ack 再 stop；**MED-3** Task4 grep 自相矛盾（新行标题含「单 Session」）→ grep 改旧事实措辞（spec §7.7 同步）。LOW×2（红测 ImportError 预期、`create_app` 不读 env）。
- **最终 commit review**（Phase 8，1 轮）：general-purpose agent 审 `30334970..HEAD` 4 代码 commit，结论 **no**（1 MED + 2 LOW）——**MED** smoke `_profile_scenarios` docstring 宣称 save-delete/PROFILE_IN_USE 但未实现（spec §5.2 多项子场景缺失）→ 补实现（REST save/delete/409 用 tmp 路径隔离不写真实配置、config.llm.model 标记使用中；WS switch_model 成功 patch 桩/失败 + set_tts_voice 成功/非法）；**LOW-1** 删 `test_sensevoice_base_url.py` 未用 asyncio 导入；**LOW-2** `test_gen_contract_frames.py` 补 P7 漂移拦截测试（patch `_ARTIFACTS` 篡改 → `_check()==1`，不触真实归档）。agent 参数类型 `object→Any`（pyright 属性未知）。

## 执行偏差（已解决）

- **Task 2 测试守卫（agent 发现，plan 缺陷）**：既有 `test_all_generation_frames_have_conversation_id` 与新增 `test_stage4_frames_present_in_goldens` 遍历全部 `_ARTIFACTS` 访问 `frame["type"]`，但 5 个 REST 文件是无 `type` 键的裸 wire 响应 → KeyError。最小修复：两处加 `frame.get("type", "")` / `if "type" in frame` 守卫只统计 WS ServerMessage 帧，语义不变。
- **Task 3 smoke 实现出入（agent 对齐真实行为）**：① `voice_session.ack` 首帧 1s 后才发（`ACK_INTERVAL=1.0`）——start 后等 ack（≤3s）再 stop；② `voice.discard` 须在 `voice.record.stop` 后（RECORDING 态 raise）——改 stop 后 discard；③ `switch_stt` 建真后端 + 1s 连通测试——成功路径 patch `STTFactory.create` 桩；④ TTS voices/preview 经 `_active_tts_profile` 读 `agent._tts`（不经 TTSFactory）——main 直接注入私有槽位。
- **smoke 桩缺 `model` 属性（验收 agent 建议）**：`_SmokeSTTBackend` 无 `model` 属性致 capabilities 广播 traceback（`core/input/capabilities.py:54`）——stub 补 `model` 属性消除（见验收后 commit）。`STT switch failed: custom STT provider requires base_url` 为预期失败路径日志、「Consolidation extraction failed」为 fake LLM 非 JSON 的良性背景噪音。
- **subagent 可靠**：4 个执行 agent 全部按时返回（Task1 ~4.6min / Task2 ~6.9min / Task3 ~15.6min / Task4 ~1.5min），未卡住；后台 git 轮询监控（每 15s，12min 无动作告警）未触发告警。
- **门禁范围**：执行 agent 各自跑定向测试；主审查跑全量 test-quick/test-normal + ruff + pyright + check-types + smoke；验收 agent 独立复跑全部门禁。

## 验收结论（Phase 10，subagent 独立验收）

- **结论：本批可标记「阶段 4 功能已开发/自动化已验证」（清单 §9）；不得标记「阶段 4 已完整联调」**——真实 Electron 联调（修复后后端 SHA + 前端 SHA）未执行；前端 checker 消费后端产物提升 voice/dictation/audio/Profile/TTS 为 error（§4.4 跨仓闭环）属 elysia-frontend 另行推进。
- 8 个维度全通过：门禁复核（test-quick 3617 / test-normal 3725 / ruff / pyright 0 / check-types / smoke 24 检查退出 0）、P0-01（生产代码 `Request URL is missing` 0 命中 + 6 用例 + custom 仍 raise + SSRF 守卫未动）、P0-02（`--check` 退出 0 + 17 golden + TypeAdapter 解析 + 键形状 + P6 无敏感数据 + P7 漂移拦截）、P1-01（spec §5.2 五类场景全覆盖 + `STAGE4_SMOKE_LIVE` opt-in + 非零退出 + pytest 包装）、P2-01（grep 无命中 + 7 行事实准确）、文档/历史（spec `bd614239`/plan `867d3f9b` 单 commit 零丢失 + todo 批次块 + CLAUDE.md/architecture/scripts README 同步）、范围纪律（`frontend_contract/`/`Makefile`/`providers.yaml` 零改动 + §7 暂缓项零混入）、标记判定。
- **计数修正**：验收复跑较主审查 test-quick +1（3616→3617）/test-normal +1（3724→3725）——差异来自最终 commit `dfedfd51` 新增 `test_check_fails_on_artifacts_drift`（P7），本记录已按验收值修正。

## 后续衔接

- 本批后端提交 + 记录产物 SHA → **前端（elysia-frontend）**：消费同一后端契约产物（17 golden + contract.schema.json），将 voice/dictation/audio/Profile/TTS 从 warning/未检查提升为 error，对真实 Adapter 跑解析测试，故意改错字段/帧名证明 `check:contract` 失败（清单 §4.4）；
- 双方 SHA 固定后 → Windows Electron + Ubuntu 后端阶段 4 真实联调（清单 §5.4/§9），报告记录前后端 SHA、运行时版本、三语音模式、Profile/TTS 测试结果、失败诊断；
- 真实 SenseVoice 无 `.env` 地址连通验收（spec §3.5/T10）需本机 SenseVoice 服务运行，属 runbook 手动作业，未在本批 CI 内；
- 阶段 4 标记：前端消费 + 真实联调通过前，只可「已开发/自动化已验证」。
