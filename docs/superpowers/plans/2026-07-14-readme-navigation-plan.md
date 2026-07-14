# README 导航网络 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 28 个 README 之间建立导航网络：根 README 作为总枢纽，每个子 README 有面包屑 + "相关文档"节。

**Architecture:** 根 README 新增"文档导航"章节 + 架构节精简；每个子 README 顶部加面包屑导航、底部加"相关文档"表格；5 个工具 README 补充内容；1 个新建（vision）。

**Tech Stack:** 纯 Markdown 编辑，无代码变更。

## Global Constraints

- `docs/architecture.md` 不动 — 最后单独讨论，动之前和用户确认
- `CLAUDE.md` 不动 — 确保 AI 行为不受影响
- 子 README 现有内容不动，除非明显过时/错误
- 不改变任何模块的代码、API 或测试
- 所有链接使用相对路径
- 面包屑放在 `# 标题` 之后、正文第一段之前
- "相关文档"节放在文件末尾（`---` 分隔线之后）

---

### Task 1: 根 README — 架构节精简 + 文档导航

**Files:**
- Modify: `README.md`

**Interfaces:**
- Produces: 文档导航章节供所有子 README 面包屑追溯至根

- [ ] **Step 1: 精简架构节（替换 L133-154）**

将当前 22 行 ASCII 图 + 数据流替换为概要：

````markdown
## 架构

三层结构：

```
L0  Module/              无状态原子能力 — LLM, STT, TTS, Embedding, Database, Output, _common/, system_prompt, tool
L1  core/                认知中枢 — EventBus → AgentFSM → CognitiveLoop → PromptAssembler
L2  core/memory/         记忆与工具 — SessionMemory, LongTermMemory, ToolManager (Plugin 架构)
L2  Module/task_system/  后台任务 — Planner → Executor → Evaluator (DAG 编排)
```

每层职责分明。每个 Module 服务遵循 Protocol-Registry-Backend 模式，可独立替换。

数据流：用户输入 → EventBus → FSM(THINKING) → CognitiveLoop(inner_voice/TTS/tool_calls) → IDLE（详见 [docs/architecture.md](docs/architecture.md) 完整管线）。
````

- [ ] **Step 2: 在架构节之后、功能表之前插入"文档导航"章节**

