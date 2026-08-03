# 测试分层管理 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给全部 63 个测试文件的 ~1500 个测试加上 4 层 pytest markers（unit/service/integration/slow），逐测试审查正确性，更新 pytest.ini/CLAUDE.md/Makefile。

**Architecture:** 按 spec 定义的 10 批推进。每批：打 marker + 4 维度 review（有效性/正确性/归属/分层）+ commit。最后更新基础设施文件。

**Tech Stack:** pytest markers, Python 3.13, conda

## Global Constraints

- 4 层 markers: `unit`（纯函数无 I/O）、`service`（单模块+mock）、`integration`（需 DB/API）、`slow`（完整管线或 >1s）
- 每个测试函数必须恰好有 1 个 marker（class 级别标记继承到所有方法）
- review 4 维度: 有效性/正确性/归属/分层
- 过时测试当场删除并记录原因到 commit message
- 断言错误当场修复（小改动）或记录到 todo.md（大改动）
- 归属不合理的暂不拆分，记录到 todo.md
- 每批完成后验证该批所有测试通过
- CI 命令: `pytest -m "not slow" -q --tb=short`

---

### Task 0: 基础设施准备

**Files:**
- Modify: `pytest.ini`
- Modify: `Makefile`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新 pytest.ini 注册 4 个 markers**

Run: `cat pytest.ini`
将内容替换为：

```ini
[pytest]
markers =
    unit:        纯函数/类测试，无 I/O，无 Agent 启动，无外部服务 mock
    service:     单模块测试，可能需要 mock，但不启动完整 Agent/不连真实 DB
    integration: 需要真实数据库（MongoDB/Milvus）或外部 API
    slow:        完整管线、端到端、或单测耗时 >1s
```

- [ ] **Step 2: 扩展 Makefile test targets**

Run: `cat Makefile` 检查现有 test targets。

追加以下 targets（**注意：Makefile 缩进必须用 TAB，不能用空格**）：

```makefile
# 测试分层命令
test-quick:
	conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short

test-normal:
	conda run -n Elysia python -m pytest -m "not slow" -q --tb=short

test-full:
	conda run -n Elysia python -m pytest -q
```

- [ ] **Step 3: 更新 CLAUDE.md 测试命令**

Run: `cat CLAUDE.md | head -45` 找到测试命令区域（第 14-41 行）。

替换为：

```markdown
## Running tests

### 分层运行

```bash
# 日常开发 — 秒级（unit + service）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow" -q --tb=short

# 全量 — 推之前（~22min）
make test-full
# 或: pytest -q
```

### 按模块运行

```bash
# Personality engine tests
python -m pytest core/personality/tests/ -v

# System prompt tests
python -m pytest Module/system_prompt/tests/ -v

# Core tests
python -m pytest core/tests/ -v

# TaskSystem tests
python -m pytest Module/task_system/tests/ -v

# 其他模块
python -m pytest Module/llm/test_llm.py -v
python -m pytest Module/tts/test_tts.py -v
python -m pytest Module/stt/test_stt.py -v
python -m pytest Module/embedding/test_embedding.py -v
python -m pytest Module/output/test_output.py -v
python -m pytest Module/config/test_config.py -v
python -m pytest Module/system_prompt/test_prompt.py -v
python -m pytest Module/logger/test_logger.py -v
python -m pytest Module/database/vectordb/test_vectordb.py -v
python -m pytest Module/database/documentdb/test_documentdb.py -v
python -m pytest Module/tool/test_framework.py -v
python -m pytest Module/tool/test_tool_safety.py -v
```

### pytest markers

| marker | 标准 | 日常使用 |
|--------|------|----------|
| `unit` | 纯函数/类，无 I/O，无 Agent | `-m "unit or service"` |
| `service` | 单模块 + mock，不启动 Agent | `-m "unit or service"` |
| `integration` | 需真实 DB 或外部 API | `-m "not slow"` |
| `slow` | 完整管线或 >1s | 全量时才跑 |
```

- [ ] **Step 4: 验证 markers 注册生效**

```bash
pytest --markers
```
Expected: 看到 `unit`、`service`、`integration`、`slow` 在输出中。

- [ ] **Step 5: Commit**

```bash
git add pytest.ini Makefile CLAUDE.md
git commit -m "feat: test 分层管理基础设施 — 4 markers + Makefile targets + CLAUDE.md"
```

---

### Task 1: Batch 1 — Module/_common/ + Module/config/ + Module/logger/

**Files 范围（8 文件）:**
- `Module/_common/tests/test_registry_helpers.py` (11 tests)
- `Module/_common/tests/test_test_helpers.py` (3 tests)
- `Module/_common/tests/test_main_runner.py` (2 tests)
- `Module/_common/tests/test_config_test_helpers.py` (4 tests — parametrized)
- `Module/config/test_config.py` (64 tests)
- `Module/config/test_preflight.py` (19 tests)
- `Module/logger/test_logger.py` (33 tests)

**预计 marker 分布:** 全部 `unit`

