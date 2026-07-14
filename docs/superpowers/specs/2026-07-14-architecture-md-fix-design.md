# architecture.md 全面修复 — 设计规格

## 问题

`docs/architecture.md`（1049 行）是项目最重要的架构文档。6 维度审查发现 50 个确认问题：11 critical（数据错误/断链）、26 important（内容不完整/不准确）、13 minor（精度/细节）。同时缺少导航连接（面包屑 + 相关文档）。

## 目标

一次性修复全部 50 个问题，并为 architecture.md 添加导航连接。

## 原则

- 只修错、补漏，不重写、不重构结构
- 13 个现有章节标题和顺序不变
- 能指向 testing.md / readme-conventions.md 的就指过去，不复制内容
- 代码行数声明（CognitiveLoop ~400→~1041 等）直接更新，但标注"约"以容忍未来漂移

## 修复清单（按章节）

### 全局：导航连接

- 顶部 `#` 标题后加面包屑：`> 文档导航：[项目文档](../README.md)`
- 底部加"相关文档"节：

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

### 第 1 节：架构总览

| 行 | 修复 |
|----|------|
| 36 | `(5 states)` → `(6 states)`。FSM 图加 `IDLE_BACKGROUND_TASK` 状态 |

### 第 3 节：L0 基础设施层

| 行 | 修复 |
|----|------|
| 221 | LLM 后端表加 `gemini`（标注未实现） |
| 222 | STT 后端表加 `openai`/`deepgram`（标注未实现） |

### 第 4 节：L1 认知控制层

| 行 | 修复 |
|----|------|
| 344 | `(~400 行)` → `(~1,040 行)` |
| 396 | `docs/superpowers/specs/2026-06-24-inner-voice-design.md` → `.../archive/2026-06-24-inner-voice-design.md` |
| 479 | `docs/superpowers/specs/2026-07-04-prompt-section-provider-design.md` → `.../archive/2026-07-04-prompt-section-provider-design.md` |
| 495 | `(~90 行)` → `(~170 行)` |
| 497 | `core/_fake_llm.py` → `core/tests/fake_llm.py` |

### 第 5 节：L2 记忆与工具层

| 行 | 修复 |
|----|------|
| 508 | `默认 50` → `代码默认 20，config.yaml 覆盖为 50` |
| 580-594 | 工具表逐项修正（见下表） |

**工具表修正：**

| 工具 | 参数 | 说明 |
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

### 第 6 节：输入输出系统

| 行 | 修复 |
|----|------|
| 625 | Server→Browser 消息表：加 `session`、`capabilities`、`wake`、`switch_model_result`、`error` |
| 638 | Browser→Server 消息表：加 `wake_toggle`、`switch_model`、`tool_approval`；text content 标注可为 content array |
| 648 | 路由表：30+ → 40+；补全缺失的 10+ 条路由 |

### 第 7 节：配置系统

| 行 | 修复 |
|----|------|
| 722 | 优先级加 `.env`：`CLI flag > 环境变量 (.env) > config.yaml > 默认值` |
| 729 | 角色配置示例加 `appearance` 块 |
| 732 | `wake_words` 列表格式改为多行 `-` 格式（匹配实际文件） |
| 749 | 删除重复且无效的 `- type: time_of_day` 行 |

### 第 8 节：Debug Dashboard

- 路由表修正已覆盖（第 6 节），此节无需额外改动

### 第 9 节：组件组装 (Agent)

| 行 | 修复 |
|----|------|
| 780-789 | `start()` 伪代码补全：FSM.reset + reconnect、Archive.connect、状态恢复、JSONL recovery、ToolContext、db_health_loop |
| 781 | `TTS/STT 条件启动` → `TTS 条件启动`（STT 移至 run_server 说明） |
| 791-803 | `stop()` 伪代码补全：buffer flush（consolidator/reflector/compression/MemoryReflector）→ 修正执行顺序 → 加 EmbeddingFactory.shutdown → 加 db_health_task cancel |
| 803 | `每步 3s timeout` → `大部分 3s，少数关键步骤 5s` |

### 第 10 节：日志系统

- 无需改动

### 第 11 节：测试架构

| 行 | 修复 |
|----|------|
| 837-866 | **整个测试节替换为指针**：删掉过时的文件列表和计数，改为简要总结 + 指向 `docs/testing.md` |

替换内容：
```markdown
## 11. 测试架构

~1,558 个测试按 4 层 markers 组织（unit / service / integration / slow）。

完整分层体系、测试文件索引、按模块运行命令 → [docs/testing.md](testing.md)
```

### 第 12 节：文件结构树

| 行 | 修复 |
|----|------|
| 859 | `prompt/test_prompt.py` → `system_prompt/test_prompt.py` |
| 880 | 顶层加 `Makefile`、`requirements.txt`、`docker-compose.yml`、`pytest.ini`、`data/` |
| 904 | core/ 加 `consolidator.py`、`schema.py`、`task.py` |
| 945-947 | cyrene/ 子树删 `config.yaml` 和 `personality.md`（不存在），只保留 `reference/` |
| 949 | prompt/ 树加 `PersonalityCompression.j2` |
| 959 | `ref/` 描述去掉 `snake_case.md`，改为 `GPT 生成原始版本` |
| 961 | static/ 加 `Elysia.png`、`debug.md`、`live2d-demo/` |
| 967 | docs/ 展开为多行，列出主要文件 |

### 第 13 节：并发任务全景

| 行 | 修复 |
|----|------|
| 995 | 标题"13 个"→ 重新核实计数。加注释"（非穷举，仅列出核心调用点）"。补充遗漏的 6 个调用点。 |

### 第 14 节：关键设计决策

| 行 | 修复 |
|----|------|
| 1020 | `INTERRUPT 的 poll-wait (500×10ms)` → `消息队列模式（busy 时暂存至 pending_messages）` |

## 约束

- 13 个章节标题、顺序、编号不变
- 现有 ASCII 架构图、数据流图保留（不动）
- 只修错补漏，不重写
- 能指向 testing.md / readme-conventions.md 的就不复制内容
