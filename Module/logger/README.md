# Logger Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

基于 Python 标准库 logging 的配置化日志工厂。支持 plain / JSON 双格式、按模块差异化级别、root logger 兜底。

## 文件结构

```
logger/
├── __init__.py           # 导出 LoggerFactory
├── factory.py            # LoggerFactory 类
├── test_logger.py        # 单元测试
└── README.md
```

## 快速开始

```python
from Module.logger import LoggerFactory

lf = LoggerFactory(log_dir="./log")

logger = lf.get("MyComponent")
logger.info("hello")
logger.debug("won't show — default level is INFO")

lf.close()
```

## 接口

```python
class LoggerFactory:
    def __init__(
        self,
        *,
        log_dir: str = "log",
        level: str | int = "INFO",
        json_format: bool = False,
        module_levels: dict[str, str] | None = None,
        setup_root: bool = True,
        backup_count: int = 30,
    ): ...

    def get(self, name: str) -> logging.Logger: ...
    def set_level(self, name: str, level: str | int) -> None: ...
    def close(self) -> None: ...
```

### get(name)

获取或创建命名 logger。首次调用自动挂载：
- `TimedRotatingFileHandler` — 写入 `log_dir/logger_{name}.log`，midnight 切割，保留 `backup_count` 天
- `StreamHandler` — 输出到 stdout

重复调用同一 name 不会重复添加 handler。

### set_level(name, level)

运行时动态调整指定 logger 的日志级别。

## 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `log_dir` | `"log"` | 日志文件目录，自动创建 |
| `level` | `"INFO"` | 默认日志级别 |
| `json_format` | `False` | `True` 时输出 JSON 行格式 |
| `module_levels` | `None` | 按模块差异化级别，前缀匹配 |
| `setup_root` | `True` | 给 root logger 挂 StreamHandler（WARNING），捕获第三方库日志 |
| `backup_count` | `30` | 切割文件保留天数 |

## JSON 格式

```python
lf = LoggerFactory(log_dir="./log", json_format=True)
logger = lf.get("ApiService")
logger.info("request completed")
# → {"time": "2026-05-16T15:30:00.123", "level": "INFO", "logger": "ApiService", "message": "request completed"}
```

## 按模块差异化级别

```python
lf = LoggerFactory(
    level="INFO",
    module_levels={
        "L1": "DEBUG",       # L1 及子 logger（L1.BrainLayer 等）使用 DEBUG
        "L2": "WARNING",     # L2 及子 logger 仅输出 WARNING+
    },
)
```

前缀匹配规则：以 `key` 本身或 `key.` 开头即认为匹配。多个前缀匹配时，更长的优先。

## 与 Module/config/ 配合

```python
from dataclasses import dataclass
from Module.config import ConfigField, ConfigLoader, Choices
from Module.logger import LoggerFactory

@dataclass
class LoggerConfig:
    log_dir: str = ConfigField(default="log")
    level: str = ConfigField(default="INFO", constraints=[Choices(["DEBUG", "INFO", "WARNING", "ERROR"])])
    json_format: bool = ConfigField(default=False)

loader = ConfigLoader(LoggerConfig, yaml_path="config.yaml", yaml_key="Logger")
cfg = loader.load()
lf = LoggerFactory(**dataclasses.asdict(cfg))
```

## 测试

```bash
python -m pytest Module/logger/test_logger.py -v
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [docs/architecture.md](../../docs/architecture.md) | 日志系统架构 |
