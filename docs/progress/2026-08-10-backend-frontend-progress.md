# 后端 + 前端适配进度总览（2026-08-10）

> **快照时间**：2026-08-10（文档持续更新，每次重大里程碑修订本文件）。
> **后端基线**：`d1cb3ae`（2026-08-10 全项目评审基线）；当前 `HEAD = 4a6e198`，`d1cb3ae..HEAD` 共 **38 个提交**。
> **前端仓库**：`/home/yomu/elysia-frontend`（Cyrene Electron，独立仓库）；当前 `HEAD = f6f7ef1`。
> **数据来源**：git 提交历史、`docs/review/full-project-review-2026-08-10.md`、`docs/review/remediation-execution-report-2026-08-10.md`、`docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md`、`todo.md`、前端 `docs/16-Cyrene-前端实施任务清单与进度跟踪.md`。

---

## 1. 一句话总览

**后端**：P0 ×2 + P1 ×19 已全部修复清零，本机/受控联调可用；正式发布仍被 F 批次工程化 + D6b/D6c 产品门禁 + Workflow E 项挡住。

**前端适配**（Cyrene Electron）：阶段 1 安全骨架三块全部落地并全量验证；阶段 2 块 A（领域契约 + raw wire 层 + frame mapper）收尾中；两端已具备**首次真实后端联调**条件。

**关键判断**：两端单机联调可开始；远程发布仍受 WS Bearer token 缺口 + 后端产品化门禁约束。

---

## 2. 后端进度

### 2.1 评审基线

2026-08-10 全项目综合审查（`docs/review/full-project-review-2026-08-10.md`）基线 `d1cb3ae`，发现：

| 严重度 | 数量 | 性质 |
|---|---|---|
| P0 | 2 | GC 误删附件、self-hosted runner 读宿主 `.env` |
| P1 | 19 | 持久化假成功、并发竞态、安全边界（沙箱/SSRF/日志脱敏）|
| P2 | 14 | 可靠性、契约、资源、工程化缺口 |
| P3 | 3 | 维护性、文档、门禁覆盖 |
| 已知门禁 | 3 | 远程鉴权 fail-closed、实际限流、跨重启幂等 |

发布判断原文：本机/受控联调**有条件可继续**；正式发布**不通过**（P0/P1 修复 + F 批次工程化 + 门禁齐全前）。

### 2.2 已执行修复：23/41 Task（P0 + P1 全清）

按 `docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md` 执行，每个 Task 均为「失败测试 → 修复 → 回归 → 独立提交」。

| 批次 | 内容 | Review 项 | 状态 |
|---|---|---|---|
| **A 止血** | GC 完整分页 fail-closed；PR CI 与宿主凭据隔离 | P0-01 / P0-02 | ✅ |
| **B 持久化** | 统一存储不可用语义 + 真实健康检查；session durable save/并发 load/pending 恢复；归档任务所有权 + durable 删除；附件 ref 在 accepted ACK 前持久建立；consolidator batch + LTM dirty/tombstone；session 查询/TTL/reflector 持久化 | P1-01~05、P1-11~12、P2-01 | ✅（P2-01 部分）|
| **C 生命周期** | 用户消息排队 + work lease 幂等释放；per-call ToolInvocation + 会话绑定；after-response 绑定会话；安全 shutdown + 可重入 restart；checkpoint 可消费 + proactive 事件闭环 | P1-06~10、P1-13、P2-09 | ✅（P2-09 部分）|
| **D 安全边界** | 文件 containment/symlink/资源上限；SSRF 逐跳校验 + 流式上限；搜索字面化 + WS/REST 输入上限；集中日志脱敏；上传活动内容隔离 + config secret 纪律；非回环 fail-closed 鉴权；Compose 收紧 + embedding 供应链固定 | P1-14~19、P2-02~05、P2-10、门禁 D6a | ✅ |
| **E 其它** | Milvus 输入校验 + metric 持久恢复 | P2-10 | ✅ |
| **F 工程化** | — | — | ⏳ 未做 |

### 2.3 评审外的 follow-up 修复（`beeba57` 起，16 个提交）

