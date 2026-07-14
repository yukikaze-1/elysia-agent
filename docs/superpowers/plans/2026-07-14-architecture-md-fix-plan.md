# architecture.md 全面修复 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `docs/architecture.md` 的 50 个确认问题（11 critical + 26 important + 13 minor），并添加导航连接（面包屑 + 相关文档）。

**Architecture:** 单文件编辑（~1049 行 → ~1100 行）。9 个 task，严格按文件从上到下顺序执行——前面的编辑会导致后面行号偏移，所有定位必须以内容匹配为准，不能机械按行号。

**Tech Stack:** 纯 Markdown 编辑。无代码变更，无测试。

## Global Constraints

- 13 个现有章节标题、顺序、编号不变
- 现有 ASCII 架构图、数据流图保留不动
- 只修错补漏，不重写
- 能指向 testing.md / readme-conventions.md 的就不复制内容
- 编辑顺序严格从上往下，以内容匹配定位，不靠行号

---

### Task 1: 导航连接 + 第 1 节（FSM 状态数）

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Produces: 面包屑 + FSM 状态数修正。后续所有 task 依赖此 task 先完成（行号偏移）。

- [ ] **Step 1: 标题后加面包屑**

在 `# Elysia Agent 架构与设计` 标题后、`## 1. 架构总览` 前插入：

```markdown
> 文档导航：[项目文档](../README.md)
```

- [ ] **Step 2: 修正 FSM 状态数（第 1 节）**

搜索 `(5 states)`，替换为 `(6 states)`。

搜索 `AgentFSM   (5 states)`（在 ASCII FSM 图中），在 FSM 图中加入 `IDLE_BACKGROUND_TASK` 状态。找到 FSM 图（约第 36 行附近），在 `IDLE` 行下方添加：

```
                         ┌──────────────────────────────┐
                         │    IDLE_BACKGROUND_TASK       │
                         │  TaskSystem mailbox polling   │
                         └──────────────┬───────────────┘
                                ▲       │
```

并将状态数标注从 5 改为 6。

- [ ] **Step 3: Visual check**

```bash
head -80 docs/architecture.md  # 确认面包屑位置 + FSM 图
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md 加面包屑 + FSM 5→6 states"
```

---

### Task 2: 第 3 节 — L0 后端表补全（gemini, deepgram）

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Task 1 面包屑已插入（后续搜索需注意 +1 行偏移）

- [ ] **Step 1: LLM 后端表加 gemini**

搜索 `| \`Module.llm\` | \`deepseek\`, \`openai\`, \`ollama\`, \`mimo\`, \`custom\` |`，替换为：

```
| `Module.llm` | `deepseek`, `openai`, `ollama`, `mimo`, `gemini*`, `custom` | OpenAI-compatible | `stream_think()` 结构化流式 (content + tool_calls 并行检测) |

> \* gemini 已注册但抛出 NotImplementedError
```

- [ ] **Step 2: STT 后端表标注 openai/deepgram stub**

搜索 `| \`Module.stt\` | \`sensevoice\`, \`custom\` |`，替换为：

```
| `Module.stt` | `sensevoice`, `custom` | HTTP API | SenseVoice: localhost:20042，返回文本 + 情绪标签 + 音频事件；Custom: OpenAI-compatible。`openai`/`deepgram` 已注册但抛出 NotImplementedError |
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md L0 后端表补全 gemini + deepgram stub"
```

---

### Task 3: 第 4 节 — L1 认知控制层修正

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-2 编辑已完成

- [ ] **Step 1: CognitiveLoop 行数**

搜索 `(~400 行)`（在 CognitiveLoop 描述附近），替换为 `(~1,040 行)`。

- [ ] **Step 2: Spec 链接归档路径 × 2**

搜索 `docs/superpowers/specs/2026-06-24-inner-voice-design.md`，替换为 `docs/superpowers/specs/archive/2026-06-24-inner-voice-design.md`。

搜索 `docs/superpowers/specs/2026-07-04-prompt-section-provider-design.md`，替换为 `docs/superpowers/specs/archive/2026-07-04-prompt-section-provider-design.md`。

