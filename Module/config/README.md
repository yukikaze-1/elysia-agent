# Module/config

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

配置基础设施模块。基于 dataclass，支持 YAML + 环境变量加载和校验。

## 文件结构

```
Module/config/
├── __init__.py          # 导出 ConfigLoader, ConfigField, 约束类
├── loader.py            # ConfigLoader — YAML + env 分层加载
├── field.py             # ConfigField — dataclass 字段描述符
├── validator.py         # 类型校验 + 约束验证
├── errors.py            # ConfigValidationError 错误类型
├── providers.py         # 自定义 provider 端点管理（CRUD + 连通性测试）
├── preflight.py         # 环境预检（app.py --check）
├── _utils.py            # 内部工具
├── providers.yaml       # 内置 provider 注册表（LLM/STT/TTS 默认端点）
├── __main__.py          # 集成烟雾测试
└── tests/
```

`core/input/custom_providers.py` 管理 `custom_providers.yaml` 的运行时读写，支持添加/删除/测试自定义 LLM/STT/TTS 端点。`preflight.py` 实现 `app.py --check` 和 `app.py --check --full` 的 14 项环境预检。

## 快速开始

```python
from dataclasses import dataclass
from Module.config import ConfigField, ConfigLoader, Range, Choices

@dataclass
class MyConfig:
    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8000, constraints=[Range(1, 65535)])
    api_key: str = ConfigField(default="", secret=True, env="MY_API_KEY")
    mode: str = ConfigField(default="prod", constraints=[Choices(["dev", "prod"])])

loader = ConfigLoader(MyConfig, yaml_path="config.yaml", env_path=".env")
cfg = loader.load()
```

## 优先级

```
环境变量 > YAML > dataclass 默认值
```

1. 实例化 dataclass，使用字段默认值
2. 加载 `.env` 文件（幂等 — 进程内只执行一次）
3. YAML 合并覆盖默认值（跳过 `secret=True` 字段）
4. 环境变量覆盖（自动映射：`字段名大写`，或显式指定 `env="VAR"`）
5. 校验（必填、类型、约束）— 收集全部错误后一次性抛出

### 环境变量类型强制转换

环境变量的值始终是字符串，loader 会根据字段的类型注解自动转换：

| 目标类型 | 转换规则 |
|---|---|
| `str` | 直接使用 |
| `int` | `int(value)` |
| `float` | `float(value)` |
| `bool` | `"true"`/`"1"`/`"yes"` → `True`，`"false"`/`"0"`/`"no"` → `False` |
| `list` / `dict` | JSON 解析 (`json.loads`) |
| `Optional[X]` | 展开为 `X` 后按上表转换 |

> **注意**: 对于 `Union[X, Y, ...]` 类型，只会使用**第一个**非 None 的类型参数进行转换（例如 `Union[int, str]`  → 尝试 `int`）。如果需要更复杂的解析，请将字段声明为 `str` 类型并在加载后自行处理。

## ConfigField 参数

| 参数 | 说明 |
|---|---|
| `default` | 默认值。不填则为 `MISSING`（需由 YAML 或环境变量提供）。 |
| `default_factory` | 零参数可调用对象，用于生成默认值（与 `default` 互斥）。 |
| `env` | 显式指定环境变量名。为 `None` 时自动推 导为 `字段名大写`。 |
| `secret` | 为 `True` 时跳过 YAML 合并，仅从环境变量读取。隐含 `required=True`。 |
| `required` | 加载后值仍为 `MISSING` 时抛出 `ConfigMissingError`。 |
| `constraints` | 约束列表，如 `Range`、`Choices`、`Regex`。 |
| `**kwargs` | 透传给 `dataclasses.field()`（如 `repr=False`）。 |

## 内置约束

- `Range(min, max)` — 数值范围（含边界），`min`/`max` 可选（省略表示无下限/上限）
- `Choices([...])` — 值必须在允许列表中，列表至少包含一个元素
- `Regex(pattern, *, message)` — 字符串必须包含匹配正则的子串；可选参数 `message` 自定义错误信息

## API

```python
from Module.config import ConfigField, ConfigLoader, MISSING
from Module.config import Range, Choices, Regex
from Module.config import ConfigError, ConfigValidationError, ConfigMissingError
```

### ConfigLoader

```python
loader = ConfigLoader(
    MyConfig,
    env_path=".env",             # .env 文件路径，可选
    yaml_path="config.yaml",     # YAML 文件路径，可选
    yaml_key="Module.MyModule",  # YAML 中的嵌套 key（点号分隔），可选
)
cfg = loader.load()              # 完整流程：默认值 → YAML → 环境变量 → 校验

cfg = loader.load_from_dict({    # 从字典加载（跳过 YAML 文件）
    "host": "127.0.0.1",
})
```

### 错误类型

```
ConfigError
├── ConfigValidationError      # 聚合错误，包含 errors 列表
├── ConfigMissingError         # 必填字段未设置
├── ConfigTypeError            # 值与类型注解不匹配
├── ConfigConstraintError      # 约束违反
└── ConfigEnvFormatError       # 环境变量字符串无法转为目标类型
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [docs/architecture.md](../../docs/architecture.md) | 配置系统架构 |
