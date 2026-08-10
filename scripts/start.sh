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
        docker compose --profile dev up -d milvus 2>/dev/null && echo "  ✓ Milvus 已启动" || echo "  ⚠ Milvus 启动失败"
    fi
    if ! $MONGODB_OK; then
        docker compose --profile dev up -d mongodb 2>/dev/null && echo "  ✓ MongoDB 已启动" || echo "  ⚠ MongoDB 启动失败"
    fi
else
    echo "  ⚠ Docker 未安装且数据库未运行"
fi

# ── 2. 构建前端（F3：backend-only 默认；--with-frontend 才构建 v1 UI）───

# 参数解析：--with-frontend 显式构建 v1 UI（需 npm）；--skip-preflight 显式降级跳过预检
WITH_FRONTEND=false
SKIP_PREFLIGHT=false
for arg in "$@"; do
    case "$arg" in
        --with-frontend) WITH_FRONTEND=true ;;
        --skip-preflight) SKIP_PREFLIGHT=true ;;
    esac
done

echo "→ 构建前端..."
if $WITH_FRONTEND; then
    if command -v npm &> /dev/null; then
        cd frontend && npm ci --silent 2>&1 && npm run build --silent 2>&1 && cd ..
        echo "  ✓ 前端构建完成"
    else
        echo "  ✗ --with-frontend 需要 npm——请先安装 Node.js/npm，或去掉该 flag 用 backend-only"
        exit 1
    fi
elif [[ -f "static/index.html" ]]; then
    echo "  ✓ 跳过前端构建（static/ 已存在）"
else
    echo "  ⚠ 跳过前端构建（backend-only，无 static/）——访问 / 将无 v1 UI，仅 WS/REST 可用"
fi

# ── 3. 快速预检（F3：失败默认非零退出；--skip-preflight 显式降级）───

if $SKIP_PREFLIGHT; then
    echo "  ⚠ 跳过预检（显式 --skip-preflight 降级）"
else
    echo "→ 预检..."
    if ! $PYTHON app.py --check; then
        echo "  ✗ 预检失败——配置/依赖有问题，拒绝启动。修复后重试；或显式 --skip-preflight 降级启动"
        exit 1
    fi
fi

# ── 4. 启动服务 ────────────────────────────────────────────────────────────

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
