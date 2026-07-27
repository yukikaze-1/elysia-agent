# core/config/ — 配置子包

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

应用和角色的配置 dataclass，零内部依赖，纯数据类。

## 文件布局

```
core/config/
├── __init__.py          # 重导出
├── app_config.py        # 应用配置
└── character_config.py  # 角色配置
```

## 组件

### app_config.py（~70 行）

`AppConfig` dataclass — `ConfigLoader` 从 `config.yaml` + env var 加载：

- `AgentConfig` — agent 名、用户、character 路径
- `LLMConfig` — 模型、provider、API key/URL
- `MemoryConfig` — Session/LTM 参数
- `ServerConfig` — host、port
- `TTSConfig` / `STTConfig` — 语音开关与配置
- `WakeWordConfig` / `Live2DConfig` / `VisionConfig`

优先级：env var > YAML > dataclass default。

### character_config.py（~80 行）

`CharacterConfig` dataclass — `from_yaml()` 从 `character/<name>/config.yaml` 加载：

- `name`、`wake_words`
- `EmotionConfig` — 情绪阈值和 idle 表情列表
- `ProactiveConfig` — 主动搭话参数（冷却、时段触发）
- L1 Core 性格文件路径（`personality.md`）

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + Config 系统 |
| [Module/config/README.md](../../Module/config/README.md) | Config 基础设施 |
