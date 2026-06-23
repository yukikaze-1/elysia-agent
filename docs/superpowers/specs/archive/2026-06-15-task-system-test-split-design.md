# task_system 测试拆分设计

## 背景

`Module/task_system/tests/core/` 下有 10 个测试文件，其中 `test_executor.py` 和 `test_delegation_preparer.py` 两个文件混合了单元测试和集成测试（`@pytest.mark.integration`）。虽然已通过 marker 支持 `pytest -m "not integration"` 筛选运行，但单元测试和集成测试混在同一文件不利于维护和清晰度。

目标：将混合文件拆分为独立的单元测试文件和集成测试文件，遵循已有的 `test_planner.py`（集成）+ `test_planner_unit.py`（单元）命名约定。

## 设计

### 命名约定

- **集成测试**：保留在原文件名（如 `test_executor.py`）
- **单元测试**：移到新文件 `*_unit.py`（如 `test_executor_unit.py`）

此约定已在 `test_planner.py` / `test_planner_unit.py` 中确立，本次拆分是对该模式的扩展。

### 拆分操作

#### 1. `test_executor.py` → `test_executor.py` + `test_executor_unit.py`

**当前状态**（479 行）：
- 第 1–356 行：单元测试（mock LLM / ToolRegistry），约 15 个测试类
- 第 358–479 行：2 个 `@pytest.mark.integration` 集成测试（需要 DEEPSEEK_API_KEY + ToolManager）

**拆分后**：

| 文件 | 内容 |
|------|------|
| `test_executor_unit.py` | 单元测试的 fixtures（mock_artifact_store, mock_llm_client, mock_tool_registry, mock_infra, executor, sample_node, sample_tools）+ 全部单元测试类 |
| `test_executor.py` | 集成测试 fixtures（_build_integration_executor）+ 2 个 `@pytest.mark.integration` 测试 |

#### 2. `test_delegation_preparer.py` → `test_delegation_preparer.py` + `test_delegation_preparer_unit.py`

**当前状态**（1079 行）：
- 第 1–687 行：单元测试（mock `_call_llm`），约 30 个测试方法
- 第 689–1079 行：8 个 `@pytest.mark.integration` 集成测试（需要 DEEPSEEK_API_KEY）

**拆分后**：

| 文件 | 内容 |
|------|------|
| `test_delegation_preparer_unit.py` | 单元测试 fixtures（sample_tools, sample_session_messages, preparer）+ 全部单元测试类 |
| `test_delegation_preparer.py` | 集成测试 fixtures（_make_integration_tools）+ 8 个 `@pytest.mark.integration` 测试 |

### 不变内容

以下文件保持原样：

| 文件 | 类型 | 行数 |
|------|------|------|
| `test_planner.py` | 纯集成 | 119 |
| `test_planner_unit.py` | 纯单元 | 288 |
| `test_evaluator.py` | 纯单元 | 544 |
| `test_orchestrator.py` | 纯单元 | 679 |
| `test_graph.py` | 纯单元 | 520 |
| `test_sub_agent.py` | 纯单元 | 432 |
| `test_checkpoint.py` | 纯单元 | 169 |
| `test_mailbox.py` | 纯单元 | 183 |

### 目录结构

```
Module/task_system/tests/core/
├── test_checkpoint.py
├── test_delegation_preparer.py          # 纯集成（8 个 @pytest.mark.integration）
├── test_delegation_preparer_unit.py     # 新建：纯单元
├── test_evaluator.py
├── test_executor.py                     # 纯集成（2 个 @pytest.mark.integration）
├── test_executor_unit.py               # 新建：纯单元
├── test_graph.py
├── test_mailbox.py
├── test_orchestrator.py
├── test_planner.py                      # 纯集成
├── test_planner_unit.py
└── test_sub_agent.py
```

文件数量：10 → 12

### 原则

1. **仅移动代码，不修改测试逻辑**——只改变测试函数所在的文件，不改变测试内容
2. **导入自动适配**——移入新文件的代码需要添加必要的 imports（pytest, unittest.mock, 被测模块等）
3. **不再做额外改进**——不抽取共享 fixtures、不注册 pytest marker、不修改测试内容

## 验证

拆分完成后运行以下命令确认：

```bash
# 单元测试全部通过（不需要 DEEPSEEK_API_KEY）
python -m pytest Module/task_system/tests/ -m "not integration" -v

# 集成测试在有 API key 时通过
python -m pytest Module/task_system/tests/ -m "integration" -v

# 全部测试
python -m pytest Module/task_system/tests/ -v
```