```markdown
## 文档导航

### 用户文档
| 文档 | 内容 |
|------|------|
| [快速开始](#快速开始) | 安装、启动、配置、故障排查 |
| [docs/testing.md](docs/testing.md) | 测试命令速查 + 分层体系 |
| [docs/companion-roadmap.md](docs/companion-roadmap.md) | 伴侣化改造路线 |
| [docs/companion-prompt-guide.md](docs/companion-prompt-guide.md) | Prompt 编写指南 |

### Module 层
| 模块 | README | 说明 |
|------|--------|------|
| LLM | [Module/llm/README.md](Module/llm/README.md) | 大模型调用（DeepSeek/OpenAI/Ollama） |
| STT | [Module/stt/README.md](Module/stt/README.md) | 语音识别（SenseVoice） |
| TTS | [Module/tts/README.md](Module/tts/README.md) | 语音合成（GPT-SoVits/MiniMax） |
| Embedding | [Module/embedding/README.md](Module/embedding/README.md) | 文本向量化（HuggingFace/OpenAI） |
| Output | [Module/output/README.md](Module/output/README.md) | 多通道输出（Console + WebSocket） |
| Config | [Module/config/README.md](Module/config/README.md) | 配置基础设施（YAML + env + 校验） |
| Logger | [Module/logger/README.md](Module/logger/README.md) | 日志工厂（plain + JSON） |
| System Prompt | [Module/system_prompt/README.md](Module/system_prompt/README.md) | System Prompt 引擎 + Section Provider |
| Tool | [Module/tool/README.md](Module/tool/README.md) | 工具执行框架（Plugin + SafetyPipeline） |
| Database | [Module/database/README.md](Module/database/README.md) | 数据库总览（Milvus + MongoDB） |
| VectorDB | [Module/database/vectordb/README.md](Module/database/vectordb/README.md) | 向量数据库（Milvus） |
| DocumentDB | [Module/database/documentdb/README.md](Module/database/documentdb/README.md) | 文档数据库（MongoDB） |
| Input | [Module/input/README.md](Module/input/README.md) | AudioBuffer + WebSocket 服务器 |
| Bench | [Module/bench/README.md](Module/bench/README.md) | 延迟基准测试 |
| Module 总览 | [Module/README.md](Module/README.md) | 架构模式 + 各模块 API 速览 |

### Core 层
| 模块 | README | 说明 |
|------|--------|------|
| PersonalityEngine | [core/personality/README.md](core/personality/README.md) | 四层性格模型（Core→Self→State→Mood） |
| 工具: filesystem | [core/tools/filesystem/README.md](core/tools/filesystem/README.md) | read_file / list_directory / write_file |
| 工具: memory | [core/tools/memory/README.md](core/tools/memory/README.md) | search_memory / save_memory |
| 工具: reminder | [core/tools/reminder/README.md](core/tools/reminder/README.md) | set_reminder |
| 工具: runtime_config | [core/tools/runtime_config/README.md](core/tools/runtime_config/README.md) | get_runtime_config |
| 工具: self_state | [core/tools/self_state/README.md](core/tools/self_state/README.md) | get_self_state |
| 工具: time | [core/tools/time/README.md](core/tools/time/README.md) | get_time |
| 工具: vision | [core/tools/vision/README.md](core/tools/vision/README.md) | describe_image（多模态视觉） |
| 工具: web | [core/tools/web/README.md](core/tools/web/README.md) | web_search / web_fetch |

### 其他文档
| 文档 | 内容 |
|------|------|
| [docs/architecture.md](docs/architecture.md) | 完整架构详解（1000+ 行） |
| [docs/frontend-api.md](docs/frontend-api.md) | 前端 API 文档 |
| [CLAUDE.md](CLAUDE.md) | AI 助手指令 + 项目地图 |
| [character/README.md](character/README.md) | 角色配置说明 |
| [prompt/README.md](prompt/README.md) | Prompt 模板说明 |
| [docs/database-storage.md](docs/database-storage.md) | 数据库存储设计 |
| [docs/sync-lessons.md](docs/sync-lessons.md) | 同步经验笔记 |
| [docs/experience.md](docs/experience.md) | 开发经验记录 |
```

- [ ] **Step 3: 修复项目结构树中过时的 `prompt/` 引用**

README.md 项目结构树中有一行过时的目录引用（目录早已从 `Module/prompt/` 重命名为 `Module/system_prompt/`）：

```bash
# 查找并确认过时引用
grep -n "prompt/.*Brain Prompt" README.md
```

将 README.md 中的：
```
├── prompt/                 # Brain Prompt 引擎 — SystemPromptManager + 11 PromptSections
```
替换为：
```
├── system_prompt/          # Brain Prompt 引擎 — SystemPromptManager + 11 PromptSections
```

- [ ] **Step 4: 视觉检查**

```bash
head -200 README.md  # 检查架构节替换 + 文档导航章节插入位置正确
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: 根 README 架构精简 + 新增文档导航章节 + 修复过时 prompt/ 引用"
```

---

### Task 2: Module/README.md — 中间枢纽

**Files:**
- Modify: `Module/README.md`

**Interfaces:**
- Consumes: Task 1 的根 README 文档导航（面包屑追溯目标）
- Produces: Module 层枢纽，所有 Module 子 README 面包屑追溯至此处

- [ ] **Step 1: 顶部加面包屑**

在 `# Module` 标题后、第一段描述前插入：

```markdown
> 文档导航：[项目文档](../README.md)
```

- [ ] **Step 2: 每个模块小节末尾加 📖 链接**

在每个 `### <模块名>` 小节的末尾加一行链接提示。具体位置：

- LLM 节（`### LLM — 大模型调用` 末尾，代码块之后）加：
  ```markdown
  > 📖 完整 API 文档 → [Module/llm/README.md](llm/README.md)
  ```

- STT 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/stt/README.md](stt/README.md)
  ```

- TTS 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/tts/README.md](tts/README.md)
  ```

- Embedding 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/embedding/README.md](embedding/README.md)
  ```

- Output 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/output/README.md](output/README.md)
  ```

