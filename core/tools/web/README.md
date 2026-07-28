> 文档导航：[项目文档](../../../README.md)

# 网络工具插件

提供 Agent 访问互联网的能力。

## 插件

- `web_search` — Tavily / Brave 搜索
- `web_fetch` — 抓取网页纯文本内容

## 配置 (config.yaml)

- `block_private_ips` — 是否阻止访问内网 IP
- `max_redirects` — HTTP 重定向最大次数
- `max_response_bytes` — 最大响应大小
- `request_timeout` — 请求超时时间（秒）
- `max_concurrency` — 最大并发请求数

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/filesystem/README.md](../filesystem/README.md) | 文件系统工具（同样涉及安全约束） |
