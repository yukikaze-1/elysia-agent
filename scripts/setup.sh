#!/usr/bin/env bash
# Elysia Agent — 一键安装脚本
# 用法: bash scripts/setup.sh

set -euo pipefail

# 确保工作目录是项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo ""
echo -e "${BOLD}=== Elysia Agent 一键安装 ===${NC}"
echo ""

# ── Step 1: 检测 Python 环境 ──────────────────────────────────────────────

echo -e "${BOLD}Step 1: 检测 Python 环境${NC}"

PYTHON=""

if command -v conda &> /dev/null; then
    echo "  检测到 conda"

    # 检查 Elysia 环境是否已存在
    if conda env list 2>/dev/null | grep -q "^Elysia "; then
        echo -e "  ${GREEN}✓${NC} conda 环境 'Elysia' 已存在，跳过创建"
    else
        echo -n "  创建 conda 环境 'Elysia' (python=3.12)? [Y/n] "
        read -r answer
        if [[ "$answer" =~ ^[Nn]$ ]]; then
            echo "  跳过。将使用系统 python3。"
        else
            echo "  正在创建 conda 环境..."
            # python=3.12：3.13 下 openwakeword/tflite-runtime 等无预编译 wheel，
            # typing.TypedDict 在 Pydantic 2.10+ 也要求 Python>=3.12（N5095 部署实录）
            conda create -n Elysia python=3.12 -y
            echo -e "  ${GREEN}✓${NC} conda 环境已创建"
        fi
    fi

    if conda env list 2>/dev/null | grep -q "^Elysia "; then
        PYTHON="conda run -n Elysia python"
    fi
fi

if [[ -z "$PYTHON" ]]; then
    # 无 conda，用系统 python3
    if command -v python3 &> /dev/null; then
        PYTHON="python3"
        PY_VER=$($PYTHON --version 2>&1 | awk '{print $2}')
        MAJOR=$(echo "$PY_VER" | cut -d. -f1)
        MINOR=$(echo "$PY_VER" | cut -d. -f2)
        if [[ "$MAJOR" -lt 3 ]] || [[ "$MAJOR" -eq 3 && "$MINOR" -lt 11 ]]; then
            echo -e "  ${RED}✗${NC} Python $PY_VER — 需要 >= 3.11"
            exit 1
        fi
        echo -e "  ${GREEN}✓${NC} 系统 $PYTHON ($PY_VER)"
    else
        echo -e "  ${RED}✗${NC} 未找到 conda 或 python3，请先安装 Python >= 3.11"
        exit 1
    fi
fi

# ── Step 2: pip install ────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 2: pip install -r requirements.txt${NC}"
$PYTHON -m pip install -r requirements.txt
echo -e "${GREEN}✓${NC} pip 依赖安装完成"

# ── Step 3: 前端构建（F3：backend-only 默认；--with-frontend 才构建 v1 UI）───

echo ""
echo -e "${BOLD}Step 3: 前端构建 (Vue 3 + Vite)${NC}"

# backend-only 默认（Electron/后端部署无 Node 也能安装）；显式 --with-frontend /
# --legacy-ui 才构建 v1 UI（用 npm ci，干净安装）。缺 Node 且未显式请求 → 跳过，不报错。
BUILD_FRONTEND=false
for arg in "$@"; do
    if [[ "$arg" == "--with-frontend" || "$arg" == "--legacy-ui" ]]; then
        BUILD_FRONTEND=true
    fi
done

if ! $BUILD_FRONTEND; then
    echo -e "  ${YELLOW}→${NC} 跳过前端构建（backend-only）——如需 v1 UI 用: bash scripts/setup.sh --with-frontend"
elif ! command -v node &> /dev/null || ! command -v npm &> /dev/null; then
    echo -e "  ${RED}✗${NC} --with-frontend 需要 Node.js >= 18 + npm——请先安装，或去掉该 flag 用 backend-only"
    exit 1
else
    NODE_VER=$(node --version | sed 's/v//' | cut -d. -f1)
    if [[ "$NODE_VER" -lt 18 ]]; then
        echo -e "  ${RED}✗${NC} Node.js >= 18  (当前 $(node --version))"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} Node.js $(node --version)"
    echo -n "  正在 npm ci (clean install)..."
    cd frontend && npm ci --silent 2>&1 && cd ..
    echo -e "  ${GREEN}✓${NC} npm ci 完成"
    echo -n "  正在 npm run build..."
    cd frontend && npm run build --silent 2>&1 && cd ..
    echo -e "  ${GREEN}✓${NC} 前端构建完成 → static/"
fi

# ── Step 4: 配置 .env ──────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 3: 配置 .env${NC}"
if [[ -f ".env" ]]; then
    echo -e "  ${GREEN}✓${NC} .env 已存在，跳过"
else
    cp .env.example .env
    echo -e "  ${YELLOW}→${NC} .env 已创建，请编辑填入 API key："
    echo -e "     ${BOLD}DEEPSEEK_API_KEY${NC} (必需)"
    echo "  其他可选配置见 .env.example 中的注释。"
fi

# ── Step 4b: 数据库凭据（P1-19，docker compose 必需）──────────────────────

echo ""
echo -e "${BOLD}Step 3b: 数据库凭据（docker compose）${NC}"

