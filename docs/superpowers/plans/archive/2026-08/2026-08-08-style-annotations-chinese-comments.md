# 风格整改：参数类型注解补齐 + 英文注释中文化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐生产代码缺失的参数类型注解（ANN001/002/003），将纯英文注释/docstring 全部改为中文，并把 ANN 规则加入 ruff lint 门禁防回归。

**Architecture:** 纯机械风格整改，零行为变更。按目录分簇并行派发 subagent（每簇一个，互不重叠文件）。生产代码同时处理注解 + 注释 + docstring；测试文件单独一簇只处理注释。

**Tech Stack:** Python 3.11+ / ruff 0.16（ANN = flake8-annotations）/ AST 扫描脚本（检测英文 docstring）。

**Spec:** 决策见 `docs/superpowers/specs/archive/2026-08/2026-08-08-style-annotations-chinese-comments-design.md`

## Global Constraints

- **仅改生产代码注解**（用户决策）；测试文件不做注解（pytest fixture 动态类型惯例），仅翻英文注释。
- **英文注释/docstring 全部翻成中文**（用户决策）——`# 纯英文行` + 英文 docstring 首行。中英混合注释（中文嵌英文技术术语如 `# JSONL fallback — MongoDB 不可用`）是既有风格，**保持不动**。
- **保留**：代码标识符（`prune`/`Param`/`TypeAdapter` 等）、URL、`# noqa`/`# type: ignore`/`# pragma`/shebang/编码声明。翻译时只翻散文部分，标识符留在原文。
- **`**kwargs: Any` / `*args: Any`** 需 `from typing import Any`（缺则加 import）。
- 方法参数注解沿用既有风格：`self`/`cls` 不注解；带默认值参数给类型即可。
- **中文注释**、**中文 docstring**（CLAUDE.md「代码注释用中文」）。
- **禁相对导入**（ruff TID252）——新增 import 一律绝对导入。
- ruff 门禁：`ruff.toml` 的 `select` 加 `ANN001/ANN002/ANN003`，并 `per-file-ignores` 豁免测试文件（`**/tests/**`、`**/conftest.py`、`**/test_*.py`、`**/fake_*.py`）。
- 每簇完成后：`ruff check <scope>` 零 ANN 违规 + `python -m pytest <scope 内 tests> -q` 全绿。

---

### Task 1: `core/input/` + `core/agent/` + `core/adapter/` 生产文件整改

**Files:**
- Modify（注解）: `core/input/session_routes.py`(14) `ws_handler.py`(6) `ssrf.py`(4) `server.py`(3) `api_routes.py`(3) `voice_sessions.py`(2) `tts_profile.py`(2) `attachment_routes.py`(2) `core/agent/agent.py`(3) `core/adapter/llm_adapter.py`(3)
- Modify（注释）: `core/input/audio_buffer.py`(15) `server.py`(8) `wakeword.py`(5) `ws_handler.py`(2) `voice_sessions.py`(2) `agent.py`(2) `llm_adapter.py`(2) + docstrings（`audio_buffer.py`11 `server.py`8 `wakeword.py`7 `debug_routes/*`~22 `ws_handler.py`6 `agent.py`3 `llm_adapter.py`3 `prompt_assembler.py`2）
- Test: 覆盖范围含 tests/ 的运行

**Interfaces:**
- Consumes: ruff ANN001/002/003 精确违规清单（Agent 自行 `ruff check core/input/ core/agent/ core/adapter/ --select ANN001,ANN002,ANN003`）
- Produces: 零 ANN 违规 + 零英文注释/docstring 的生产文件

- [ ] **Step 1: 写失败验证基线**——`ruff check core/input/ core/agent/ core/adapter/ --select ANN001,ANN002,ANN003` 报 N>0；纯英文注释 grep 报 N>0
- [ ] **Step 2: 补齐注解**——按 ruff 输出逐行补类型；`**kwargs: Any`/`*args: Any`；缺 `Any` 时加绝对导入
- [ ] **Step 3: 翻注释/docstring**——纯英文 `#` 行与英文 docstring 首行改中文；保留标识符/URL/pragma
- [ ] **Step 4: 验证**——`ruff check <scope> --select ANN001,ANN002,ANN003` 清零；`python -m pytest core/input/tests/ core/agent/tests/ core/adapter/tests/ -q` 全绿
- [ ] **Step 5: 提交** `style(core/input): 参数类型注解补齐 + 英文注释中文化`