**Review 关注点:**
- `test_test_helpers.py`: 刚修复过，确认 marker 正确
- `test_config_test_helpers.py`: parametrized tests，marker 加在 class 上
- `test_config.py`: 64 个测试中是否有需要外部文件的？

- [ ] **Step 1: 逐个文件打 marker + review**

对每个文件：
1. 读取文件，理解测试内容
2. 在 class 级别或 function 级别加 `@pytest.mark.unit`（这批全部是 unit）
3. 4 维度 review：有效性/正确性/归属/分层
4. 记录发现到 batch review notes

Marker 添加示例（class 级别）：
```python
import pytest

pytestmark = pytest.mark.unit  # 整个模块默认 unit

class TestSomething:
    ...
```

或者 per-class：
```python
@pytest.mark.unit
class TestRegistryHelpers:
    ...
```

- [ ] **Step 2: 验证该批测试全部通过**

```bash
conda run -n Elysia python -m pytest Module/_common/tests/ Module/config/test_config.py Module/logger/test_logger.py -v -q
```
Expected: 全绿（~136 tests passed）

- [ ] **Step 3: 验证 marker 查询有效**

```bash
conda run -n Elysia python -m pytest -m unit -q --tb=short Module/_common/tests/ Module/config/test_config.py Module/logger/test_logger.py
```
Expected: 同样全部通过。

- [ ] **Step 4: Commit**

```bash
git add Module/_common/tests/ Module/config/test_config.py Module/logger/test_logger.py
git commit -m "test: batch 1 — marker review: _common/ + config/ + logger/ (all unit)

Review result: <N> passed, <M> issues found, <K> tests marked unit"
```

---

### Task 2: Batch 2 — Module/database/ + Module/embedding/

**Files 范围（6 文件）:**
- `Module/database/documentdb/test_documentdb.py` (29 tests)
- `Module/database/documentdb/test_config.py` (2 tests)
- `Module/database/vectordb/test_vectordb.py` (33 tests)
- `Module/database/vectordb/test_config.py` (2 tests)
- `Module/embedding/test_embedding.py` (31 tests)
- `Module/embedding/test_config.py` (2 tests)

**预计 marker 分布:** unit（config tests）+ service（backend tests with mock）

**Review 关注点:**
- `test_documentdb.py` / `test_vectordb.py`: mock 是否正确？是否真的不连真实 DB？
- `test_embedding.py`: 缓存 key 修复已生效？检查断言

- [ ] **Step 1: 逐个文件打 marker + review**

- `test_config.py` 类 → `@pytest.mark.unit`
- Backend tests → `@pytest.mark.service`（它们 mock 了数据库连接）

- [ ] **Step 2: 验证**

```bash
conda run -n Elysia python -m pytest Module/database/ Module/embedding/ -v -q
```

- [ ] **Step 3: Commit**

---

### Task 3: Batch 3 — Module/llm/ + Module/stt/ + Module/tts/

**Files 范围（6 文件）:**
- `Module/llm/test_llm.py` (38 tests)
- `Module/llm/test_config.py` (2 tests)
- `Module/stt/test_stt.py` (37 tests)
- `Module/stt/test_config.py` (2 tests)
- `Module/tts/test_tts.py` (70 tests)
- `Module/tts/test_config.py` (2 tests)

**预计 marker 分布:** unit（config）+ service（backend tests with mock）

**Review 关注点:**
- `test_tts.py` 是第三大文件（70 tests），逐个过
- `test_llm.py`: stream_think / retry 逻辑测试是否 valid

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 4: Batch 4 — core/input/ + Module/output/

**Files 范围（4 文件）:**
- `core/input/test_server.py` (30 tests)
- `core/input/test_audio_buffer.py` (29 tests)
- `core/input/test_wakeword.py` (8 tests)
- `Module/output/test_output.py` (54 tests)

**预计 marker 分布:** unit + service

**Review 关注点:**
- `test_server.py`: 刚修过 `test_invalid_json_returns_error_message`，确认 marker
- `test_audio_buffer.py`: 大量 `asyncio.sleep()`，按标准归 `slow` 还是 `service`？

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 5: Batch 5 — Module/system_prompt/

**Files 范围（6 文件）:**
- `Module/system_prompt/test_prompt.py` (19 tests)
- `Module/system_prompt/tests/unit/test_protocol.py` (9 tests)
- `Module/system_prompt/tests/unit/test_sections/test_boundary.py` (25 tests)
- `Module/system_prompt/tests/unit/test_system_prompt_manager.py` (6 tests)
- `Module/system_prompt/tests/integration/test_full_render.py` (3 tests)
- `Module/system_prompt/tests/integration/test_message_builder.py` (12 tests)

**预计 marker 分布:** unit（unit/ 目录）+ integration（integration/ 目录）

**Review 关注点:**
- `test_full_render.py` 和 `test_message_builder.py` 是否需要真实文件系统/模板？

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 6: Batch 6 — Module/tool/

**Files 范围（2 文件）:**
- `Module/tool/test_framework.py` (96 tests)
- `Module/tool/test_tool_safety.py` (33 tests)

**预计 marker 分布:** unit + service