外部 review 复验 + 决策落地 + CI 收口：

- **远程鉴权 fail-closed（D1 拍板）**：`beeba57` — 远程绑定仅认业务 token（`AGENT_ACCESS_TOKEN`），仅配 `SERVER_AUTH_TOKEN` 拒绝启动；默认 host 改回环 `127.0.0.1`（`c055de2` 复验补 runtime 断连语义）。
- **会话删除提交点前置（D2 拍板）**：`071fe67` — 先删 Mongo 记录（提交点）再 best-effort 清理，失败写 `session_pending_cleanup` journal 由健康循环重试；`76853ef` 同步补 LTM `pending_syncs` journal + `_write_lock`。
- **接受时容量检查（burst 不再 ACK-then-drop）**：`a4f590a` — `EventBus.pending_user_inputs` 计数 + `process_message` 忙判定。
- **部署工程**：`38bb3e4` `SERVER_HOST` env 覆盖 + README 远程开发说明；`e9de217` 脚本下载失败非零退出；`ed8be0b` Milvus metric 校验前置。
- **CI 修复**：`1681bb9` + `4a6e198` — LTM journal 测试隔离；`ProjectPath` 成员数漂移 → 成员名集合精确断言（根因：批次 7 `MEMORY_PENDING_DELETES` + 本轮两个新增导致魔法数 23→26 漂移，CI test-normal 显性失败）。
- **已知缺口登记**：`14ddb03` — WS Bearer token 缺口入 todo.md。

### 2.4 当前门禁状态（全绿）

| 门禁 | 结果 |
|---|---|
| `make test-quick` | **3051 passed, 2 skipped, 119 deselected**（300s）|
| `make test-normal`（CI 同款）| **3131 passed, 0 failed** |
| `ruff`（`make lint`）| 通过（TID252/ANN）|
| `pyright`（Elysia env）| **0 error**，1 warning（`core/monitoring/__init__.py` IdleMonitor，F4 处理）|
| `python app.py --check` | 13/13 |
| contract / golden frames 漂移 | 通过 |

### 2.5 剩余未做项

#### A. 需要产品决策 / 外部依赖（`todo.md` A 组，不宜擅自拍板）

| 项 | 现状 | 决策点 |
|---|---|---|
| **D6c 跨重启持久化幂等** | `clientRequestId` 幂等表是进程内 dict、TTL 1h，重启丢失——ACK 丢失 + 后端重启后 Electron 重试可能重复生成 | Mongo 离线时 fail-closed 还是内存降级 |
| **D6b 公开入口实际限流** | 契约已登记 `RATE_LIMITED` 但无生产者，远程暴露无应用层抵御 | 单进程内限流 vs 多实例共享 backend |
| **E2 Workflow submit 有界排队** | `submit()` 先等 semaphore，PAUSED 任务占 slot 时第二次 submit 无限等待 | 计划推荐「立即返回 + 后台 QUEUED」 |

#### B. Workflow 独立子系统（工作量大，非 P1）

- **E3** cancel/pause 终态单调性（M19/M20：resume→pause 销毁 task/mailbox；planning 后 cancel 被改回 RUNNING）
- **E4** 测试确定性：全量 pytest 的 2 个失败均为真实 `DEEPSEEK_API_KEY` 外部 E2E 首事件超时
- **E1** 工具热重载原子回滚（reload 后读旧 class、失败无回滚）

#### C. 工程化 / 文档（F 批次，正式 release candidate 前置）

- **F1** 真正全量离线 CI（`test-all` 名不副实）
- **F2** 依赖锁 + 安全扫描（requirements 无 lock/hash；CI 复用宿主 conda env）
- **F3** backend-only setup/start（现强制装 v1 前端）
- **F4** scripts 静态门禁 + 高复杂度入口拆分（scripts 17 ANN；monitoring `__all__`）
- **F5** 文档/测试统计漂移 + Electron 交接（活跃文档 3186 vs 实际 3290+；错误码 40/41）

#### D. 部分完成项（不影响对应 review 项主结论）

