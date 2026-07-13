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
