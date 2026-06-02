# 网络工具插件

提供 Agent 访问互联网的能力。

## 插件

- `web_search` — DuckDuckGo / Brave 搜索
- `web_fetch` — 抓取网页纯文本内容

## 配置 (config.yaml)

- `block_private_ips` — 是否阻止访问内网 IP
- `max_redirects` — HTTP 重定向最大次数
- `max_response_bytes` — 最大响应大小
- `request_timeout` — 请求超时时间（秒）
- `max_concurrency` — 最大并发请求数
