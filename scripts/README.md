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
| `clean_db.py` | 数据库清理（清空 MongoDB + Milvus + 本地持久化文件；`--conversation-only` 只清对话数据、保留角色与用户身份） | `conda run -n Elysia python scripts/clean_db.py` |
| `check_compose_security.py` | Compose 安全策略门禁（P1-19）：数据服务端口仅 loopback、无默认凭据/空 token、生产不发布管理端口 | `conda run -n Elysia python scripts/check_compose_security.py` |
| `gen_contract_frames.py` | 契约层 golden frames 生成/漂移检测（9B-1 + 阶段4）：17 个 `ws-v2-*.jsonl`/`rest-*.jsonl`（WS 含阶段4 普通录音/听写/音频/服务切换，REST 含 Profile/TTS 正反例）；`--check` 漂移返回 1（`make check-types` 强制） | `conda run -n Elysia python scripts/gen_contract_frames.py` |
| `token_corpus/build_corpus.py` | **token 估算基准语料生成器**（Batch 18 / KB-12）：确定性产出 `Module/system_prompt/tests/fixtures/token_corpus/` 下的 55 条语料（自然语言/CJK/JSON/代码/Unicode/工具 schema/代表性 Elysia prompt + `repeat` 退化类 + 6 条 holdout）。无网络、无依赖。**签入产物才是测试输入**，生成器只为可审计 | `conda run -n Elysia python scripts/token_corpus/build_corpus.py` |
| `token_corpus/build_golden.py` | **真值生成器**（Batch 18 / KB-12）：用 7 个权威 tokenizer（HF Qwen2.5/DeepSeek-V3/Llama-3/Mistral + tiktoken cl100k/o200k/p50k）与真实 Ollama `prompt_eval_count` 产出 `GOLDEN.json`。**需一次性拉取 tokenizer 文件 + 本机 Ollama，不进 CI**；词表不签入仓库 | `conda run -n Elysia python scripts/token_corpus/build_golden.py`（前置见文件 docstring） |
| `token_corpus/validate_families.py` | **估算合同的外部验证入口**（Batch 18 / KB-12）：对任意 HF repo 报出它在对应 profile 下的 est/actual min/median/max 与越界样本，并显式列出**未覆盖**（min < 0.75）的家族。新增/升级 provider 家族时跑一次，据此决定「纳入拟合」还是「如实登记」。需下载 tokenizer 文件，**不进 CI** | `conda run -n Elysia python scripts/token_corpus/validate_families.py [repo_id ...]` |
| `stage4_smoke.py` | 阶段 4 确定性契约 smoke（P1-01）：进程内 `create_app` + 真 agent(fake LLM) + STT/TTS 桩，覆盖 Profile/三语音模式/TTS 播放 5 场景；默认 stub 确定性、失败非零退出；`STAGE4_SMOKE_LIVE=1` 才接真实 provider（不进 CI） | `conda run -n Elysia python scripts/stage4_smoke.py` |
| `dev/seed_stage_h_pinned.py` | Stage H pinned 联调夹具（BE-STAGEH-05）：幂等建 fixture definition/revision + pinned & follow_latest instance + 两条 session binding；`--cleanup` 删净固定 ID 的**全部**数据——先派生记录（message_archive/token_usage/generations）再本体（session/instance/全部 revision/definition），随后回读复查。**必须在服务停止后运行**（否则关机 flush 会复活）。须 `STAGE_H_FIXTURES=1` 且 Mongo 非公网 | `STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/seed_stage_h_pinned.py [--cleanup]` |
| `dev/stage_h_pinned_smoke.py` | Stage H pinned 真实开发库 smoke（BE-H2-03）：进程内 `create_app` + 真实 Mongo/Milvus + stub LLM，跑 seed → 公开 REST list/detail → 推进到 revision 2 → pinned/follow_latest 各一轮真实回合 → 关机 → cleanup → 冷服务复查（BE-R1~R5，38 项断言；两段 agent 各自 try/finally，异常也走完收尾）。须 `STAGE_H_FIXTURES=1` 且 Mongo 非公网 | `STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/stage_h_pinned_smoke.py` |

