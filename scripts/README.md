# Scripts 工具脚本

> 文档导航：[项目文档](../README.md)

项目根目录下的工具脚本集合，覆盖安装、启动、预检、模型下载和数据库清理。

## 脚本一览

| 脚本 | 用途 | 运行方式 |
|------|------|----------|
| `setup.sh` | 一键安装（conda 环境 + pip 依赖 + 嵌入模型下载） | `bash scripts/setup.sh` |
| `start.sh` | 一键启动（conda 激活 → 数据库检查 → 预检 → 启动服务） | `bash scripts/start.sh` |
| `check.sh` | 环境预检（conda/python 版本 + 依赖 + 配置文件） | `bash scripts/check.sh` |
| `download_model.py` | 嵌入模型下载（BAAI/bge-large-zh-v1.5，约 1.3GB） | `conda run -n Elysia python scripts/download_model.py` |
| `clean_db.py` | 数据库清理（清空 MongoDB + Milvus + 本地持久化文件） | `conda run -n Elysia python scripts/clean_db.py` |

## clean_db.py

一键清理 Elysia Agent 所有数据库内容，用于测试环境重置。

### 清理范围

| 存储 | 目标 | 方法 |
|------|------|------|
| MongoDB (`default`) | `sessions`、`message_archive`、`token_usage` | `delete_many({})` 清空文档，保留 indexes |
| Milvus | `long_term_memory` | drop 后自动重建（dimension=1024, IVF_FLAT） |
| 本地文件 | `data/session_default.json`、`data/token_usage_pending.jsonl`、`data/scheduler_checkpoint.json`、`data/cache/vision/*.json` | 逐个删除 |

### CLI 参数

```
usage: clean_db.py [-h] [-f] [--mongo-only] [--milvus-only] [--files-only] [--dry-run]

options:
  -h             显示帮助
  -f, --force    跳过确认提示，直接执行
  --mongo-only   只清理 MongoDB
  --milvus-only  只清理 Milvus
  --files-only   只清理本地 data/ 文件
  --dry-run      仅检查数据库连接，不实际清理
```

### 使用示例

```bash
# 清理全部（需输入 yes 确认）
conda run -n Elysia python scripts/clean_db.py

# 跳过确认，直接执行
conda run -n Elysia python scripts/clean_db.py -f

# 只清理 MongoDB
conda run -n Elysia python scripts/clean_db.py --mongo-only

# 检查连接但不执行清理
conda run -n Elysia python scripts/clean_db.py --dry-run
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/testing.md](../docs/testing.md) | 测试指南（测试流程中常用 `clean_db.py` 重置数据库） |
| [docs/architecture.md](../docs/architecture.md) | 完整架构（数据库存储设计、数据流） |