- **P2-01**：TTL 真实生效 + reflector 持久化 ✅；session 列表 keyset 分页下推 + 显式过期级联清理 ⬜
- **P2-02**：搜索/上下文字面化 + `/api/chat` 429 ✅；契约层附件业务上限 + 附件 bounded batch ⬜
- **P2-09**：checkpoint 空删 + 版本隔离 ✅；EventBus lifecycle epoch（重放风险低）⬜
- **E6**：desktop golden 参数 `offset/limit` vs runtime 只支持 `path`，`--check` 只查生成器自洽

### 2.6 发布判断

| 场景 | 结论 | 前置 |
|---|---|---|
| 本机 / 测试数据 / 受控联调 | **可用** | 数据备份；不要当作重启可靠性证明 |
| 内部多人试用 / 真实会话附件 | **可用（推荐先故障注入复验）** | P1 已清 |
| 非回环 / 远程生产 | **不推荐** | D6b 限流 + D6c 幂等 + F 批次 |
| 正式发布 | **不通过** | F1-F5 + D6b/D6c + E 项清完 |

---

## 3. 前端适配进度（Cyrene Electron）

### 3.1 后端契约封口对前端的意义（Batch 9A/9B/9C，2026-08-09 完成）

后端 Electron 契约专项 8 项已全部落地（commit `9e96657..d58493d` + 外部 review 修复），为前端提供**稳定、可联调的 wire 契约**：

- **9A 文本/历史**：session 回放必带 `conversationId`；历史分页显式 opaque cursor（`before/after` 只认 cursor）；显式 conversationId 验证存在，禁止幽灵会话懒创建。
- **9B 契约工程化**：版本化机器契约产物（`contract.schema.json` + `contract.raw.ts` + `contract.generated.ts` + golden frames）+ `--check` 漂移门禁；统一入站校验（ClientMessage 判别联合入口）；严格版本协商（`2`→v2、`2.0`→400）。
- **9C 语音**：输入音频固定格式（16kHz/mono/pcm_s16le）+ descriptor 校验 + chunk 上限 1 MiB + 段整帧对齐；dictation 会话内 STT profile 隔离（不改全局活动 STT）。

2026-08-10 又做了接入充分性复审收口：Desktop 精确 connectionId 投递、cancel/fail golden、公开文档同步。**结论：单机真实联调可开始。**

### 3.2 阶段进度总表

```text
阶段 0 资料与协议基线 ──▶ 阶段 1 Electron 安全骨架 ──▶ 阶段 2 核心聊天纵向闭环
                                                            ├─▶ 阶段 3 完整聊天、历史与多媒体
                                                            ├─▶ 阶段 4 模型服务与语音
                                                            └─▶ 阶段 5 Live2D
                             阶段 1/2 ──▶ 阶段 6 工具、权限与 Desktop P0 能力
  阶段 3/4/5/6 ──▶ 阶段 7 安全、稳定性与性能收敛 ──▶ 阶段 8 打包、跨平台与 Demo 验收
```

| 阶段 | 状态 | 备注 |
|---|---|---|
| 0 资料/契约/脚手架 | ✅ 完成 | 仓库、环境、脚手架、契约、Adapter+Mock、四区 UI 壳、单测、CI |
| 1 Electron 安全骨架 | ✅ **三块全落地** | 182 单测 + 18 集成用例 + verify:artifact 冒烟全绿；验收门槛 4 项全勾 |
| 2 块 A（领域契约 + raw wire）| 🚧 **进行中，近收尾** | 最新 4 个 commit 为实现 |
| 2 核心聊天纵向闭环 | ⬜ 未开始 | 块 A 完成后启动 |
| 3 完整聊天/历史/多媒体 | ⬜ 未开始 | — |
| 4 模型服务与语音 | ⬜ 未开始 | — |
| 5 Live2D | ⬜ 未开始 | 缺 SDK/模型资源（G-01/G-02）|
| 6 工具/权限/Desktop P0 | ⬜ 未开始 | 后端 4 个 Desktop 工具已具备真实闭环 |
| 7 安全/稳定性收敛 | ⬜ 未开始 | Demo P0 范围 |
| 8 打包/跨平台/Demo 验收 | ⬜ 未开始 | 缺签名证书（H-04）|

