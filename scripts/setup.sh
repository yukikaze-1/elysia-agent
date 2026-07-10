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
        echo -n "  创建 conda 环境 'Elysia' (python=3.13)? [Y/n] "
        read -r answer
        if [[ "$answer" =~ ^[Nn]$ ]]; then
            echo "  跳过。将使用系统 python3。"
        else
            echo "  正在创建 conda 环境..."
            conda create -n Elysia python=3.13 -y
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

# ── Step 3: 配置 .env ──────────────────────────────────────────────────────

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

# ── Step 4: 嵌入模型 ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 4: 嵌入模型${NC}"
CACHE_DIR=""
if [[ -d ".model_cache/models--BAAI--bge-large-zh-v1.5" || -d ".model_cache/BAAI/bge-large-zh-v1.5" ]]; then
    CACHE_DIR=".model_cache"
elif [[ -d "$HOME/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5" || -d "$HOME/.cache/huggingface/hub/BAAI/bge-large-zh-v1.5" ]]; then
    CACHE_DIR="$HOME/.cache/huggingface"
fi

if [[ -n "$CACHE_DIR" ]]; then
    echo -e "  ${GREEN}✓${NC} 嵌入模型已缓存 ($CACHE_DIR)"
else
    echo -e "  ${YELLOW}→${NC} 嵌入模型未缓存，正在下载（约 1.3GB，默认使用 hf-mirror.com 镜像）..."
    $PYTHON scripts/download_model.py
    if [[ $? -ne 0 ]]; then
        echo -e "  ${RED}✗${NC} 嵌入模型下载失败"
        exit 1
    fi
fi

# ── Step 5: 创建目录 ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 5: 创建目录${NC}"
mkdir -p log/ data/
echo -e "${GREEN}✓${NC} log/ data/ 已就绪"

# ── Step 6: 环境预检 ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Step 6: 环境预检${NC}"
CHECK_EXIT=0
$PYTHON app.py --check || CHECK_EXIT=$?

# ── Step 7: 下一步 ─────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}=== 安装完成 ===${NC}"
echo ""
echo "启动服务:"
echo "  bash scripts/start.sh    # 一键启动 (http://localhost:8765)"
echo ""
echo "其他:"
echo "  交互模式:  $PYTHON app.py"
echo "  全量预检:  $PYTHON app.py --check --full"

exit $CHECK_EXIT
