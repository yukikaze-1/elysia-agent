# 日志文件说明

> 文档导航：[项目文档](../README.md)

> `ls log/` 快速索引。更详细的日志系统设计见 [architecture.md](../docs/architecture.md#10-日志系统)。

## 文件一览

| 文件 | 用途 | 级别 | 轮转 |
|------|------|------|------|
| `app.log` | 应用主日志 — 关键事件：对话轮次、心情变化、压缩/反思、会话加载 | INFO+ | 10MB×3 |
| `app-debug.log` | 全量调试日志 — 所有 DEBUG 及以上，开发调试用 | DEBUG+ | 20MB×5 |
| `conversation.log` | 对话内容 — 用户消息（`>>>`）和 AI 回复（`<<<`），从 app.log 中分离 | INFO | 10MB×3 |
| `mongo.log` | MongoDB 操作 — pymongo/motor 库日志（WARNING+）+ 应用层 session/message/token/character_versions 的 CRUD（DEBUG） | DEBUG | 10MB×3 |
| `milvus.log` | Milvus 操作 — pymilvus/milvus_client 库日志（WARNING+）+ 应用层 long_term_memory 的 CRUD（DEBUG） | DEBUG | 10MB×3 |
| `http.log` | HTTP 请求 — httpx/httpcore/openai/urllib3 库日志 | WARNING+ | 10MB×3 |
| `embedding.log` | 模型 — sentence_transformers/transformers/huggingface_hub + LTM embedding 操作 | WARNING / DEBUG | 10MB×3 |
| `torch.log` | PyTorch 运行时（通常空） | WARNING+ | 10MB×3 |

## 常用查询

```bash
# 只看关键事件
tail -f log/app.log

# 调试某个功能（全量细节）
tail -f log/app-debug.log | grep "心情\|mood"

# 只查数据库操作
tail -f log/mongo.log

# 追踪一次完整对话
tail -f log/conversation.log

# 排查 Milvus 连接/搜索问题
tail -f log/milvus.log
```

## 设计原则

1. **双文件输出**：`app.log`（干净，日常看）+ `app-debug.log`（全量，调试用）。两条独立，互不影响。
2. **DB 操作单独分流**：所有 MongoDB/Milvus CRUD 操作在各自日志中可查，`app.log` 中也有副本。
3. **库日志隔离**：pymongo、httpx、torch 等第三方库日志分流到对应文件，不污染主日志。
4. **终端干净**：stdout 仅输出对话内容；stderr 为 INFO 级别（`--verbose` 变 DEBUG），不写文件依赖。
5. **日志不追踪到具体文件/handler**：所有 handler 追加模式（`addHandler`），日志同时写入多个文件。
