# 风格整改设计：参数类型注解补齐 + 英文注释中文化

> **触发**：用户要求全面排查项目代码风格问题——「有些函数的参数没有类型注释。有些注释是英文。」。审计后确认两维度规模，经用户决策确定修复范围与门禁策略。
>
> **关联**：`CLAUDE.md`（代码注释用中文约定）、`ruff.toml`（现仅 TID252 门禁）、`docs/import-style.md`。

## 1. 审计数据（2026-08-08 实测）

### 1.1 参数缺类型注解（ruff ANN，`core/` + `Module/` + `frontend_contract/`）

| 范围 | 数量 | 说明 |
|---|---|---|
| 生产代码 ANN001/002/003 | **185** | ~55 文件。ANN001 普通参数 ~124、ANN003 `**kwargs` ~50、ANN002 `*args` ~11 |
| 测试文件 ANN001/002/003 | **~2100** | pytest fixture 动态类型，惯例不注解 |

生产代码集中文件：`core/input/session_routes.py`(14)、`Module/output/manager.py`(14)、`core/memory/session_manager.py`(10)、`Module/tool/__main__.py`(10)、`Module/output/protocol.py`(8)、`core/storage/attachment_release.py`(8)、`frontend_contract/gen_ts.py`(7)、`Module/llm/openai_compat.py`(6)、`core/input/ws_handler.py`(6)。

⚠️ **关键发现**：现 lint 门禁仅 `select = ["TID252"]`（禁相对导入），ANN 从未启用——185 处是潜伏风格债，非回归。

### 1.2 英文注释/docstring

| 类型 | 数量 | 分布 |
|---|---|---|
| 纯英文 `#` 注释行（行内无中文字符） | **447**（生产 168 + 测试 279） | 生产 ~45 文件 + 测试 ~50 文件 |
| 英文 docstring（首行英文 prose） | **141** | 生产 ~30 文件，`core/input/` 系最集中 |

**中英混合注释**（中文嵌英文技术术语，如 `# JSONL fallback — MongoDB 不可用`）是项目既有风格，**不算违规，不改**。

## 2. 用户决策（AskUserQuestion，2026-08-08）

| # | 决策点 | 选择 | 理由 |
|---|---|---|---|
| D1 | 参数注解修复范围 | **仅生产代码**（185 处） | pytest fixture 动态类型，补注解收益低噪音大；测试多为遗留 |
| D2 | 英文注释/docstring 修复范围 | **全部翻成中文**（447 + 141） | 纯文本改写，零行为风险；符合 CLAUDE.md「代码注释用中文」 |
| D3 | ANN 规则加入 lint 门禁 | **加门禁** | 防回归；用 `per-file-ignores` 豁免测试文件，生产强制、测试豁免 |

## 3. 修复机制

### 3.1 注解
- `**kwargs: Any` / `*args: Any`（缺 `Any` 加 `from typing import Any`，绝对导入）。
- 方法 `self`/`cls` 不注解；带默认值参数给类型。
- 类型按函数实际语义推断（`str`/`dict`/`Optional[X]`/`Any` 等），不引入 `cast`、不改行为。

### 3.2 注释/docstring
- 纯英文 `#` 行 + 英文 docstring 首行 → 中文；**保留**代码标识符、URL、`# noqa`/`# type: ignore`/`# pragma`/shebang/编码声明。
- 中英混合行不动。

### 3.3 门禁
```toml
[lint]
select = ["TID252", "ANN001", "ANN002", "ANN003"]
[lint.per-file-ignores]
"**/__init__.py" = ["TID252"]
"**/__main__.py" = ["TID252"]
"**/tests/**" = ["ANN001", "ANN002", "ANN003"]
"**/conftest.py" = ["ANN001", "ANN002", "ANN003"]
"**/test_*.py" = ["ANN001", "ANN002", "ANN003"]
"**/fake_*.py" = ["ANN001", "ANN002", "ANN003"]
```
生产代码强制注解；测试（pytest fixture 惯例）+ `__init__`/`__main__`（既有豁免）不强制。

## 4. 执行策略

按目录分簇并行派发 subagent（每簇一个，文件互不重叠）：
- **Task 1**: `core/input/` + `core/agent/` + `core/adapter/`
- **Task 2**: `core/` 其余生产文件
- **Task 3**: `Module/` 全部生产文件
- **Task 4**: `frontend_contract/`
- **Task 5**: 测试文件英文注释（不改注解）
- **Task 6**: ruff.toml 门禁
- **Task 7**: 文档更新 + 全量验证
- **Task 8**: 最终 review + doc commit 整理

每簇验收：`ruff check <scope> --select ANN001,ANN002,ANN003` 清零 + 覆盖 tests 全绿 + 英文注释清零。

## 5. 风险与边界

- **零行为变更**：注解与注释是纯文本层改动，不改逻辑。风险点仅 import 错误/语法错误——靠 ruff + pytest 兜底。
- **`Module/workflow_engine/tests` 排除**：该目录测试较慢/独立，不在 `test-quick` 门禁内（沿用既有分层）；生产文件仍修。
- **翻译准确性**：subagent 翻译注释须保持语义，不确定时保留标识符查证代码。
