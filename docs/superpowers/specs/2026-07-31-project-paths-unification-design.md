# 项目路径统一管理 — 设计文档

**日期:** 2026-07-31  
**状态:** 设计完成，待审查

## 动机

项目中硬编码路径分散在 ~20 个文件中，推导方式各异：

- **cwd-relative**：`Path("log")`、`os.path.join("data", ...)` — 依赖启动目录
- **`__file__`-relative**：`Path(__file__).resolve().parents[N]` — 脆弱，`N` 无文档
- **同一路径多处重复推导**：`data/uploads/images` 在 3 个文件各自计算
- **`Module/system_prompt/templates`** 在 4 处各自硬编码

没有统一的"项目根目录"概念，每个模块各自为政。

## 目标

1. 所有项目级文件系统路径集中定义在一个顶层模块 `paths.py` 中
2. 项目根目录由 `ELYSIA_ROOT` 环境变量显式控制，`paths.py` 位置作为 fallback
3. 消费方替换为一行的 `import paths` → `ProjectPath.X.resolve()`
4. 不改目录结构，不改变量传递方式（路径值语义不变，只改获取方式）

## 范围边界

**纳入 ProjectPath 管理：** 项目树内的文件系统路径（目录 + 文件），共 20 个。

**排除：**
- URL/URI（MongoDB、Milvus、LLM base_url 等）— 配置值，非文件路径
- `config.yaml` 中的路径值（`agent.character`、`live2d.static_image`）— 配置数据，非路径常量
- `Module/tts/*_voices.yaml` — `__file__` 同目录引用，合理不统一
- HuggingFace 缓存路径（`~/.cache/huggingface/`）— 第三方库路径
- openWakeWord 模型路径 — pip 包安装路径
- 测试文件 — 本 spec 只改生产代码

## 设计

### 1. 项目根目录检测

```
优先级: ELYSIA_ROOT 环境变量 > paths.py 所在目录（项目根）
```

- `paths.py` 位置固定（项目根目录），不可移动 — 它是空间锚点
- `_project_root` 模块加载时计算一次并缓存
- 后续所有 `resolve()` 基于此值拼接

### 2. 枚举定义

`paths.py` — 顶层模块，`ProjectPath(str, Enum)`：

**目录（13 个）：**

| 成员 | 值 | 说明 |
|------|-----|------|
| `DATA` | `data/` | 运行时数据根目录 |
| `DATA_UPLOADS` | `data/uploads/` | 用户上传文件 |
| `DATA_UPLOADS_IMAGES` | `data/uploads/images/` | 上传图片 |
| `DATA_SANDBOX` | `data/sandbox/{session_id}` | 工具安全沙箱 |
| `DATA_CACHE_VISION` | `data/cache/vision/` | 视觉工具缓存 |
| `DATA_REF` | `data/ref/` | 参考文件（音频等） |
| `LOG` | `log/` | 日志目录 |
| `STATIC` | `static/` | 前端静态资源 |
| `LIVE2D_ASSETS` | `data/live2d-assets/` | Live2D 资源 |
| `PROMPTS` | `core/prompts/` | Prompt 模板 |
| `SYSTEM_PROMPT_TEMPLATES` | `Module/system_prompt/templates/` | System prompt 模板 |
| `CHARACTERS` | `character/` | 角色配置目录 |
| `MODEL_CACHE` | `.model_cache/` | 嵌入模型本地缓存 |

**文件（7 个）：**

| 成员 | 值 | 说明 |
|------|-----|------|
| `CONFIG_YAML` | `config.yaml` | 主配置文件 |
| `DOTENV` | `.env` | 环境变量文件 |
| `CUSTOM_PROVIDERS_YAML` | `custom_providers.yaml` | 自定义 provider 配置 |
| `TOKEN_PENDING` | `data/token_usage_pending.jsonl` | Token 统计 pending |
| `SESSION_PENDING` | `data/session_{session_id}.json` | Session 持久化 pending |
| `SESSION_PENDING_CHECK` | `data/session_pending.jsonl` | Debug 面板 session pending 检查（注：此文件名与 `session.py` 实际写入的 `session_{id}.json` 不匹配，属于已有 bug，迁移不改行为） |
| `SCHEDULER_CHECKPOINT` | `data/scheduler_checkpoint.json` | 调度器检查点 |

### 3. API

