# Scripts 工具脚本

> 文档导航：[项目文档](../README.md)

项目根目录下的工具脚本集合，覆盖安装、启动、预检、模型下载和数据库清理。

## 脚本一览

| 脚本 | 用途 | 运行方式 |
|------|------|----------|
| `setup.sh` | 一键安装（conda 环境 + pip 依赖 + 嵌入模型下载） | `bash scripts/setup.sh` |
| `start.sh` | 一键启动（conda 激活 → 数据库检查 → 预检 → 启动服务） | `bash scripts/start.sh` |
| `check.sh` | 环境预检（conda/python 版本 + 依赖 + 配置文件） | `bash scripts/check.sh` |
| `download_model.py` | 嵌入模型下载（默认 `BAAI/bge-large-zh-v1.5`，固定 revision + sha256 校验，默认官方源） | `conda run -n Elysia python scripts/download_model.py` |
| `clean_db.py` | 数据库清理（清空 MongoDB + Milvus + 本地持久化文件） | `conda run -n Elysia python scripts/clean_db.py` |
| `check_compose_security.py` | Compose 安全策略门禁（P1-19）：数据服务端口仅 loopback、无默认凭据/空 token、生产不发布管理端口 | `conda run -n Elysia python scripts/check_compose_security.py` |
| `gen_contract_frames.py` | 契约层 golden frames 生成/漂移检测（9B-1 + 阶段4）：17 个 `ws-v2-*.jsonl`/`rest-*.jsonl`（WS 含阶段4 普通录音/听写/音频/服务切换，REST 含 Profile/TTS 正反例）；`--check` 漂移返回 1（`make check-types` 强制） | `conda run -n Elysia python scripts/gen_contract_frames.py` |
| `stage4_smoke.py` | 阶段 4 确定性契约 smoke（P1-01）：进程内 `create_app` + 真 agent(fake LLM) + STT/TTS 桩，覆盖 Profile/三语音模式/TTS 播放 5 场景；默认 stub 确定性、失败非零退出；`STAGE4_SMOKE_LIVE=1` 才接真实 provider（不进 CI） | `conda run -n Elysia python scripts/stage4_smoke.py` |

## clean_db.py

一键清理 Elysia Agent 所有数据库内容，用于测试环境重置。

### 清理范围

| 存储 | 目标 | 方法 |
|------|------|------|
| MongoDB (`default`) | `sessions`、`message_archive`、`token_usage`、`character_versions`、`character_l2_versions`（旧） | `delete_many({})` 清空文档，保留 indexes |
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

## download_model.py（供应链安全）

模型下载已做供应链固定（P1-18），防止镜像/上游 tag/本地缓存被替换后执行非预期代码：

- 默认走**官方 HuggingFace**，默认模型 `BAAI/bge-large-zh-v1.5` 固定不可变 commit
  revision，并维护所需文件的 sha256 manifest（单源 `Module/embedding/model_manifest.py`）。
- 第三方 mirror 只能**显式 opt-in**：`HF_MIRROR` 环境变量或 `--mirror` CLI；
  镜像内容仍按同一 manifest 校验 hash，内容不同会被拒绝。
- 下载先落 `.model_cache/.tmp` 临时目录，全部文件 sha256 校验通过后**原子切换**到
  最终快照；失败保留旧可用版本。
- 默认 `trust_remote_code=False`；未登记 manifest 的模型打印未固定警告。

```bash
# 官方源（默认，固定 revision + hash 校验）
conda run -n Elysia python scripts/download_model.py

# 显式 opt-in 镜像（仍校验相同 hash）
HF_MIRROR=https://hf-mirror.com conda run -n Elysia python scripts/download_model.py
conda run -n Elysia python scripts/download_model.py --mirror
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/testing.md](../docs/testing.md) | 测试指南（测试流程中常用 `clean_db.py` 重置数据库） |
| [docs/architecture.md](../docs/architecture.md) | 完整架构（数据库存储设计、数据流） |
