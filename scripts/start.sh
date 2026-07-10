#!/usr/bin/env bash
# Elysia Agent — 一键启动
# 用法: bash scripts/start.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# ── 检测 Python 环境 ───────────────────────────────────────────────────────

PYTHON=""
if command -v conda &> /dev/null && conda env list 2>/dev/null | grep -q "^Elysia "; then
    PYTHON="conda run -n Elysia python"
elif command -v python3 &> /dev/null; then
    PYTHON="python3"
else
    echo "错误: 未找到 Python。请先运行 bash scripts/setup.sh"
    exit 1
fi

echo "=== Elysia Agent ==="

# ── 1. 确保数据库在运行 ────────────────────────────────────────────────────

echo "→ 数据库..."
if command -v docker &> /dev/null; then
    docker compose up -d 2>/dev/null || echo "  跳过（Docker 未运行？）"
else
    echo "  跳过（Docker 未安装）"
fi

# ── 2. 快速预检 ─────────────────────────────────────────────────────────────

echo "→ 预检..."
$PYTHON app.py --check || true

# ── 3. 启动服务 ────────────────────────────────────────────────────────────

echo "→ 启动服务 (http://localhost:8765)..."
echo ""
$PYTHON app.py
