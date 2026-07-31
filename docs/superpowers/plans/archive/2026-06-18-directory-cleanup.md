# 项目目录整理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将散放的测试文件和顶层文档归位，清理日志残留，使目录结构一致。

**Architecture:** 纯文件操作——3 个 `git mv`（测试归位），2 个 `mv`（顶层文件归类），17 个 `rm`（空日志清理）。不修改任何 `.py` 代码。

**Tech Stack:** bash / git

## Global Constraints

- 不修改任何 Python 代码
- 测试移动后必须能正常运行
- 每个 Task 独立可验证

---

### Task 1: 移动 `core/` 中 3 个测试文件到 `core/tests/`

**Files:**
- Move: `core/test_prompt_recorder.py` → `core/tests/test_prompt_recorder.py`
- Move: `core/test_timing.py` → `core/tests/test_timing.py`
- Move: `core/test_token_tracker.py` → `core/tests/test_token_tracker.py`

**Interfaces:**
- Produces: 3 个测试文件位于 `core/tests/`，import 不变（均使用 `from core.xxx` 绝对导入）

- [ ] **Step 1: 移动测试文件**

```bash
git mv core/test_prompt_recorder.py core/tests/test_prompt_recorder.py
git mv core/test_timing.py core/tests/test_timing.py
git mv core/test_token_tracker.py core/tests/test_token_tracker.py
```

- [ ] **Step 2: 验证移走的文件能正常运行**

```bash
python -m pytest core/tests/test_prompt_recorder.py core/tests/test_timing.py core/tests/test_token_tracker.py -v
```

预期: 所有测试 PASS

- [ ] **Step 3: 核心测试套件无回归**

```bash
python -m pytest core/tests/ -v
```

预期: 所有测试 PASS（含新移入的 3 个 + 原有 7 个）

- [ ] **Step 4: 提交**

```bash
git commit -m "chore: 移动 core/ 中 3 个测试文件到 core/tests/ 归位"
```

---

### Task 2: 顶层文件归类 + 空日志清理

**Files:**
- Move: `code-review-report.md` → `docs/code-review-report.md`
- Move: `app_log.txt` → `log/app.log`
- Delete: `log/` 下 17 个零字节 `logger_*.log`

**Interfaces:**
- 无依赖

- [ ] **Step 1: 移动顶层文件**

```bash
mv code-review-report.md docs/code-review-report.md
mv app_log.txt log/app.log
```

- [ ] **Step 2: 删除 17 个零字节日志残留**

```bash
rm log/logger_CloseSet.log log/logger_CloseTest.log log/logger_concurrent_test.log log/logger_ExactName.log log/logger_IntLevel.log log/logger_L1.BrainLayer.log log/logger_LoggerA.log log/logger_LoggerB.log log/logger_NoDup.log log/logger_RuntimeLevel.log log/logger_SameName.log log/logger_SensorLayer.log log/logger_test_crit_set.log log/logger_TestInfo.log log/logger_TestModule.log
```

- [ ] **Step 3: 验证顶层干净**

```bash
ls /home/yomu/Agent/code-review-report.md /home/yomu/Agent/app_log.txt 2>&1
```

预期: `No such file or directory`（两个文件都已移走）

- [ ] **Step 4: 提交**

```bash
git add docs/code-review-report.md log/app.log
git rm code-review-report.md app_log.txt log/logger_*.log
git commit -m "chore: 顶层文件归类 + 清理 17 个空日志残留"
```

---

## 验证

```bash
# 全文测试无回归
python -m pytest core/tests/ -v

# 确认文件结构符合设计
ls core/test_*.py          # 应该为空（无测试文件散落 core/）
ls core/tests/test_*.py    # 应该为 10 个
ls log/logger_*.log        # 应该为空
ls code-review-report.md   # 不应该存在
ls app_log.txt             # 不应该存在
```
