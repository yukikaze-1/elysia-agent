# Docker Compose 部署数据库 + README 教程

## 动机

当前 README 数据库部分只有两行难以复制的 `docker run` 命令。Milvus standalone 模式需要一条 docker 命令；MongoDB 需要另一条。需要联网下载镜像，没有持久化配置，也没有停止/清理命令。

目标：`docker compose up -d` 一键启动 MongoDB + Milvus，README 中写清楚每一步。

## 前置条件

- Docker + Docker Compose 插件（Docker Desktop 自带，Linux 需 `sudo apt install docker.io docker-compose-v2`）

不需要单独安装 MongoDB 或 Milvus —— Docker 镜像自带。

## 设计

### 文件

| 文件 | 动作 | 说明 |
|------|------|------|
| `docker-compose.yml` | **新建** | MongoDB 7 + Milvus v2.4 standalone |
| `README.md` | **修改** | 数据库段重写为 docker-compose 教程 |
| `.env.example` | **修改** | 数据库段加提示："用 docker compose 启动后无需修改" |

### docker-compose.yml

```yaml
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

- 端口、`.env` 默认值完全匹配，不需要额外配置
- `volumes` 数据卷确保持久化——容器删除后数据不丢失
- `restart: unless-stopped` 确保服务器重启后自动启动

### README 数据库段

```
### 3. 可选：数据库（长期记忆持久化）

前置条件: 安装 Docker + Docker Compose。

```bash
# 启动 MongoDB + Milvus
docker compose up -d

# 查看状态
docker compose ps

# 停止
docker compose down
```

端口和 .env 默认值匹配，无需额外配置。启动后运行 `python app.py --check --full` 验证连通性。

**数据在哪？** Docker volume 中，重启不丢失。清除数据：

```bash
docker compose down -v   # 会删除所有数据！
```

### .env.example 数据库段

加一行提示注释。
```

## 成功标准

1. `docker compose up -d` → MongoDB + Milvus 启动 → `docker compose ps` 显示两者 running
2. `docker compose ps` 显示两者 running，端口与 `.env` 默认值一致，无需额外配置
3. `docker compose down` 停止 → `docker compose up -d` 再次启动 → 数据仍在