- Config 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/config/README.md](config/README.md)
  ```

- Logger 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/logger/README.md](logger/README.md)
  ```

- Prompt 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/system_prompt/README.md](system_prompt/README.md)
  ```

- Tool 节末尾加：
  ```markdown
  > 📖 完整 API 文档 → [Module/tool/README.md](tool/README.md)
  ```

- Database 节末尾加：
  ```markdown
  > 📖 VectorDB 文档 → [Module/database/vectordb/README.md](database/vectordb/README.md)
  > 📖 DocumentDB 文档 → [Module/database/documentdb/README.md](database/documentdb/README.md)
  ```

- [ ] **Step 3: 修复 Module/README.md 结构树中过时的 `prompt/` 引用**

Module/README.md 结构树中有一行过时的目录引用：

```bash
# 查找并确认过时引用
grep -n "prompt/.*System Prompt" Module/README.md
```

将 Module/README.md 结构树中的：
```
├── prompt/               # System Prompt 引擎 + Section Provider 架构
```
替换为：
```
├── system_prompt/        # System Prompt 引擎 + Section Provider 架构
```

- [ ] **Step 4: 底部加"相关文档"节**

在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../docs/architecture.md) | 完整架构详解 + 数据流 |
| [docs/testing.md](../docs/testing.md) | 测试指南 |
```

- [ ] **Step 5: Commit**

```bash
git add Module/README.md
git commit -m "docs: Module README 加面包屑 + 模块链接 + 相关文档"
```

---

### Task 3: Module 子 README（浅层）— 面包屑 + 相关文档

**Files:**
- Modify: `Module/llm/README.md`, `Module/stt/README.md`, `Module/tts/README.md`, `Module/embedding/README.md`, `Module/output/README.md`, `Module/config/README.md`, `Module/logger/README.md`, `Module/input/README.md`, `Module/bench/README.md`（9 个文件）

**Interfaces:**
- Consumes: Task 2 Module/README.md 枢纽已就位（面包屑追溯目标）

- [ ] **Step 1: 给 9 个文件各加顶部面包屑**

每个文件在 `# <标题>` 后、正文第一段前插入相同面包屑：

```markdown
> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)
```

- `Module/llm/README.md`
- `Module/stt/README.md`
- `Module/tts/README.md`
- `Module/embedding/README.md`
- `Module/output/README.md`
- `Module/config/README.md`
- `Module/logger/README.md`
- `Module/input/README.md`
- `Module/bench/README.md`

- [ ] **Step 2: 给每个文件底部加"相关文档"节**

**`Module/llm/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/config/README.md](../config/README.md) | Config 基础设施（LLM 配置依赖） |
| [Module/output/README.md](../output/README.md) | 输出通道（LLM 流式输出目标） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 数据流 |
```

**`Module/stt/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/input/README.md](../input/README.md) | AudioBuffer + WebSocket（STT 输入源） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 音频输入管道 |
```

**`Module/tts/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/output/README.md](../output/README.md) | 输出通道（TTS 输出目标） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + TTS 绑定发送 |
```

**`Module/embedding/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/database/vectordb/README.md](../database/vectordb/README.md) | Milvus 向量数据库（Embedding 消费者） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + LongTermMemory |
```

**`Module/output/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/input/README.md](../input/README.md) | WebSocket 服务器（Output 通道载体） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 输出系统 |
```

**`Module/config/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [docs/architecture.md](../../docs/architecture.md) | 配置系统架构 |
```

**`Module/logger/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [docs/architecture.md](../../docs/architecture.md) | 日志系统架构 |
```

**`Module/input/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/stt/README.md](../stt/README.md) | STT（音频输入消费者） |
| [Module/output/README.md](../output/README.md) | 输出通道（WebSocket 方向） |
| [docs/architecture.md](../../docs/architecture.md) | 输入输出系统架构 |
```

