#!/usr/bin/env bash
# Elysia Agent — 一键启动
# 用法: bash scripts/start.sh
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

echo "=== Elysia Agent ==="

# ── 1. 确保数据库在运行 ────────────────────────────────────────────────────

echo "→ 数据库..."

_port_open() {
    timeout 1 bash -c "echo >/dev/tcp/localhost/$1" 2>/dev/null
}

MILVUS_OK=false
MONGODB_OK=false

if _port_open 19530; then
    echo "  ✓ Milvus 已运行 (localhost:19530)"
    MILVUS_OK=true
fi
if _port_open 27017; then
    echo "  ✓ MongoDB 已运行 (localhost:27017)"
    MONGODB_OK=true
fi

if $MILVUS_OK && $MONGODB_OK; then
    echo "  数据库均已运行，跳过"
elif command -v docker &> /dev/null; then
    if ! $MILVUS_OK; then
        docker compose up -d milvus 2>/dev/null && echo "  ✓ Milvus 已启动" || echo "  ⚠ Milvus 启动失败"
    fi
    if ! $MONGODB_OK; then
        docker compose up -d mongodb 2>/dev/null && echo "  ✓ MongoDB 已启动" || echo "  ⚠ MongoDB 启动失败"
    fi
else
    echo "  ⚠ Docker 未安装且数据库未运行"
fi

# ── 2. 快速预检 ─────────────────────────────────────────────────────────────

echo "→ 预检..."
$PYTHON app.py --check 2>/dev/null || true

# ── 3. 启动服务 ────────────────────────────────────────────────────────────

echo "→ 启动服务 (http://localhost:8765)..."
echo ""
echo "=================================================="
echo "  服务启动中，请稍候..."
echo "  就绪后访问: http://localhost:8765"
echo "  按 Ctrl+C 停止"
echo "=================================================="
echo ""

# 清除 ALL_PROXY（httpx 不支持 socks:// scheme，会炸）
export ALL_PROXY="" all_proxy=""
$PYTHON app.py