- [ ] **Step 3: LLMAdapter 行数**

搜索 `(~90 行)`（在 LLMAdapter 描述附近），替换为 `(~170 行)`。

- [ ] **Step 4: fake_llm 路径**

搜索 `core/_fake_llm.py`，替换为 `core/tests/fake_llm.py`。

- [ ] **Step 5: Section 4.2 FSM 图 + 转移表更新**

Task 1 已在 Section 1 将状态数从 5 改为 6。Section 4.2（`### 4.2 有限状态机 (AgentFSM)`）的详细 FSM 图和转移表也需要同步更新：

**FSM ASCII 图**：搜索 `### 4.2 有限状态机`，在 FSM 图中加入 `IDLE_BACKGROUND_TASK`：

在 `IDLE` 节点旁边添加 `IDLE_BACKGROUND_TASK` 节点（从 IDLE 进入，可转移到 THINKING 或回到 IDLE）：

```
  ┌──────┐   USER_INPUT   ┌──────────┐      tool_calls   ┌──────────┐
  │ IDLE │ ─────────────► │ THINKING │ ◄───────────────► │EXECUTING │
  │      │ ◄───────────── │          │                    │          │
  └──┬───┘    INTERRUPT   └────┬─────┘                    └──────────┘
     │                         │
     │  TASK_RESULT       LLM done
     │                         │
  ┌──▼──────────────────┐  ┌───▼─────┐
  │ IDLE_BACKGROUND_TASK│  │ SPEAKING │
  │ (mailbox polling)   │  │          │
  └─────────────────────┘  └─────────┘
```

**转移表**：在 `SPEAKING → IDLE | EXECUTING | THINKING | INTERRUPTED` 后添加：

```
IDLE_BACKGROUND_TASK → IDLE | THINKING | INTERRUPTED
```

