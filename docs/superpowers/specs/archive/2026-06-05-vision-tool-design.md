# 多模态图片理解工具 — `describe_image`

纯文本 LLM（如 DeepSeek）通过调用 `describe_image` 工具，委托多模态大模型（MIMO）理解图片。

## 动机

- MIMO 看图能力强但聊天弱
- DeepSeek 聊天强但不支持多模态
- 两者组合：DeepSeek 做主聊，MIMO 做"眼睛"

## 架构

```
用户发图 → prompt_assembler 检测纯文本LLM → 注入工具提示
→ DeepSeek 看到 "[用户发送了一张图片。使用 describe_image 工具查看。]"
→ DeepSeek 决定调用 describe_image 工具
→ 工具: LLMFactory.create(vision_model) → 发图+MIMO → 返回文字描述
→ DeepSeek 基于描述回复
```

## 改动清单

### 1. 配置

**`config.yaml`** — 新增：
```yaml
vision:
  model: mimo/mimo-v2.5
```

**`core/app_config.py`** — 新增：
```python
@dataclass
class VisionConfig:
    model: str = ConfigField(default="mimo/mimo-v2.5")

# AppConfig 加字段
vision: VisionConfig = field(default_factory=VisionConfig)
```

### 2. Registry 加 multimodal 标识

**`Module/llm/registry.py`**：
- `_BACKENDS` 每个 provider 加 `"supports_multimodal": True/False`
  - `mimo` → True
  - `deepseek` / `openai` / `ollama` / `gemini` → False
- `LLMFactory` 新增类方法 `supports_multimodal(model_id: str) -> bool`

### 3. 工具插件 — `core/tools/vision/`

**`plugin.py`** — `DescribeImagePlugin(ToolPlugin)`：
- `name = "describe_image"`
- `description = "查看用户发送的图片并描述其内容..."`
- `param_model = DescribeImageParams`

**`DescribeImageParams`**：
```python
class DescribeImageParams(BaseModel):
    prompt: str = Field(
        default="请详细描述这张图片的内容、人物、场景和细节。",
        description="对图片的提问或描述要求"
    )
    image_index: int = Field(default=0, ge=0, description="图片序号，0=第一张")
```

**`execute()` 逻辑**：
1. 从 `ctx.get("session")` 取最近一条用户消息
2. 从 content 数组中找第 `image_index` 个 `type:"image"` part
3. `_resolve_image_source(source)` → base64 data URI
4. `LLMFactory.create(vision_model)` → 发 `[image_part, text_prompt]`
5. 返回 MIMO 的回复文字

**vision_model** 从 `ctx.get("config").vision.model` 读取。

### 4. 缓存

- 目录：`data/cache/vision/`
- 文件：`{sha256}.json`
- 内容：`{"hash": "...", "description": "...", "model": "mimo/mimo-v2.5", "created_at": "..."}`
- 同一图片+同一 model → 命中缓存，跳过 MIMO 调用

### 5. prompt_assembler 改动

**`core/prompt_assembler.py`**：
- 构造函数加 `supports_multimodal: bool = False`
- `build_messages()`：当前消息有 image 且 `!supports_multimodal` 时，不发 image_url，改为：
  ```
  [用户发送了一张图片。使用 describe_image 工具查看。]
  用户文字内容
  ```

### 6. cognitive_loop 改动

**`core/cognitive_loop.py`**：
- `_think_loop()` 从 `self._llm` 获取 `supports_multimodal` → 传给 `build_messages()`

### 7. agent 改动

**`core/agent.py`**：
- ToolManager context 加 `session` 引用，供 `describe_image` 查找用户消息

### 8. 工具注册

**`core/tools/__init__.py`**：
- `ALL_PLUGINS` 加 `DescribeImagePlugin`

## 不改

- 前端 — 零改动，用户发图流程不变
- `Module/schema/message_format.py` — 不动
- `core/input/server.py` — 不动
- `core/memory/session.py` — 不动
- 现有的 MIMO 直接聊图路径 — 不变（切到 MIMO 时 `supports_multimodal=True`，走原路径）

## 验证

```bash
# 1. 启动服务（LLM=DeepSeek）
python -m core.input

# 2. 浏览器 → 发一张图 + 文字
# → DeepSeek 看到工具提示
# → DeepSeek 调用 describe_image
# → MIMO 返回描述
# → DeepSeek 基于描述回复

# 3. 再次发同一张图 → 应命中缓存

# 4. 切换到 MIMO → 发图 → 直接看图（不走工具）

# 5. 测试
python -m pytest core/tests/ -v --asyncio-mode=auto
python -m pytest Module/ -v --asyncio-mode=auto
```