## clean_db.py

一键清理 Elysia Agent 数据库内容，用于测试环境重置。提供两个 MongoDB 清理范围：
**全量**（默认）与 **`--conversation-only` 只清对话数据**（保留角色、版本、附件、用户身份）。

> **必须先停止后端服务**：`idempotency` / `desktop_calls` / `attachments` 均 fail-closed，
> 且 `desktop_calls` 有进程内缓存作为读权威——服务在跑时删库，内存态仍在、已删记录继续可读；
> 下次关机回写还会把行重新创建。

### 清理范围

| 存储 | 全量模式 | `--conversation-only` | 方法 |
|------|----------|----------------------|------|
| MongoDB (`default`) | `sessions`、`message_archive`、`token_usage`、`generations`、`idempotency`、`desktop_calls`、`characters`、`character_revisions`、`character_instances`、`character_versions`、`character_l2_versions`（旧）、`attachments` | `sessions`、`message_archive`、`token_usage`、`generations`、`idempotency`、`desktop_calls` | `delete_many({})` 清空文档，保留 indexes |
| Milvus | `long_term_memory` | 同左（由 `--milvus-only` 组合控制） | drop 后自动重建（dimension=1024, IVF_FLAT） |
| 本地文件 | 运行时数据根（`ProjectPath.DATA`）下的 journal：`token_usage_pending.jsonl`、`scheduler_checkpoint.json`、`memory_pending_deletes.json`、`memory_pending_syncs.json`、`session_*.json`（每会话一个，含 `session_default.json` 与 `session_pending_cleanup.json`）、`data/cache/vision/*.json` | 同左（由 `--files-only` 组合控制） | 逐个删除 |

- **永不删除**：`user_identity_profiles`——用户身份是 Stage H 的 Mongo 权威（BE-STAGEH-02），删了会退回 bootstrap 默认身份。
- `attachments` 只在全量模式清（且仅清 MongoDB 记录）；对话模式保留附件记录，删掉 `message_archive` 后的孤儿记录由 `AttachmentGC` 的「未关联超 7 天软删」规则回收，`data/uploads/` 磁盘文件不在本脚本范围。
- 会话文件按 `session_*.json` glob 删（多会话每会话一个）——只删写死的 `session_default.json` 会漏掉其余会话，`SessionMemory.load()` 在 Mongo 读失败时 fallback 读它们，清库后会被复活。
- 集合清单单源在 `clean_db.py` 的 `CONVERSATION_COLLECTIONS` / `FULL_ONLY_COLLECTIONS`，由 `core/tests/test_clean_db.py` 用字面量钉死——增删集合必须让测试红。

### CLI 参数

```
usage: clean_db.py [-h] [-f] [--mongo-only | --conversation-only]
                   [--milvus-only] [--files-only] [--dry-run]

options:
  -h                    显示帮助
  -f, --force           跳过确认提示，直接执行
  --mongo-only          只清理 MongoDB（全量范围，含角色/附件）
  --conversation-only   只清理对话数据（保留角色、版本、附件、用户身份）
  --milvus-only         只清理 Milvus
  --files-only          只清理本地 data/ 文件
  --dry-run             仅检查数据库连接，不实际清理
```

`--mongo-only` 与 `--conversation-only` 互斥（对 mongo 的范围诉求冲突，报错而非静默取其一）；
`--conversation-only` 可与 `--milvus-only` / `--files-only` 组合。

### 使用示例

```bash
# 清理全部（需输入 yes 确认）
conda run -n Elysia python scripts/clean_db.py

# 跳过确认，直接执行
conda run -n Elysia python scripts/clean_db.py -f

# 只清对话数据（保留角色、版本、附件、用户身份）
conda run -n Elysia python scripts/clean_db.py --conversation-only

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