- [ ] **Step 6: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md L1 行数 + 断链 + 路径修正"
```

---

### Task 4: 第 5 节 — 工具表重写 + session_max_messages

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-3

- [ ] **Step 1: session_max_messages 默认值注释**

搜索 `默认 50 (session_max_messages)`，替换为 `代码默认 20，config.yaml 覆盖为 50`。

- [ ] **Step 2: 完整替换工具表**

搜索 `已注册工具（12 个）`，将下面的整个工具表替换为：

```markdown
已注册工具（12 个）：
| 工具 | 参数 | 功能 |
|------|------|------|
| `search_memory` | query, limit | 语义搜索长期记忆 |
| `save_memory` | content, type | 将重要信息保存到长期记忆库（type: fact/plan/preference/note） |
| `get_time` | timezone, format | 获取当前日期时间，支持时区和格式 |
| `web_search` | query, limit | Tavily / Brave 搜索（默认 Tavily） |
| `web_fetch` | url | 抓取网页内容 |
| `read_file` | path, start_line, max_lines | 读取本地文件（沙箱内） |
| `list_directory` | path, depth | 列出目录内容（depth 默认 1，最大 3） |
| `write_file` | path, content | 写入文件（需审批） |
| `set_reminder` | message, delay_seconds | 设置定时提醒（1-86400 秒） |
| `get_runtime_config` | query | 查询运行时配置和状态 |
| `get_self_state` | — | 查询 Agent 自身状态 |
| `describe_image` | prompt, image_index, image_indices | 视觉模型描述图片（单图/批量互斥） |
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md 工具表重写 + session_max_messages 修正"
```

---

### Task 5: 第 6-7 节 — WS 协议表 + 路由表 + 配置示例

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-4

- [ ] **Step 1: Server→Browser 消息表补全**

搜索 `**Server → Browser:**`，在 `command` 的 `block` 行之后、`**Browser → Server:**` 之前，插入以下 5 行：

```markdown
| `session` | `{"type":"session","messages":[...],"token_stats":{...}}` | 连接时推送会话历史 |
| `capabilities` | `{"type":"capabilities","stt":true,...}` | 服务能力声明 |
| `wake` | `{"type":"wake"}` | 唤醒词检测通知 |
| `switch_model_result` | `{"type":"switch_model_result","service":"...","ok":true}` | 模型切换结果 |
| `error` | `{"type":"error","message":"..."}` | 错误通知 |
```

- [ ] **Step 2: Browser→Server 消息表补全**

搜索 `**Browser → Server:**`，在 `binary` 行之前插入：

```markdown
| `wake_toggle` | `{"type":"wake_toggle","enabled":true}` | 唤醒词开关 |
| `switch_model` | `{"type":"switch_model","service":"llm","model":"..."}` | 切换模型 |
| `tool_approval` | `{"type":"tool_approval","request_id":"...","approved":true}` | 工具审批回复 |
```

并将 `text` 行的说明从 `文本输入` 改为 `文本输入（content 可为 string 或 content array）`。

- [ ] **Step 3: 路由表重写**

搜索 `aiohttp Application (30+ 路由):`，将路由表替换为：

```markdown
aiohttp Application (41 条路由):

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | static/index.html |
| GET | `/debug` | static/debug.html |
| GET | `/ws` | WebSocket handler |
| GET | `/health`, `/ready` | 健康检查 |
| GET\|POST | `/shutdown` | 关闭服务 |
| POST | `/restart` | 重启服务 |
| GET | `/static/` | 静态文件（JS/CSS/HTML） |
| GET | `/live2d/` | Live2D 角色模型资源 |
| GET | `/uploads/` | 用户上传文件 |
| POST | `/api/chat` | JSON chat API |
| GET | `/api/messages` | 消息归档分页查询 |
| POST | `/api/images` | 图片上传 |
| GET | `/api/providers` | 模型 provider 列表 |
| POST | `/api/providers` | 添加自定义 provider |
| DELETE | `/api/providers/{service}/{name}` | 删除自定义 provider |
| POST | `/api/providers/test` | 连通性测试 |
| GET | `/tools` | 工具插件列表 |
| POST | `/tools/reload` | 热重载工具插件 |
| GET | `/debug/api` | DebugInfo 完整 JSON |
| GET | `/debug/api/prompts` | Prompt 记录列表 |
| GET | `/debug/api/prompts/{n}` | Prompt 详情 |
| GET | `/debug/api/responses` | Response 记录列表 |
| GET | `/debug/api/responses/{n}` | Response 详情 |
| GET | `/debug/api/db-status` | 数据库连接状态 |
| GET\|POST | `/debug/api/ltm` | LTM 列表 / 新增记忆 |
| PUT\|DELETE | `/debug/api/ltm/{entry_id}` | 更新 / 删除单条记忆 |
| DELETE | `/debug/api/ltm` | 清空全部 LTM |
| GET\|DELETE | `/debug/api/sessions` | Session 列表 / 清空全部 |
| GET\|DELETE | `/debug/api/sessions/{session_id}` | Session 详情 / 删除 |
| GET\|DELETE | `/debug/api/archive` | Archive 分页 / 清空全部 |
| PUT\|DELETE | `/debug/api/archive/{msg_id}` | 更新 / 删除单条归档 |
| GET | `/debug/api/tasks` | TaskSystem 快照 |
| GET | `/debug/api/tasks/stream` | TaskSystem SSE 事件流 |
```

- [ ] **Step 4: 配置优先级修正**

搜索 `优先级: \`CLI flag > config.yaml > AppConfig 默认值\``，替换为 `优先级: \`CLI flag > 环境变量 (.env) > config.yaml > AppConfig 默认值\``。

- [ ] **Step 5: 角色配置示例补全**

搜索 `wake_words: [Elysia, 爱莉, 爱莉希雅, 妖精小姐]`，替换为：

```yaml
appearance:
  height: "162cm"
  hair: "粉色长发"
  eyes: "粉红色"

wake_words:
  - Elysia
  - 爱莉
  - 爱莉希雅
  - 妖精小姐
```

- [ ] **Step 6: 删除重复无效的 time_of_day 行**

搜索空的 `- type: time_of_day`（无 `times` 字段的那一行，在 `---` 分隔线之前），删除该行。