### 3.3 阶段 1 详情（✅ 已完成）

| 块 | 内容 | 验证 |
|---|---|---|
| **块 A** 安全加载与环境基线 | `cyrene://` 受限协议替代 `file://`；严格 CSP（响应头 AND meta、`frame-src 'none'`）；dev/test/prod 环境（`__APP_MODE__` 烘焙）+ 生产安全默认（DevTools 恒关 + 远程调试门）；electron-builder 未分发包 + `verify:artifact`（静态 + 冒烟 16/17 项，负路径验证调试门）| 单测 + verify:artifact |
| **块 B** 主进程受信服务 | 凭据存储（safeStorage 封装 + 版本化 `config/credentials.json` 0600/原子写/每 key 加密块 + `credentialStatus` 4 值 IPC + key 白名单单源）；日志/审计（手写 JSONL logger 无 msg 参数/落盘双净化、append-only 审计按日 + 30 天清理、file-writer rotate 5MB×3）；装配（before-quit flush 防挂死）| 182 单测全绿 + typecheck/lint/build |
| **块 C** IPC/协议安全集成测试 | Playwright `_electron` 集成套件：协议 handler F1（403/404/逃逸/200+CSP）、IPC API 面/超大输入/来源校验、外部链接受控（`window.open`/`location` → `shell.openExternal` + deny）、普通浏览器信任边界（无 preload 时 `desktopApi` undefined）；`test:integration`（`--mode test` 构建）+ CI xvfb-run | 18 集成用例 + CI |

阶段 1 前置决策已拍板（doc16 §4.0）：App 身份 Cyrene/com.cyrene.desktop、沙盒 `userData/sandbox`、开发者模式不进 P0 恒关、凭据 safeStorage 不可用即拒存、日志 5MB×3 / 审计 30 天 / 诊断导出进 P0。

### 3.4 阶段 2 块 A（🚧 进行中）

领域契约修复 + raw wire 层，TDD 6 任务（已 code-review，plan 见前端 `docs/superpowers`）。最新实现 commit：

| commit | 内容 |
|---|---|
| `3fbfcf4` | 领域契约类型扩展——ProtocolWarning / Token 结构 / 新事件 |
| `bbf5909` | `contracts/index` 聚合导出 protocol.ts |
| `84566ea` | 领域 schema 同步新事件 + 领域 fixture 迁移改形状 |
| `df70ae7` | **raw wire 层**——raw-types / raw-schemas（判别 `type` + strict）|
| `f6f7ef1` | **frame mapper**——raw generation 帧 → 领域事件（status 过滤 / fail-closed / 告警）|

**作用**：把后端 raw wire 帧（`type/delta/mood/generationId`）映射为前端领域事件（`event/text/emotion/responseId`），这是阶段 2「核心聊天纵向闭环」的输入基础。块 A 完成且 review 通过后即可进入阶段 2 主任务。

### 3.5 前端待补外部输入（非后端缺口）

| ID | 项 | 性质 |
|---|---|---|
| H-01 | 主验收平台（Windows/Ubuntu 选型）| 产品决策 |
| G-01/G-02 | Live2D SDK 许可 + 模型资源 | 外部资源 |
| H-04 | 签名证书 | 外部资源 |
| B-01 | 测试环境地址 | 外部输入 |

其余 `[ ]` 为前端本地授权/沙盒/审计设计（F-06 等，Electron 职责，后端 security 文档已明确）与产品决策。

### 3.6 前端发布判断

阶段 1 三块全落地 + 后端契约封口完毕 → **阶段 2 可以开始真实后端联调**。但前端尚未走到阶段 7/8，且受后端正式发布门禁（F 批次 + D6b/D6c）约束——**两端都尚不构成正式发布**。

---

## 4. 两端衔接点与联调状态

### 4.1 可联调前提（已具备）

