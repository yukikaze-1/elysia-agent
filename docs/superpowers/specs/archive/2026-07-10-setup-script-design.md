# 一键安装脚本 (setup.sh)

## 动机

当前部署流程分散在多个步骤中：conda 建环境、pip 安装、手动配 .env、手动下载嵌入模型、手动创建目录。新手首次部署容易遗漏步骤。

目标：`bash scripts/setup.sh` 一键完成从零到可运行。

## 设计

### 文件

| 文件 | 动作 | 说明 |
|------|------|------|
| `scripts/setup.sh` | **新建** | 一键安装脚本 |
| `scripts/download_model.py` | **新建** | 嵌入模型下载工具 |
| `README.md` | **修改** | 快速开始改为 `bash scripts/setup.sh` |

### setup.sh 流程

```
=== Elysia Agent 一键安装 ===

Step 1: 检测 Python 环境
  - 优先检测 conda，找到则询问是否创建 'Elysia' 环境 (python=3.13)
  - 无 conda 则用系统 python3，检查 >= 3.11
  - 已有 Elysia 环境 → 跳过创建
  - 保存检测到的解释器到变量 PYTHON（conda 则 "conda run -n Elysia python"，系统则 "python3"）

Step 2: pip install -r requirements.txt
  - 使用 $PYTHON -m pip install -r requirements.txt

Step 3: 配置 .env
  - 不存在 → cp .env.example .env，提示用户编辑填入 DEEPSEEK_API_KEY
  - 已存在 → 跳过

Step 4: 嵌入模型（可选，仅 huggingface provider 需要；使用 OpenAI embedding 可跳过）
  - 检测 .model_cache/ 或 ~/.cache/huggingface/ 中是否有 BAAI/bge-large-zh-v1.5
  - 不存在 → 提示运行 $PYTHON scripts/download_model.py（约 1.3GB，需网络）
  - 不阻塞 setup，用户可跳过

Step 5: 创建必要目录
  - mkdir -p log/ data/

Step 6: 环境预检
  - 运行 $PYTHON app.py --check，验证配置完整性

Step 7: 打印下一步
  - 如何启动服务
  - 可选：配置 TTS/STT/数据库
  - 可选：下载嵌入模型
```

### download_model.py

```python
"""下载 sentence-transformers 嵌入模型到 .model_cache/。

用法: python scripts/download_model.py [--model BAAI/bge-large-zh-v1.5]

已存在则跳过。需要先 pip install -r requirements.txt。
"""
```

用 `SentenceTransformer(model_name, cache_folder=cache_dir)` 触发下载。默认模型 `BAAI/bge-large-zh-v1.5`。`--model` 可指定其他模型。

### README 变更

快速开始第 1-2 步合并为:

```bash
bash scripts/setup.sh
```

其余步骤（启动、可选服务）不变。

## 成功标准

1. 全新 Linux 机器上运行 `bash scripts/setup.sh` → 完成所有步骤 → `app.py --check` 通过
2. 已有 conda 环境 → 再次运行跳过创建，不重复安装
3. `.env` 已存在 → 跳过，不覆盖
4. 嵌入模型已缓存 → 跳过下载提示
5. 无 conda 的系统 → 用系统 python3，正常工作