**`Module/bench/README.md`** — 在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/llm/README.md](../llm/README.md) | LLM 模块（bench 测试目标之一） |
| [Module/tts/README.md](../tts/README.md) | TTS 模块（bench 测试目标之一） |
```

- [ ] **Step 3: Commit**

```bash
git add Module/llm/README.md Module/stt/README.md Module/tts/README.md Module/embedding/README.md Module/output/README.md Module/config/README.md Module/logger/README.md Module/input/README.md Module/bench/README.md
git commit -m "docs: Module 子 README（浅层）加面包屑 + 相关文档"
```

---

### Task 4: Module 子 README（嵌套）— 面包屑 + 相关文档

**Files:**
- Modify: `Module/system_prompt/README.md`, `Module/system_prompt/templates/README.md`, `Module/tool/README.md`, `Module/database/README.md`, `Module/database/vectordb/README.md`, `Module/database/documentdb/README.md`（6 个文件）

**Interfaces:**
- Consumes: Task 2 Module/README.md 枢纽已就位

- [ ] **Step 1: 给 6 个文件各加顶部面包屑**

**`Module/system_prompt/README.md`**:（深度 2）
```markdown
> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)
```

**`Module/system_prompt/templates/README.md`**:（深度 3）
```markdown
> 文档导航：[项目文档](../../../README.md) → [Module 文档](../../README.md) → [System Prompt 文档](../README.md)
```

**`Module/tool/README.md`**:（深度 2）
```markdown
> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)
```

**`Module/database/README.md`**:（深度 2）
```markdown
> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)
```

**`Module/database/vectordb/README.md`**:（深度 3）
```markdown
> 文档导航：[项目文档](../../../README.md) → [Module 文档](../../README.md) → [数据库文档](../README.md)
```

**`Module/database/documentdb/README.md`**:（深度 3）
```markdown
> 文档导航：[项目文档](../../../README.md) → [Module 文档](../../README.md) → [数据库文档](../README.md)
```

- [ ] **Step 2: 给每个文件底部加"相关文档"节**

**`Module/system_prompt/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/system_prompt/templates/README.md](templates/README.md) | 模板格式约定 |
| [prompt/README.md](../../prompt/README.md) | Prompt 模板目录说明 |
| [docs/architecture.md](../../docs/architecture.md) | 提示词组装架构 |
```

**`Module/system_prompt/templates/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [System Prompt 文档](../README.md) | System Prompt 引擎 + Section Provider |
| [Module 文档](../../README.md) | 所有 Module 服务总览 |
```

**`Module/tool/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [core/tools/](../../core/tools/) | Elysia 项目的具体工具插件 |
| [docs/architecture.md](../../docs/architecture.md) | ToolManager 架构 + 工具列表 |
```

**`Module/database/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [VectorDB 文档](vectordb/README.md) | Milvus 向量数据库 |
| [DocumentDB 文档](documentdb/README.md) | MongoDB 文档数据库 |
| [docs/architecture.md](../../docs/architecture.md) | 记忆与存储架构 |
```

**`Module/database/vectordb/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [数据库文档](../README.md) | 数据库模块总览 |
| [Module 文档](../../README.md) | 所有 Module 服务总览 |
| [Module/embedding/README.md](../../embedding/README.md) | Embedding 模块（向量生产者） |
```

**`Module/database/documentdb/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [数据库文档](../README.md) | 数据库模块总览 |
| [Module 文档](../../README.md) | 所有 Module 服务总览 |
| [docs/architecture.md](../../../docs/architecture.md) | SessionMemory + 消息归档架构 |
```

- [ ] **Step 3: Commit**

```bash
git add Module/system_prompt/README.md Module/system_prompt/templates/README.md Module/tool/README.md Module/database/README.md Module/database/vectordb/README.md Module/database/documentdb/README.md
git commit -m "docs: Module 子 README（嵌套）加面包屑 + 相关文档"
```

---

### Task 5: Core READMEs + 工具 READMEs（内容够的）— 面包屑 + 相关文档

**Files:**
- Modify: `core/personality/README.md`, `core/tools/filesystem/README.md`, `core/tools/memory/README.md`, `core/tools/web/README.md`（4 个文件）

**Interfaces:**
- Consumes: Task 1 根 README 文档导航已就位

- [ ] **Step 1: 给 4 个文件各加顶部面包屑**

**`core/personality/README.md`**:
```markdown
> 文档导航：[项目文档](../../README.md)
```

**`core/tools/filesystem/README.md`**:
```markdown
> 文档导航：[项目文档](../../../README.md)
```

**`core/tools/memory/README.md`**:
```markdown
> 文档导航：[项目文档](../../../README.md)
```

**`core/tools/web/README.md`**:
```markdown
> 文档导航：[项目文档](../../../README.md)
```

- [ ] **Step 2: 给每个文件底部加"相关文档"节**

**`core/personality/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 提示词组装 |
| [character/README.md](../../character/README.md) | 角色配置说明 |
```

**`core/tools/filesystem/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/web/README.md](../web/README.md) | 网络工具（同样涉及安全约束） |
```

**`core/tools/memory/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [Module/database/vectordb/README.md](../../../Module/database/vectordb/README.md) | Milvus 向量数据库 |
```

**`core/tools/web/README.md`**:

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/filesystem/README.md](../filesystem/README.md) | 文件系统工具（同样涉及安全约束） |
```