---

### Task 2: `core/` 其余生产文件（cognitive/memory/monitoring/debug/status/personality/storage/task/schema/config/text/utils/tools）

**Files:**
- Modify（注解）: `core/cognitive/loop.py`(5) `core/memory/session_manager.py`(10) `session.py`(3) `core/storage/attachment_release.py`(8) `desktop_call_repo.py`(1) `core/task/scheduler.py`(1) `core/personality/engine.py`(1) `core/monitoring/__init__.py`(1) `core/schema/token.py`(3) `core/schema/adapters/message_adapter.py`(1)
- Modify（注释/docstring）: `core/debug/info.py`(18) `core/memory/long_term.py`(12+2doc) `core/cognitive/loop.py`(6+4doc) `core/utils/log_setup.py`(5) `core/task/scheduler.py`(4) `core/text/splitter.py`(1) `parts.py`(1) `core/tools/filesystem/plugin.py`(3) + docstrings（`idle_monitor.py`5 `event_bus.py`4 `agent_fsm.py`4 `emotion.py`3 `memory/consolidator.py`2 `agent/server.py`2 `core/config/*`~6 `core/status/*`）
- Test: `core/memory/tests/ core/cognitive/tests/ core/storage/tests/ core/task/tests/ core/schema/tests/ core/debug/tests/ core/personality/tests/ core/config/tests/ core/monitoring/tests/ core/text/tests/`

- [ ] **Step 1: 基线**——同 Task 1
- [ ] **Step 2: 补注解**——同 Task 1
- [ ] **Step 3: 翻注释/docstring**
- [ ] **Step 4: 验证**——ruff 清零 + 上述 tests 全绿
- [ ] **Step 5: 提交** `style(core): 参数类型注解补齐 + 英文注释中文化`

---

### Task 3: `Module/` 生产文件（output/tool/database/embedding/llm/stt/tts/system_prompt/config/workflow_contract/workflow_engine/bench）

**Files:**
- Modify（注解）: `Module/output/manager.py`(14) `protocol.py`(8) `websocket.py`(3) `console.py`(1) `__main__.py`(4) `Module/tool/__main__.py`(10) `safety.py`(3) `manager.py`(2) `protocol.py`(1) `Module/database/*/registry.py`+`mongodb.py`(1)+`milvus.py`(1) `Module/embedding/{huggingface,openai_compat,registry}.py` `Module/llm/{openai_compat,registry}.py` `Module/stt/{sensevoice,registry,protocol,openai_stt}.py` `Module/tts/registry.py`(2) `Module/system_prompt/**`(~10) `Module/workflow_contract/schema.py`(2)
- Modify（注释/docstring）: `Module/bench/bench.py`(9) `Module/tool/safety.py`(6) `__init__.py`(4) `Module/workflow_contract/schema.py`(5) `Module/config/{preflight,field,validator,loader}.py`(~10) `Module/workflow_engine/*`(~8) `Module/tts/__main__.py`(3) `Module/output/console.py`(2) `Module/embedding/huggingface.py`(2) + docstrings（`Module/config/errors.py`6 `validator.py`5 `loader.py`4 `field.py`2 `workflow_engine/debug_api.py`4 `workflow_engine.py`2 `Module/tool/protocol.py`3）
- Test: `Module/output/tests/ Module/tool/tests/ Module/database/*/tests/ Module/embedding/tests/ Module/llm/tests/ Module/stt/tests/ Module/tts/tests/ Module/system_prompt/tests/ Module/config/tests/`（`Module/workflow_engine/tests` 排除——另有 test-normal 门禁）

- [ ] **Step 1: 基线**
- [ ] **Step 2: 补注解**
- [ ] **Step 3: 翻注释/docstring**
- [ ] **Step 4: 验证**——ruff 清零 + 上述 tests 全绿（不含 workflow_engine/tests）
- [ ] **Step 5: 提交** `style(module): 参数类型注解补齐 + 英文注释中文化`

---

### Task 4: `frontend_contract/` 生产文件