# 生成随机十六进制密码（openssl 不可用时退回 python secrets）
_rand_hex() {
    if command -v openssl &> /dev/null; then
        openssl rand -hex 24
    else
        $PYTHON -c "import secrets; print(secrets.token_hex(24))"
    fi
}

# 若 .env 缺失某键则补写；默认值非空时用默认值，为空时生成随机密码
_ensure_env() { # $1=var_name  $2=default_value
    local name=$1 default_value=$2
    if ! grep -qE "^${name}=" .env; then
        if [[ -n "$default_value" ]]; then
            echo "${name}=${default_value}" >> .env
        else
            echo "${name}=$(_rand_hex)" >> .env
        fi
        echo -e "  ${GREEN}✓${NC} 已生成 ${name}"
    else
        echo -e "  ${GREEN}✓${NC} ${name} 已存在"
    fi
}

# Mongo/MinIO/Milvus 服务端凭据（compose 用 ${VAR:?} 强制引用，缺一即拒启）
_ensure_env MONGO_INITDB_ROOT_USERNAME elysia
_ensure_env MONGO_INITDB_ROOT_PASSWORD ""
_ensure_env MINIO_ROOT_USER elysia-minio
_ensure_env MINIO_ROOT_PASSWORD ""
_ensure_env MILVUS_ROOT_PASSWORD ""

# 读取 .env 中的实际值，派生应用侧连接串（保持 root:<密码> 与 Mongo URI 一致）
_MONGO_USER="$(grep -E "^MONGO_INITDB_ROOT_USERNAME=" .env | tail -1 | cut -d= -f2-)"
_MONGO_PW="$(grep -E "^MONGO_INITDB_ROOT_PASSWORD=" .env | tail -1 | cut -d= -f2-)"
_MILVUS_PW="$(grep -E "^MILVUS_ROOT_PASSWORD=" .env | tail -1 | cut -d= -f2-)"

# MONGODB_URI：缺失（含被 .env.example 注释掉）时补写带认证连接串；仅当仍是
# "无凭据 localhost 默认值"时改写。自定义（远程/已有认证）URI 不做修改。
if ! grep -qE "^MONGODB_URI=.+" .env; then
    echo "MONGODB_URI=mongodb://${_MONGO_USER}:${_MONGO_PW}@127.0.0.1:27017/default?authSource=admin" >> .env
    echo -e "  ${GREEN}✓${NC} MONGODB_URI 已补写为带认证连接串"
elif grep -qE "^MONGODB_URI=mongodb://(localhost|127\.0\.0\.1):27017$" .env; then
    sed -i "s|^MONGODB_URI=.*|MONGODB_URI=mongodb://${_MONGO_USER}:${_MONGO_PW}@127.0.0.1:27017/default?authSource=admin|" .env
    echo -e "  ${GREEN}✓${NC} MONGODB_URI 已改写为带认证连接串"
fi

# MILVUS_TOKEN：缺失或为空时补写 root:<milvus 密码>
if ! grep -qE "^MILVUS_TOKEN=.+" .env; then
    if grep -qE "^MILVUS_TOKEN=" .env; then
        sed -i "s|^MILVUS_TOKEN=.*|MILVUS_TOKEN=root:${_MILVUS_PW}|" .env
    else
        echo "MILVUS_TOKEN=root:${_MILVUS_PW}" >> .env
    fi
    echo -e "  ${GREEN}✓${NC} MILVUS_TOKEN 已补写为 root:<密码>"
fi

# ── Step 5: 嵌入模型 ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 4: 嵌入模型${NC}"
# review 复核 #11：不再按「目录存在」跳过——恒调用 download_model.py（内部
# cached_model_valid 做 manifest + sha256 校验：已缓存且合法秒过；目录存在但
# 损坏/不完整/被篡改时重新下载）。此前仅查目录会跳过校验，损坏缓存被当成功。
echo -e "${YELLOW}→${NC} 校验/准备嵌入模型（约 1.3GB，默认官方源 + 固定 revision + hash 校验；镜像需显式 HF_MIRROR opt-in；已缓存且校验通过则秒过）..."
$PYTHON scripts/download_model.py
if [[ $? -ne 0 ]]; then
    echo -e "  ${RED}✗${NC} 嵌入模型下载/校验失败"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} 嵌入模型就绪"

# ── Step 6: 创建目录 ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 5: 创建目录${NC}"
mkdir -p log/ data/
echo -e "${GREEN}✓${NC} log/ data/ 已就绪"

# ── Step 7: 环境预检 ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 6: 环境预检${NC}"
CHECK_EXIT=0
$PYTHON app.py --check || CHECK_EXIT=$?

# ── Step 8: 下一步 ─────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}=== 安装完成 ===${NC}"
echo ""
echo "启动服务:"
echo "  bash scripts/start.sh    # 一键启动 (http://localhost:8765)"
echo ""
echo "数据库（已自动生成凭据到 .env）:"
echo "  docker compose --profile dev up -d            # 开发（宿主端口仅 127.0.0.1）"
echo "  docker compose --profile production up -d     # 生产（不发布管理端口）"
echo ""
echo "其他:"
echo "  交互模式:  $PYTHON app.py"
echo "  全量预检:  $PYTHON app.py --check --full"

exit $CHECK_EXIT