- [ ] **Step 7: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md WS协议表 + 路由表 + 配置示例修正"
```

---

### Task 6: 第 9 节 — Agent start()/stop() 伪代码补全

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-5

- [ ] **Step 1: 修正 start() 中的 STT 位置**

搜索 `TTS/STT 条件启动`，替换为 `TTS 条件启动（STT 在 run_server() 中初始化）`。

- [ ] **Step 2: 补全 start() 缺少的步骤**

搜索 start() 伪代码块（约在 `Agent.start()` 附近）。在 `# MongoDB + Milvus 并行连接` 之前插入：

```
# AgentFSM.reset() + LLMAdapter.reconnect()
# Archive.connect()
# 状态恢复: CharacterReflector.restore_state → PersonalityEngine.restore_state → EmotionEngine.restore_state → MemoryReflector.restore_state
# JSONL snapshot 恢复
# ToolContext 构造 + 注入
# _db_health_loop 创建
```

- [ ] **Step 3: 修正 stop() — 补全 buffer flush + 修正顺序**

搜索 stop() 伪代码块（约在 `Agent.stop()` 附近）。将整个 stop() 伪代码替换为：

```python
async def stop():
    # 1. Buffer flush: consolidator → CharacterReflector → L3→L2 compression → MemoryReflector
    # 2. _stop_event_task() — cancel event processing
    # 3. cancel cognitive + TTS tasks
    # 4. _stop_mailbox() — cancel mailbox polling
    # 5. _stop_task_system() — stop TaskSystem
    # 6. idle_monitor.stop()
    # 7. scheduler.stop()
    # 8. tool_manager.shutdown()
    # 9. _stop_embedding_if_running() — cancel embedding preload
    # 10. _stop_persistence() — session.save() → EmbeddingFactory.shutdown() → long_term.close()
    # 11. _stop_db_health() — cancel db_health_task
    # 12. llm.close()
    # 13. tts.close()
    # 14. output.close()
    # (每步 3s timeout，少数关键步骤 5s，单步失败不阻塞后续)
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md Agent start/stop 伪代码补全 + 顺序修正"
```

---

### Task 7: 第 11 节 — 测试架构替换为指针

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-6

- [ ] **Step 1: 替换整个测试节**

搜索 `## 11. 测试架构`。将该节从标题到下一个 `##` 之前的全部内容替换为：

```markdown
## 11. 测试架构

~1,558 个测试按 4 层 markers 组织（unit / service / integration / slow）。

完整分层体系、测试文件索引、按模块运行命令 → [docs/testing.md](testing.md)
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md 测试节替换为 testing.md 指针"
```

---

### Task 8: 第 12 节 — 文件结构树修正

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-7

- [ ] **Step 1: 顶层加缺失文件**

搜索 `Agent/` 项目结构树的 `├── app.py`，在其上方（`Agent/` 根下）插入：

```
├── Makefile                # 测试/构建命令
├── requirements.txt        # Python 依赖
├── docker-compose.yml      # 数据库容器
├── pytest.ini              # pytest 配置（markers）
├── data/                   # 应用数据（上传、缓存、Live2D）
```

- [ ] **Step 2: core/ 加缺失文件**

在 core/ 树中搜索 `├── message_id.py`，在其下方插入：

```
│   ├── consolidator.py     #   记忆沉淀
│   ├── schema.py           #   共享类型
│   ├── task.py             #   任务模型
```

- [ ] **Step 3: 修正 prompt/test_prompt.py 路径**

搜索 `prompt/test_prompt.py`，替换为 `system_prompt/test_prompt.py`。

- [ ] **Step 4: cyrene/ 子树清理**

搜索 `cyrene/`，将：

```
│   └── cyrene/              # Cyrene 角色 (备用)
│       ├── config.yaml
│       └── personality.md
```

替换为：

```
│   └── cyrene/              # Cyrene 角色 (备用，仅含 GPT 参考材料)
│       └── reference/
```

- [ ] **Step 5: prompt/ 树补全**

