# 一键安装脚本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `bash scripts/setup.sh` 一键完成从零到可运行的部署。

**Architecture:** `scripts/setup.sh` 纯 bash 脚本（依赖 conda/pip/python3），`scripts/download_model.py` 用 sentence-transformers 下载嵌入模型。README 快速开始简化为一步。

**Tech Stack:** bash + Python (sentence-transformers)

## Global Constraints

- 使用 `$PYTHON` 变量统一持有解释器命令（conda 环境用 `conda run -n Elysia python`，系统用 `python3`）
- 每一步幂等（已存在则跳过）
- `.env` 不存在才复制，已存在不覆盖
- 嵌入模型下载提示注明"仅 huggingface provider 需要，OpenAI embedding 可跳过"

---

### Task 1: Create `scripts/download_model.py`

**Files:**
- Create: `scripts/download_model.py`

**Interfaces:**
- Produces: CLI tool `python download_model.py [--model MODEL_NAME]`

- [ ] **Step 1: Create `scripts/download_model.py`**

```python
#!/usr/bin/env python3
"""下载 sentence-transformers 嵌入模型到 .model_cache/。

用法:
    python scripts/download_model.py                        # 下载默认模型
    python scripts/download_model.py --model BAAI/bge-m3   # 指定模型

已存在则跳过。需要先 pip install -r requirements.txt。
"""

import argparse
from pathlib import Path


def find_project_root() -> Path:
    """向上查找项目根目录（包含 requirements.txt 的目录）。"""
    here = Path(__file__).resolve().parent
    for d in [here] + list(here.parents):
        if (d / "requirements.txt").exists():
            return d
    return here.parent  # fallback


def model_exists(model_name: str, cache_dir: Path) -> bool:
    """检查模型是否已在 HuggingFace Hub 缓存中。

    SentenceTransformer 使用 HuggingFace Hub 缓存结构:
    {cache_dir}/models--ORG--MODEL/snapshots/{hash}/config.json
    """
    hub_name = "models--" + model_name.replace("/", "--")
    snapshots_dir = cache_dir / hub_name / "snapshots"
    if snapshots_dir.exists() and any(snapshots_dir.iterdir()):
        return True
    return False


def download_model(model_name: str, cache_dir: Path) -> bool:
    """下载 sentence-transformers 模型。返回 True 表示成功。"""
    print(f"正在下载模型 {model_name} 到 {cache_dir} ...")
    print("（约 1.3GB，可能需要几分钟）")
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(model_name, cache_folder=str(cache_dir))
        print(f"✓ 模型已下载: {cache_dir / model_name}")
        return True
    except Exception as e:
        print(f"✗ 下载失败: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="下载 sentence-transformers 嵌入模型到 .model_cache/"
    )
    parser.add_argument(
        "--model", type=str, default="BAAI/bge-large-zh-v1.5",
        help="模型名称 (默认: BAAI/bge-large-zh-v1.5)",
    )
    args = parser.parse_args()

    root = find_project_root()
    cache_dir = root / ".model_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if model_exists(args.model, cache_dir):
        print(f"✓ 模型已缓存: {cache_dir / args.model}")
        return

    ok = download_model(args.model, cache_dir)
    if ok:
        print()
        print(f"提示: 确保 MODEL_CACHE_DIR 指向 {cache_dir}")
        print("  或在项目根创建软链接: ln -s /path/to/model_cache .model_cache")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it runs (dry-run)**

```bash
python scripts/download_model.py --help
```

- [ ] **Step 3: Commit**

```bash
git add scripts/download_model.py
git commit -m "feat: add model download script for huggingface embedding"
```

---

### Task 2: Create `scripts/setup.sh`

**Files:**
- Create: `scripts/setup.sh`

- [ ] **Step 1: Create `scripts/setup.sh`**

```bash
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
CONDA_FOUND=false

if command -v conda &> /dev/null; then
    CONDA_FOUND=true
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
if [[ -d ".model_cache/models--BAAI--bge-large-zh-v1.5" ]]; then
    CACHE_DIR=".model_cache"
elif [[ -d "$HOME/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5" ]]; then
    CACHE_DIR="$HOME/.cache/huggingface"
fi

if [[ -n "$CACHE_DIR" ]]; then
    echo -e "  ${GREEN}✓${NC} 嵌入模型已缓存 ($CACHE_DIR)"
else
    echo -e "  ${YELLOW}→${NC} 嵌入模型未缓存（仅 huggingface provider 需要；使用 OpenAI embedding 可跳过）"
    echo -e "  下载命令: ${BOLD}$PYTHON scripts/download_model.py${NC}  (约 1.3GB，需网络)"
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
echo "  $PYTHON app.py --server              # WebSocket 模式 (http://localhost:8765)"
echo "  $PYTHON app.py --server --tts --stt  # 含语音"
echo "  $PYTHON app.py                       # 交互模式"
echo ""
echo "可选:"
echo "  嵌入模型:  $PYTHON scripts/download_model.py"
echo "  Milvus:    docker run -d --name milvus -p 19530:19530 milvusdb/milvus:latest"
echo "  MongoDB:   配置 .env 中 MONGODB_URI"
echo "  全量预检:  $PYTHON app.py --check --full"

exit $CHECK_EXIT
```

- [ ] **Step 2: Verify script runs**

```bash
bash scripts/setup.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/setup.sh
git commit -m "feat: add one-click setup script"
```

---

### Task 3: Update README.md

**Files:**
- Modify: `README.md` (lines 11-64, quick start section)

- [ ] **Step 1: Replace quick start steps 1-2 with setup.sh**

Replace the existing "### 1. 环境准备" through "### 2. 配置" sections:

```
### 1. 环境准备

conda create -n Elysia python=3.13 -y
conda activate Elysia
pip install -r requirements.txt

### 2. 配置

cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY

# 环境预检
python app.py --check
# 或 make check
```

With:

```
### 1. 一键安装

bash scripts/setup.sh

脚本会自动: 检测/创建 conda 环境 → pip install → 配置 .env → 环境预检。
```

Steps 3 (嵌入模型缓存) and 4 (启动) keep their numbers but step 5 adjusts:

"### 3. 启动" (was "### 4. 启动") — content unchanged
"### 4. 可选：嵌入模型缓存" → remove (now handled by setup.sh step 4 / download_model.py)
"### 5. 可选：数据库" — content unchanged

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: simplify quick start to one-step setup.sh"
```

---

### Verification

```bash
# 1. setup.sh 幂等性 — 多次运行不报错
bash scripts/setup.sh
bash scripts/setup.sh

# 2. download_model.py help
python scripts/download_model.py --help

# 3. 最终预检通过
python app.py --check
```
