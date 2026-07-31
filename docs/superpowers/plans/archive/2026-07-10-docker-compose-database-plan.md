# Docker Compose 数据库部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up -d` 一键启动 MongoDB + Milvus，README 教程清晰。

**Architecture:** `docker-compose.yml` 定义两个服务（mongo:7 + milvusdb/milvus:v2.4.0 standalone），端口与 .env 默认值一致。

**Tech Stack:** Docker Compose v2

## Global Constraints

- 端口与 .env.example 默认值一致（MongoDB 27017，Milvus 19530）
- 数据卷持久化，重启不丢失
- 前置条件注明需要 Docker + Docker Compose

---

### Task 1: Create `docker-compose.yml`

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Create the file**

```yaml
# Elysia Agent — 数据库服务 (MongoDB + Milvus)
#
# 启动: docker compose up -d
# 停止: docker compose down
# 清除数据: docker compose down -v
#
# 端口与 .env 默认值一致，无需额外配置。

services:
  mongodb:
    image: mongo:7
    container_name: elysia-mongo
    ports:
      - "27017:27017"
    volumes:
      - mongo_data:/data/db
    restart: unless-stopped

  milvus:
    image: milvusdb/milvus:v2.4.0
    container_name: elysia-milvus
    command: ["milvus", "run", "standalone"]
    ports:
      - "19530:19530"
      - "9091:9091"
    volumes:
      - milvus_data:/var/lib/milvus
    restart: unless-stopped

volumes:
  mongo_data:
  milvus_data:
```

- [ ] **Step 2: Verify YAML syntax**

```bash
docker compose -f docker-compose.yml config
# 应无错误输出配置内容；若 Docker 未安装，用 python -c "import yaml; yaml.safe_load(open('docker-compose.yml'))" 检查
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add docker-compose.yml for MongoDB + Milvus"
```

---

### Task 2: Update README.md database section

**Files:**
- Modify: `README.md` (lines 35-44)

- [ ] **Step 1: Replace the current database section**

Replace:
```
### 3. 可选：数据库（长期记忆持久化）

```bash
# Milvus 向量数据库 — 长期记忆语义检索
docker run -d --name milvus -p 19530:19530 -p 9091:9091 milvusdb/milvus:latest
# 或配置 .env 中 MILVUS_URI 指向已有实例

# MongoDB — token 追踪持久化（默认降级到文件存储）
# .env 中配置 MONGODB_URI=mongodb://localhost:27017
```
```

With:

````
### 3. 可选：数据库（长期记忆持久化）

前置条件：安装 [Docker](https://docs.docker.com/engine/install/) + Docker Compose 插件。

数据库不需要单独安装——Docker 镜像已包含 MongoDB 和 Milvus。

```bash
# 启动 MongoDB + Milvus
docker compose up -d

# 查看运行状态
docker compose ps

# 停止
docker compose down
```

端口与 `.env` 默认值一致（MongoDB 27017，Milvus 19530），无需额外配置。

**数据持久化：** 数据存储在 Docker volume 中，重启不丢失。如需彻底清除：

```bash
docker compose down -v   # 删除所有数据！
```
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: replace docker run with docker compose in database section"
```

---

### Task 3: Update .env.example database section

**Files:**
- Modify: `.env.example` (lines 63-71)

- [ ] **Step 1: Add hint comment**

Replace:
```
# 数据库 — 持久化存储（可选，降级到文件存储）
# =============================================================================

# Milvus 向量数据库 — 长期记忆
# MILVUS_URI=http://localhost:19530
# MILVUS_TOKEN=root:your-password

# MongoDB 文档数据库 — token 追踪等
# MONGODB_URI=mongodb://localhost:27017
```

With:
```
# 数据库 — 持久化存储（可选，降级到文件存储）
# 用 docker compose up -d 启动后无需修改以下配置
# =============================================================================

# Milvus 向量数据库 — 长期记忆
# MILVUS_URI=http://localhost:19530
# MILVUS_TOKEN=

# MongoDB 文档数据库 — token 追踪等
# MONGODB_URI=mongodb://localhost:27017
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: add docker compose hint to .env.example database section"
```

---

### Verification

```bash
# 1. docker compose 启动 (需要 Docker)
docker compose up -d
docker compose ps  # 应显示 elysia-mongo, elysia-milvus running

# 2. 停止 + 重新启动验证持久化
docker compose down
docker compose up -d

# 3. 清理
docker compose down
```