- [ ] **Step 3: Commit**

```bash
git add core/personality/README.md core/tools/filesystem/README.md core/tools/memory/README.md core/tools/web/README.md
git commit -m "docs: Core READMEs + 工具 READMEs 加面包屑 + 相关文档"
```

---

### Task 6: 工具 README 补充内容 — reminder, runtime_config, self_state, time

**Files:**
- Modify: `core/tools/reminder/README.md`, `core/tools/runtime_config/README.md`, `core/tools/self_state/README.md`, `core/tools/time/README.md`（4 个文件）

**Interfaces:**
- Consumes: Task 5 的工具 README 模式已确立

- [ ] **Step 1: 重写 `core/tools/reminder/README.md`**

```markdown
# 提醒工具插件

> 文档导航：[项目文档](../../../README.md)

设置定时提醒，在指定秒数后通知用户。

## 工具

- `set_reminder` — 设置定时提醒。参数：`message`（提醒内容）、`delay_seconds`（延迟秒数，1-86400）。到期后通过 asyncio 回调通知用户。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/time/README.md](../time/README.md) | 时间工具（获取当前时间） |
```

- [ ] **Step 2: 重写 `core/tools/runtime_config/README.md`**

```markdown
# 运行时配置工具插件

> 文档导航：[项目文档](../../../README.md)

查看 Agent 运行时配置和状态：模型、TTS/STT、文件/网络权限、工具列表、后台任务、内存使用。

## 工具

- `get_runtime_config` — 查看运行时配置。参数：`query`（可选，`"tools"` / `"filesystem"` / `"network"` / `"tasks"` / `"all"`，留空返回摘要）。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/self_state/README.md](../self_state/README.md) | 自身状态工具（FSM 状态） |
```

- [ ] **Step 3: 重写 `core/tools/self_state/README.md`**

```markdown
# 自身状态工具插件

> 文档导航：[项目文档](../../../README.md)

查看 Agent 当前运行状态：FSM 状态、会话消息数、已加载工具数。

## 工具

- `get_self_state` — 查看 Agent 运行状态。无参数，返回 FSM 状态、会话消息数、已加载工具数、trace_id。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/runtime_config/README.md](../runtime_config/README.md) | 运行时配置工具 |
```

- [ ] **Step 4: 重写 `core/tools/time/README.md`**

```markdown
# 时间工具插件

> 文档导航：[项目文档](../../../README.md)

获取当前日期和时间，支持多时区和格式。

## 工具

- `get_time` — 获取当前时间。参数：`timezone`（默认 `"UTC"`，支持 `"Asia/Shanghai"`、`"+8"` 等）、`format`（`"iso"` 或 `"human"`）。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/reminder/README.md](../reminder/README.md) | 提醒工具（依赖时间） |
```

- [ ] **Step 5: Commit**

```bash
git add core/tools/reminder/README.md core/tools/runtime_config/README.md core/tools/self_state/README.md core/tools/time/README.md
git commit -m "docs: 补充 reminder/runtime_config/self_state/time 工具 README"
```

---

### Task 7: 新建 `core/tools/vision/README.md`

**Files:**
- Create: `core/tools/vision/README.md`

**Interfaces:**
- Consumes: Task 5 的工具 README 模式
- Produces: vision 工具文档，补全 CLAUDE.md 要求的 plugin.py + config.yaml + README.md 三元组

- [ ] **Step 1: 创建 `core/tools/vision/README.md`**