1. 后端 P1 清零，受控联调安全；
2. 后端 9A/9B/9C 契约封口 + golden frames 漂移门禁——前端 Adapter/累加器有权威 wire 契约可对照；
3. 前端阶段 1 安全骨架（受信主进程 + 凭据/日志）就位；
4. 前端阶段 2 块 A 的 raw wire 层 + frame mapper 为聊天闭环铺好输入映射。

### 4.2 已知联调阻塞

- **WS Bearer token 缺口（已记 todo.md）**：业务中间件只认 `Authorization` 头，WS 握手不能设自定义 Header → `AGENT_ACCESS_TOKEN` 模式下浏览器前端 WS 连不上。开发期远程用 `ALLOW_INSECURE_REMOTE=1` 规避；**若 `.env` 同时配 `AGENT_ACCESS_TOKEN` + `ALLOW_INSECURE_REMOTE=1`，bind 守卫被放行但前端 WS 仍连不上**。修法：WS 握手接受 query param token（`/ws?token=...`）+ 前端接线（约一个提交，进入联调/发布阶段时做）。
- **后端产品化门禁**：D6b 限流、D6c 跨重启幂等、E2/E3 Workflow——远程发布前必须。
- **前端 Mock→真实切换**：阶段 2 前 Adapter 主要靠 Mock 推进，标注「已开发、未联调」。

### 4.3 远程开发（开发期）说明

- `SERVER_HOST` env 覆盖绑定 host（默认回环 `127.0.0.1`）；
- 远程直连：`AGENT_ACCESS_TOKEN` + `ALLOW_INSECURE_REMOTE=1`（README 有开发期说明）；
- 正式远程方案：HTTPS/WSS + 双 token + 附件签名（生产文档要求）。

---

## 5. 决策点与下一步

### 5.1 待拍板（后端产品化门禁，`todo.md` A 组）

| 项 | 问题 |
|---|---|
| D6c 幂等 | Mongo 离线时 fail-closed 还是保留内存降级（降级需在 server_info 暴露非 durable 窗口）|
| D6b 限流 | 单进程内限流 vs 多实例共享 backend（计划禁止「假装全局限流」）|
| E2 submit | 计划推荐「立即返回 + 后台有界 QUEUED」；若改选快速 `BUSY/QUEUE_FULL` 须先改公开契约与计划记录 |

### 5.2 建议的推进顺序

**并行轨**：
- 后端：F 批次工程化（F1 CI 矩阵 + F2 依赖锁）可先做（不依赖拍板）→ D6b/D6c 等你拍板后清 → E 批次（Workflow 子系统，工作量大）。
- 前端：阶段 2 块 A 收尾（review + 进度文档 doc16 同步）→ 进入阶段 2「核心聊天纵向闭环」，用真实后端做首次联调。
- 联调衔接：做 WS query-param token 支持（一个提交）以解除远程联调阻塞。

**里程碑**：两端「真实后端联调打通阶段 2 主链路」→ 前端阶段 3/4 → 后端 F 批次 + 门禁 → 两端同时收敛到发布候选。

---

## 6. 参考文档

### 后端（本仓库）
- 评审报告：`docs/review/full-project-review-2026-08-10.md`
- 修复映射：`docs/review/remediation-execution-report-2026-08-10.md`
- 执行计划：`docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md`
- 待办清单：`todo.md`（已知 Bug / Electron 联调 / 全项目修复剩余项）
- 月度归档：`docs/completed/2026-08.md`
- Electron 契约封口审计：`docs/review/electron-v2-backend-followup-fix-report-2026-08-10.md`
- 接入充分性复审：`docs/review/electron-v2-backend-integration-readiness-review-2026-08-10.md`

### 前端（elysia-frontend 仓库）
- 进度跟踪：`docs/16-Cyrene-前端实施任务清单与进度跟踪.md`
- 契约差异与修复清单：`docs/18-Cyrene-Electron-真实后端契约差异与前端修复清单-2026-08-09.md`
- 总体架构：`docs/01-Cyrene-Electron-前端总体架构设计-v0.2.md`
- 实施路线：`docs/10-Cyrene-前端实施路线与任务拆分-v0.2.md`