搜索 `├── Utils.j2`（在 prompt/ 树中），在其下方插入：

```
│   ├── PersonalityCompression.j2     # L3→L2 性格压缩
```

并将 `ref/` 描述从 `GPT 生成原始版本 (snake_case.md)` 改为 `GPT 生成原始版本`。

- [ ] **Step 6: static/ 树补全**

在 static/ 树中搜索 `├── index.html`，在其上方插入：

```
├── Elysia.png              # 角色头像
├── debug.md                # Debug 面板文档
├── live2d-demo/            # Live2D Cubism SDK + demo
```

- [ ] **Step 7: docs/ 展开**

搜索 `├── docs/                    # 文档`，替换为：

```
├── docs/                   # 文档
│   ├── architecture.md     #   架构详解（本文档）
│   ├── frontend-api.md     #   前端 API
│   ├── testing.md          #   测试指南
│   ├── readme-conventions.md #  README 编写规范
│   ├── companion-roadmap.md  #   伴侣化路线
│   ├── companion-prompt-guide.md  # Prompt 指南
│   ├── database-storage.md #   数据库存储设计
│   ├── completed.md        #   已完成记录
│   ├── experience.md       #   开发经验
│   ├── sync-lessons.md     #   同步经验
│   ├── plans/              #   实施计划
│   ├── specs/              #   设计规格
│   └── superpowers/        #   Superpowers 计划/规格
```

- [ ] **Step 8: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md 文件结构树全面修正"
```

---

### Task 9: 第 13-14 节 + 底部"相关文档"

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Tasks 1-8（文件末尾编辑）

- [ ] **Step 1: 第 13 节 — 并发任务计数 + 表格补全**

搜索 `13 个 asyncio.create_task 调用点一览`，替换标题为 `19 个 asyncio.create_task 调用点一览（非穷举，仅列出核心调用点）`。

在表格末尾追加 6 行：

```markdown
| 14 | memory/session.py | archive_append | fire-and-forget，无追踪 |
| 15 | input/server.py | per-connection state monitor | 常驻，disconnect 时 cancel |
| 16 | task_system/task_system.py | sub-agent task submit | TaskSystem 内部追踪 |
| 17 | task_system/task_system.py | sub-agent task resume | TaskSystem 内部追踪 |
| 18 | personality/reflector.py | CharacterReflector fire-and-forget | fire-and-forget，无追踪 |
| 19 | task_scheduler.py | Worker Pool + prune loop | 常驻，stop 时 cancel |
```

- [ ] **Step 2: 第 14 节 — 设计决策表修正**

搜索 `INTERRUPT 的 poll-wait (500×10ms)`，将整行（两列）替换为：

```
| 消息队列模式（busy 时暂存至 pending_messages） | 避免忙等消耗 CPU，容量上限防止内存无限增长，新消息不会丢失 |
```

- [ ] **Step 3: 底部加"相关文档"节**

在文件末尾追加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/testing.md](testing.md) | 测试指南（分层体系 + 命令） |
| [docs/readme-conventions.md](readme-conventions.md) | README 编写规范 |
| [CLAUDE.md](../CLAUDE.md) | AI 助手指令 + 项目地图 |
```

- [ ] **Step 4: 最终验证**

```bash
# 确认总行数合理
wc -l docs/architecture.md
# 确认面包屑存在
head -5 docs/architecture.md
# 确认相关文档存在
tail -15 docs/architecture.md
# 确认无残留过时引用
grep "prompt/test_prompt" docs/architecture.md && echo "STILL STALE" || echo "OK"
grep "5 states" docs/architecture.md && echo "STILL 5" || echo "OK"
grep "~400 行" docs/architecture.md && echo "STILL 400" || echo "OK"
grep "_fake_llm" docs/architecture.md && echo "STILL FAKE" || echo "OK"
grep "~1,325" docs/architecture.md && echo "STILL OLD COUNT" || echo "OK"
grep "poll-wait" docs/architecture.md && echo "STILL POLL" || echo "OK"
```

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: architecture.md 并发表 + 设计决策 + 相关文档 — 全面修复完成"
```