```markdown
# 视觉工具插件

> 文档导航：[项目文档](../../../README.md)

委托多模态大模型（默认 MiMo v2.5）描述用户发送的图片。支持单图/批量模式，结果缓存到 `data/cache/vision/`。

## 工具

- `describe_image` — 描述用户发送的图片。参数：
  - `prompt`（可选，默认请求详细描述图片内容、人物、场景）
  - `image_index`（单图模式，图片序号，从 0 开始）
  - `image_indices`（批量模式，序号列表如 `[0, 1, 2]`，一次调用查看多张图片）

## 配置 (config.yaml)

无需特殊安全配置。Vision model ID 从 `config.vision.model` 读取，默认 `mimo/mimo-v2.5`。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [Module/llm/README.md](../../../Module/llm/README.md) | LLM 模块（视觉模型后端） |
```

- [ ] **Step 2: Commit**

```bash
git add core/tools/vision/README.md
git commit -m "docs: 新建 vision 工具 README"
```

---

### Task 8: prompt/README.md + character/README.md — 面包屑 + 相关文档

**Files:**
- Modify: `prompt/README.md`, `character/README.md`（2 个文件）

**Interfaces:**
- Consumes: Task 1 根 README 文档导航已就位

- [ ] **Step 1: 给 `prompt/README.md` 加面包屑**

在 `#` 标题后、正文前插入：

```markdown
> 文档导航：[项目文档](../README.md)
```

- [ ] **Step 2: 给 `prompt/README.md` 加"相关文档"**

在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [Module/system_prompt/README.md](../Module/system_prompt/README.md) | System Prompt 引擎 |
| [Module/system_prompt/templates/README.md](../Module/system_prompt/templates/README.md) | 模板格式约定 |
```

- [ ] **Step 3: 给 `character/README.md` 加面包屑**

在 `#` 标题后、正文前插入：

```markdown
> 文档导航：[项目文档](../README.md)
```

- [ ] **Step 4: 给 `character/README.md` 加"相关文档"**

在文件末尾添加：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [core/personality/README.md](../core/personality/README.md) | PersonalityEngine 四层模型 |
| [docs/architecture.md](../docs/architecture.md) | 角色配置架构 |
```

- [ ] **Step 5: Commit**

```bash
git add prompt/README.md character/README.md
git commit -m "docs: prompt + character README 加面包屑 + 相关文档"
```

---

### Task 9: 最终验证 — 链接完整性 + 面包屑深度检查

**Files:**
- （不修改文件，仅验证）

**Interfaces:**
- Consumes: Task 1-8 所有 README 改动

- [ ] **Step 1: 检查所有面包屑相对路径深度正确**

```bash
# 深度 2 文件（Module/<name>/README.md）应包含 ../../README.md
grep -l "../../README.md" Module/*/README.md 2>/dev/null | wc -l
# 期望：12（llm, stt, tts, embedding, output, config, logger, system_prompt, tool, database, input, bench）

# 深度 3 文件应包含 ../../../README.md
grep -l "../../../README.md" Module/*/*/README.md 2>/dev/null | wc -l
# 期望：3（database/vectordb, database/documentdb, system_prompt/templates）

# Core 深度 2（core/personality/）应包含 ../../README.md
grep "../../README.md" core/personality/README.md

# Core 深度 3（core/tools/*/）应包含 ../../../README.md
grep -l "../../../README.md" core/tools/*/README.md 2>/dev/null | wc -l
# 期望：8

# 深度 1 文件（prompt/, character/）应包含 ../README.md
grep -l "../README.md" prompt/README.md character/README.md | wc -l
# 期望：2
```

- [ ] **Step 2: 检查没有断链（用 git diff 手动抽查）**

```bash
git diff --stat main  # 确认 28 个文件被修改
```

- [ ] **Step 3: 抽查 3 个文件的渲染效果**

在 IDE 中打开以下文件，确认 Markdown 渲染正常（链接可点击、表格对齐）：
- `README.md` — 文档导航章节
- `Module/llm/README.md` — 面包屑 + 相关文档
- `core/tools/vision/README.md` — 新建文件

- [ ] **Step 4: Commit（如有修正）**

```bash
git add -A
git commit -m "docs: 最终验证 — README 导航网络链接修正"
```
