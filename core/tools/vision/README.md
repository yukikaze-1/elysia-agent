# 视觉工具插件

> 文档导航：[项目文档](../../../README.md)

委托多模态大模型（默认 MiMo v2.5）描述用户发送的图片。支持单图/批量模式，结果缓存到 `data/cache/vision/`。

## 工具

- `describe_image` — 描述用户发送的图片。参数：
  - `prompt`（可选，默认请求详细描述图片内容、人物、场景）
  - `image_index`（单图模式，图片序号，从 0 开始）
  - `image_indices`（批量模式，序号列表如 `[0, 1, 2]`，一次调用查看多张图片）

## 配置 (config.yaml)

无需特殊安全配置。Vision model ID 从 `config.vision.model` 读取，默认 `mimo/mimo-v2.5`。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [Module/llm/README.md](../../../Module/llm/README.md) | LLM 模块（视觉模型后端） |