**Files:**
- Modify（注解）: `frontend_contract/gen_ts.py`(7) `base.py`(3)
- Modify（注释/docstring）: `frontend_contract/debug.py`(3) `ws_message.py`(2) `__init__.py`(2) + 英文 docstring
- Test: `frontend_contract/tests/`

- [ ] **Step 1: 基线**
- [ ] **Step 2: 补注解**
- [ ] **Step 3: 翻注释/docstring**
- [ ] **Step 4: 验证**——ruff 清零 + `pytest frontend_contract/tests/` 全绿
- [ ] **Step 5: 提交** `style(frontend_contract): 参数类型注解补齐 + 英文注释中文化`

---

### Task 5: 测试文件英文注释中文化（不改注解）

**Files:**
- Modify: 全部测试文件（`core/**/tests/**`、`Module/**/tests/**`、`**/conftest.py`、`core/tests/fake_*.py`）——279 行纯英文注释 + 英文 docstring
- Test: 抽样运行改动的测试文件

- [ ] **Step 1: 基线**——`grep -rEn "^\s*#\s+[A-Za-z][^一-龥]*$"`（tests scope）报 279 行
- [ ] **Step 2: 翻译**——纯英文注释行/docstring 改中文；保留代码标识符、断言字符串、`# noqa`
- [ ] **Step 3: 验证**——英文注释清零；改动的测试文件 `pytest <file> -q` 全绿
- [ ] **Step 4: 提交** `style(tests): 英文注释中文化`

---

### Task 6: ruff 门禁——ANN 规则启用 + 测试豁免

**Files:**
- Modify: `ruff.toml`
- Test: `make lint` + 全量 `pytest -m "unit or service"`

**Interfaces:**
- Consumes: Task 1-4 完成后生产代码 ANN 清零；Task 5 测试注释清零
- Produces: 门禁 `select = ["TID252", "ANN001", "ANN002", "ANN003"]` + `per-file-ignores` 豁免测试

- [ ] **Step 1: 改 ruff.toml**

```toml
[lint]
select = ["TID252", "ANN001", "ANN002", "ANN003"]

[lint.per-file-ignores]
"**/__init__.py" = ["TID252"]
"**/__main__.py" = ["TID252"]
# 测试文件豁免 ANN（pytest fixture 动态类型惯例）——仅生产代码强制注解
"**/tests/**" = ["ANN001", "ANN002", "ANN003"]
"**/conftest.py" = ["ANN001", "ANN002", "ANN003"]
"**/test_*.py" = ["ANN001", "ANN002", "ANN003"]
"**/fake_*.py" = ["ANN001", "ANN002", "ANN003"]
```

- [ ] **Step 2: 门禁验证**——`make lint` 零违规（生产已清零、测试已豁免）
- [ ] **Step 3: 全量回归**——`pytest -m "unit or service" -q --ignore=Module/workflow_engine/tests` 全绿（基线 2779）
- [ ] **Step 4: 提交** `chore(lint): ruff 启用 ANN001/002/003 门禁（生产强制，测试豁免）`

---

### Task 7: 文档更新 + 全量收尾

**Files:**
- Modify: `CLAUDE.md`（Conventions 补「参数类型注解要求」一条）、`docs/import-style.md` 或 `docs/readme-conventions.md`（如有相关节）、`todo.md`（如无条目跳过）
- Test: `make lint` + 全量

- [ ] **Step 1: 文档**——CLAUDE.md Conventions 补：函数参数必须类型注解（生产代码，`make lint` 强制 ANN001/002/003）；注释与 docstring 用中文；测试文件豁免注解
- [ ] **Step 2: 全量验证**——`make lint` 零违规 + `make test-normal` 全绿 + `make gen-types`/`make check-types` 无漂移（frontend_contract 若有类型触碰）
- [ ] **Step 3: 提交** `docs: 风格约定——参数注解 + 中文注释要求`

---

### Task 8: 最终 review + 文档 commit 整理

- [ ] **Step 1: 代码 review**——对全部提交做最终 review（正确性/一致性/遗漏）
- [ ] **Step 2: 修复 review 问题**
- [ ] **Step 3: 整理**——spec/plan 文档 commit 保留各 1 个（如拆了多个 doc commit 则 squash）