```python
# 基础用法 — 返回绝对 Path
from paths import ProjectPath

log_dir = ProjectPath.LOG.resolve()              # → /path/to/project/log
prompts = ProjectPath.PROMPTS.resolve()           # → /path/to/project/core/prompts

# 含占位符 — .format() 填充
sandbox = ProjectPath.DATA_SANDBOX.resolve(session_id="abc")
# → /path/to/project/data/sandbox/abc

pending = ProjectPath.SESSION_PENDING.resolve(session_id="default")
# → /path/to/project/data/session_default.json

# 项目根目录
from paths import get_project_root
root = get_project_root()                         # → /path/to/project
```

`resolve()` 只做 `Path` 拼接，**不检查文件存在性**（不引入 I/O，纯计算）。

### 4. 环境变量覆盖

每个路径可用 `ELYSIA_PATH_{ENUM_NAME}` 环境变量覆盖，规则统一：

```bash
ELYSIA_PATH_LOG=/var/log/elysia        # → ProjectPath.LOG.resolve() 返回 /var/log/elysia
ELYSIA_PATH_DATA=/mnt/data/elysia      # → ProjectPath.DATA.resolve() 返回 /mnt/data/elysia
```

无需额外映射表，枚举名即为环境变量名后缀。

### 5. 调用示例

典型迁移前后对比：

```python
# -- Before: __file__-relative --
_IMAGES_DIR = Path(__file__).parent.parent.parent / "data" / "uploads" / "images"

# -- After --
from paths import ProjectPath
_IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()


# -- Before: cwd-relative --
self._pending_file = os.path.join("data", f"session_{session_id}.json")

# -- After --
self._pending_file = str(ProjectPath.SESSION_PENDING.resolve(session_id=session_id))


# -- Before: 同一路径在 4 处各自写 --
templates_dir = "Module/system_prompt/templates"

# -- After --
from paths import ProjectPath
templates_dir = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())
```

## 迁移分组

### 组 1：删除 `__file__`-relative 重复推导（6 处）

| 文件 | 当前 | 替换为 |
|------|------|--------|
| `core/input/server.py:25` | `.parent.parent.parent / "static"`（depth 2 → 3 hops） | `ProjectPath.STATIC.resolve()` |
| `core/input/server.py:26` | `.parent.parent.parent / "data" / "live2d-assets"` | `ProjectPath.LIVE2D_ASSETS.resolve()` |
| `core/input/server.py:249` | `.parent.parent.parent / "data" / "uploads"` | `ProjectPath.DATA_UPLOADS.resolve()` |
| `core/input/api_routes.py:189` | `.parent.parent.parent / "data" / "uploads" / "images"` | `ProjectPath.DATA_UPLOADS_IMAGES.resolve()` |
| `core/schema/adapters/message_adapter.py:103` | `.parent.parent.parent.parent / "data" / "uploads" / "images"`（depth 3 → 4 hops） | `ProjectPath.DATA_UPLOADS_IMAGES.resolve()` |
| `core/schema/adapters/message_adapter.py:104` | `.parent.parent.parent.parent / "static"` | `ProjectPath.STATIC.resolve()` |

### 组 2：消除 cwd-relative（7 处）

| 文件 | 当前 | 替换为 |
|------|------|--------|
| `core/utils/log_setup.py:16` | `Path("log")` | `ProjectPath.LOG.resolve()` |
| `core/memory/session.py:80` | `os.path.join("data", ...)` | `ProjectPath.SESSION_PENDING.resolve(session_id=...)` |
| `core/monitoring/token_tracker.py:47` | `os.path.join("data", ...)` | `ProjectPath.TOKEN_PENDING.resolve()` |
| `core/debug/info.py:352` | `"data/token_usage_pending.jsonl"` | `ProjectPath.TOKEN_PENDING.resolve()` |
| `core/debug/info.py:356` | `"data/session_pending.jsonl"` | `ProjectPath.SESSION_PENDING_CHECK.resolve()`（注：已有 bug，见枚举定义） |
| `core/input/custom_providers.py:10` | `Path("custom_providers.yaml")` | `ProjectPath.CUSTOM_PROVIDERS_YAML.resolve()` |
| `core/config/character_config.py:117` | `Path(".") / base_file` | `ProjectPath.CHARACTERS.resolve()` |

### 组 3：收敛重复引用（4 处）

