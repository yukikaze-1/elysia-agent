#!/usr/bin/env bash
# Elysia Agent — 一键启动
# 用法: bash scripts/start.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$SCRIPT_DIR/.."

# ── 检测 Python 环境 ───────────────────────────────────────────────────────

PYTHON=""
if command -v conda &> /dev/null && conda env list 2>/dev/null | grep -q "^Elysia "; then
    # CR-02：**直接 exec 环境内的 python，不经 `conda run`**。`conda run` 是中间
    # 进程且实测**不转发 SIGTERM**（docs/review/2026-09-12-code-review-ledger.md §6.1-#1），
    # 于是 `kill -TERM` / `docker stop` / `systemctl stop` 只杀 conda，python 收不到
    # 信号 → 内核默认动作硬杀 → Agent.stop() 的 20 步清理一步不跑。
    # 解析出环境前缀后 `exec` 掉 shell，SIGTERM 直达 python 本身。
    _ENV_PREFIX="$(conda run -n Elysia python -c 'import sys; print(sys.prefix)' 2>/dev/null || true)"
    if [[ -n "$_ENV_PREFIX" && -x "$_ENV_PREFIX/bin/python" ]]; then
        # CONDA_PREFIX 必须显式导出：app.py 用它定位 conda 的 SSL 证书
        # （`$CONDA_PREFIX/ssl/cert.pem`），绕过 `conda run` 后该变量不再自动设置。
        CONDA_PREFIX="$_ENV_PREFIX"
        export CONDA_PREFIX
        export PATH="$_ENV_PREFIX/bin:$PATH"
        PYTHON="$_ENV_PREFIX/bin/python"
    else
        PYTHON="conda run -n Elysia --no-capture-output python"
        echo "  ⚠ 无法解析 Elysia 环境前缀——回退 conda run（该路径下 SIGTERM 不会透传到 python）"
    fi
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
        echo "  ✓ 前端构建完成（聊天界面默认停用，SERVE_CHAT=1 恢复；/debug 仪表盘可用）"
    else
        echo "  ✗ --with-frontend 需要 npm——请先安装 Node.js/npm，或去掉该 flag 用 backend-only"
        exit 1
    fi
elif [[ -f "static/index.html" || -f "static/debug.html" ]]; then
    # 两个页面是**各自独立**的构建产物：`serve_chat=False` 时只有 /debug 有用，
    # 而 `/debug` 的路由仅在 static/debug.html 存在时注册（core/input/server.py）。
    # 旧文案只看 index.html 就断言「/debug 可用」——与后端横幅同一种谎报（2026-09-12 修）。
    _chat=$([[ -f "static/index.html" ]] && echo "有" || echo "无")
    _dbg=$([[ -f "static/debug.html" ]] && echo "有" || echo "无")
    echo "  ✓ 跳过前端构建（static/ 已存在）—— / 聊天页: ${_chat}，/debug 仪表盘: ${_dbg}"
    echo "    （缺哪个用 --with-frontend 构建；聊天界面默认停用，SERVE_CHAT=1 恢复）"
else
    echo "  ⚠ 跳过前端构建（backend-only，无 static/）——/ 与 /debug 均无 UI，仅 WS/REST/API 可用"
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
# CR-02：exec 让 python 取代本 shell —— 信号直达进程，不经中间层。
exec $PYTHON app.py