**Review 关注点:**
- `test_framework.py` 第二大文件（96 tests），review 工作量较大
- 其中是否有 `sleep(10)` 之类的 slow test？（之前排查发现 883 行有）

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 7: Batch 7 — Module/task_system/

**Files 范围（16 文件）:**
- `Module/task_system/tests/unit/` (11 files)
- `Module/task_system/tests/integration/` (5 files)

**预计 marker 分布:** unit（unit/）+ integration（integration/）

**Review 关注点:**
- `test_task_system_lifecycle.py`: 大量 `asyncio.sleep(0.5)`，可能归 `slow`
- `test_e2e_mvp.py`: 端到端，可能包含 `slow`

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 8: Batch 8 — core/tools/

**Files 范围（8 文件）:**
- `core/tools/filesystem/test_filesystem.py` (16 tests)
- `core/tools/filesystem/test_write_file.py` (11 tests)
- `core/tools/memory/test_memory.py` (9 tests)
- `core/tools/reminder/test_reminder.py` (2 tests)
- `core/tools/runtime_config/test_runtime_config.py` (13 tests)
- `core/tools/self_state/test_self_state.py` (3 tests)
- `core/tools/time/test_time.py` (8 tests)
- `core/tools/web/test_web.py` (20 tests)

**预计 marker 分布:** unit + service

**Review 关注点:**
- `test_filesystem.py` / `test_write_file.py`: 是否真的操作文件系统？是的话归 `integration`
- `test_reminder.py`: 刚归档过，确认只有 2 tests

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 9: Batch 9 — core/memory/ + core/personality/

**Files 范围（6 文件）:**
- `core/memory/tests/test_long_term.py` (8 tests)
- `core/memory/tests/test_message_archive.py` (12 tests)
- `core/memory/tests/test_session_archive_integration.py` (3 tests)
- `core/memory/tests/test_session.py` (4 tests)
- `core/personality/tests/test_engine.py` (7 tests)
- `core/personality/tests/test_reflector.py` (15 tests)

**预计 marker 分布:** unit + service

**Review 关注点:**
- `test_session_archive_integration.py`: 名字有 integration，确认是否需要 DB

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**
- [ ] **Step 3: Commit**

---

### Task 10: Batch 10 — core/tests/（最重）

**Files 范围（10 文件）:**
- `core/tests/test_phase1.py` (32 tests)
- `core/tests/test_phase2.py` (72 tests)
- `core/tests/test_phase3.py` (110 tests)
- `core/tests/test_sentence_buffer.py` (65 tests)
- `core/tests/test_task_model.py` (15 tests)
- `core/tests/test_task_scheduler.py` (26 tests)
- `core/tests/test_timing.py` (16 tests)
- `core/tests/test_request_id.py` (3 tests)
- `core/tests/test_prompt_recorder.py` (7 tests)
- `core/tests/test_token_tracker.py` (17 tests)

**预计 marker 分布:** unit + service + integration + slow（最复杂的一批）

**Review 关注点:**
- `test_phase1/2/3.py`（214 tests）: 最大的三个文件，其中很多启动完整 Agent → `slow` 或 `integration`
- 不启动 Agent 的单元测试 → `unit` 或 `service`
- 刚修过的测试确认 marker 正确
- `test_sentence_buffer.py`（65 tests）: 纯逻辑测试 → `unit`
- `test_timing.py`: 含 `time.sleep(0.01)`，归 `service` 不是 `slow`

**关键判断标准:**
- 创建 `Agent(config=AppConfig())` 并 `agent.start()` → `slow`
- 创建 `Agent` 但不 start，或只测组件 → `service`
- 纯函数/类测试无 Agent → `unit`
- 需要 MongoDB/Milvus 连接 → `integration`

- [ ] **Step 1: 逐个文件打 marker + review**
- [ ] **Step 2: 验证**

```bash
# 确保 -m "not slow" 可以跳过最重的测试
conda run -n Elysia python -m pytest -m "not slow" core/tests/ -q --tb=short
```

- [ ] **Step 3: Commit**

---

### Task 11: 最终验证 + 清理

- [ ] **Step 1: 全量 marker 验证**

```bash
# 确认 unit 层秒级完成
conda run -n Elysia python -m pytest -m unit -q -x --tb=short 2>&1 | tail -5

# 确认 "not slow" 排除慢速测试
conda run -n Elysia python -m pytest -m "not slow" -q --tb=short 2>&1 | tail -5

# 确认全量通过
conda run -n Elysia python -m pytest -q 2>&1 | tail -5
```

- [ ] **Step 2: 验证 -m "unit or service" 秒级完成**

```bash
time conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
```
Expected: < 60s（vs 完整 22min）

- [ ] **Step 3: 汇总 review 发现到 todo.md**

将所有批次 review 中发现的过时测试、归属问题、需修复断言整理到 todo.md。

- [ ] **Step 4: 更新 CLAUDE.md 测试数量**

更新 CLAUDE.md 中的测试总数（如提及）。

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "test: 分层管理完成 — 4 markers × 63 files × review"
```
