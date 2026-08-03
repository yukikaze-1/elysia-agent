# check.sh — 独立环境预检脚本 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `scripts/check.sh`，为用户提供 `bash scripts/check.sh [--full]` 一键预检入口，与现有 `scripts/setup.sh` / `scripts/start.sh` 模式对齐。

**Architecture:** 单文件 shell 脚本，检测 conda/python3 环境后透传参数给 `app.py --check [--full]`，退出码透传。README 两处补充脚本调用入口。

**Tech Stack:** Bash（无外部依赖）

## Global Constraints

- 必须使用 `set -euo pipefail`（与 setup.sh/start.sh 一致）
- conda 必须使用 `--no-capture-output`（保证实时输出）
- 无彩色输出（与 start.sh 一致）
- 退出码透传 `app.py --check` 的返回值

---

### Task 1: 新增 `scripts/check.sh`

**Files:**
- Create: `scripts/check.sh`

**Interfaces:**
- Produces: `bash scripts/check.sh [--full]` — 可选参数透传给 `app.py --check [--full]`

- [ ] **Step 1: 创建 `scripts/check.sh`**

```bash
#!/usr/bin/env bash
# Elysia Agent — 环境预检
# 用法: bash scripts/check.sh [--full]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$SCRIPT_DIR/.."

# ── 检测 Python 环境 ───────────────────────────────────────────────────────

PYTHON=""
if command -v conda &> /dev/null && conda env list 2>/dev/null | grep -q "^Elysia "; then
    PYTHON="conda run -n Elysia --no-capture-output python"
elif command -v python3 &> /dev/null; then
    PYTHON="python3"
else
    echo "错误: 未找到 Python。请先运行 bash scripts/setup.sh"
    exit 1
fi

echo "=== Elysia Agent 环境预检 ==="
echo ""
echo "→ Python 环境..."
if [[ "$PYTHON" == conda* ]]; then
    echo "  ✓ conda Elysia"
else
    echo "  ✓ 系统 python3"
fi
echo ""

# ── 预检 ───────────────────────────────────────────────────────────────────

echo "→ 预检..."
CHECK_EXIT=0
$PYTHON app.py --check "$@" || CHECK_EXIT=$?

echo ""
if [[ $CHECK_EXIT -eq 0 ]]; then
    echo "✓ 预检通过"
else
    echo "⚠ 预检发现问题，详见上方输出"
fi

exit $CHECK_EXIT
```

- [ ] **Step 2: 设置可执行权限**

```bash
chmod +x scripts/check.sh
```

- [ ] **Step 3: 验证脚本语法**

```bash
bash -n scripts/check.sh
```
Expected: 无输出（语法正确）

- [ ] **Step 4: Commit**

```bash
git add scripts/check.sh
git commit -m "feat: add scripts/check.sh for standalone preflight check"
```

---

### Task 2: 更新 README.md — "高级用法" 章节

**Files:**
- Modify: `README.md` — "高级用法" 代码块

**Interfaces:**
- Consumes: `scripts/check.sh`（Task 1）

- [ ] **Step 1: 在 `--check --full` 附近添加脚本形式**

定位到 `python app.py --check --full` 行，在它下方插入脚本等效调用：

```bash
bash scripts/check.sh           # 快速预检
bash scripts/check.sh --full    # 环境预检（含连通性）
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add check.sh usage to advanced section"
```

---

### Task 3: 更新 README.md — "故障排查" 章节

**Files:**
- Modify: `README.md` — "故障排查" → "预检报错" 区块

**Interfaces:**
- Consumes: `scripts/check.sh`（Task 1）

- [ ] **Step 1: 将预检报错部分改为脚本优先**

把原来的 `python app.py --check --full` 替换为脚本形式：

```
**预检报错**
```bash
bash scripts/check.sh --full    # 完整预检，准确定位问题
python scripts/download_model.py  # 缺少嵌入模型
```
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: use check.sh in troubleshooting section"
```

---

### 完成验证

全部任务完成后，运行以下确认：

```bash
# 语法检查
bash -n scripts/check.sh

# 快速预检
bash scripts/check.sh
# Expected: 退出码 0（环境正常）或非 0（有问题），输出包含 "=== Elysia Agent 环境预检 ==="

# 全量预检
bash scripts/check.sh --full
# Expected: 同上，但包含连通性探测结果
```