| 文件 | 当前 | 替换为 |
|------|------|--------|
| `core/adapter/prompt_assembler.py:36` | `_SECTION = "Module/system_prompt/templates"` | `ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()` |
| `core/personality/compression.py:268` | `"Module/system_prompt/templates/c6_speaking_style.j2"` | `ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve() / "c6_speaking_style.j2"` |
| `core/input/debug_routes/recording.py:55` | `templates_dir = "Module/system_prompt/templates"` | `ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()` |
| `Module/system_prompt/__main__.py:19` | `os.path.join(os.path.dirname(...), "templates")` | `ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()` |

### 组 4：prompt 模板路径（5 处）

| 文件 | 当前 | 替换为 |
|------|------|--------|
| `core/memory/consolidator.py:32` | `.parent.parent / "prompts" / "ExtractMemory.j2"`（depth 2 → 2 hops） | `ProjectPath.PROMPTS.resolve() / "ExtractMemory.j2"` |
| `core/memory/consolidator.py:58` | `.parent.parent / "prompts" / "ConsolidationCompare.j2"` | `ProjectPath.PROMPTS.resolve() / "ConsolidationCompare.j2"` |
| `core/memory/memory_reflector.py:31` | `.parent.parent / "prompts" / "MemoryReflector.j2"` | `ProjectPath.PROMPTS.resolve() / "MemoryReflector.j2"` |
| `core/personality/engine.py:107` | `.parent.parent / "prompts"` | `ProjectPath.PROMPTS.resolve()` |
| `core/personality/reflector.py:66` | `.parent.parent / "prompts"` | `ProjectPath.PROMPTS.resolve()` |

### 组 5：其他（5 处）

| 文件 | 当前 | 替换为 |
|------|------|--------|
| `core/tools/vision/plugin.py:22` | `.parent.parent.parent.parent / "data" / "cache" / "vision"`（depth 3 → 4 hops） | `ProjectPath.DATA_CACHE_VISION.resolve()` |
| `core/tools/filesystem/plugin.py:221` | `"data/sandbox/{session_id}"` | `ProjectPath.DATA_SANDBOX.resolve(session_id=...)` |
| `Module/config/preflight.py:228-229` | `Path(".model_cache/...")` | `ProjectPath.MODEL_CACHE.resolve()` |
| `Module/config/preflight.py:242` | `("log", "data")` | `ProjectPath.LOG.resolve()`, `ProjectPath.DATA.resolve()` |
| `Module/_common/env.py:17` | `.parent.parent.parent` 推导项目根（depth 2 → 3 hops） | `ProjectPath.DOTENV.resolve()` |

### 保留不改

- `Module/tool/safety.py:58` — `"data/sandbox/{session_id}"` 作为 `SafetyProfile` 默认值（dataclass 字段），改其默认值即可，不改变量传递
- `core/config/app_config.py:10` — `character="character/elysia/config.yaml"` 是配置默认值，不是路径常量
- `app.py:38` — `ssl/cert.pem` conda 路径，非项目路径
- `Module/stt/__main__.py:10`、`Module/bench/bench.py:17` — `__main__.py` 均为 smoke-test 脚本，非生产代码

## 兼容性

**行为不变：**
- 所有路径值语义不变 — 只改获取方式，不改目录结构
- 现有 `os.makedirs(..., exist_ok=True)` 调用方继续正常工作

**调用方适配：**
- `resolve()` 返回 `pathlib.Path`，需要字符串的调用方（如 `os.path.join`、`open()` 等）需包裹 `str()`
- 枚举成员可在任何需要 `str` 的上下文中直接使用（`ProjectPath(str, Enum)`），无需显式转换

**环境变量：**
- `ELYSIA_PATH_{NAME}` 覆盖是纯加法，未设置时行为完全不变
- 已有环境变量（`ELYSIA_ROOT`、现有的 path env vars）不受影响

**导入路径：**
- `from paths import ProjectPath, get_project_root` — 顶层模块，无 breaking import path 变更

**新增枚举成员（共 20 个）：**
- 目录 13 个 + 文件 7 个（含 `SESSION_PENDING_CHECK`）

## 验证

1. `make test-quick` 全绿
2. `grep -rn "parents\[" --include="*.py" core/ Module/ | grep -v test_ | grep -v __pycache__` — 输出为空（除保留不改的）
3. `grep -rn 'os\.path\.join("data"' --include="*.py" core/ | grep -v test_` — 输出为空
4. `grep -rn '"Module/system_prompt/templates"' --include="*.py" core/ Module/` — 输出为空
