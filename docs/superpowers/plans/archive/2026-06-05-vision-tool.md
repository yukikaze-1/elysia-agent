# describe_image 视觉工具 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 纯文本 LLM（DeepSeek）通过调用 `describe_image` 工具委托 MIMO 理解图片

**Architecture:** DescribeImagePlugin 工具插件 + LLMFactory.supports_multimodal() 检测 + prompt_assembler 根据 LLM 能力决定发 image_url 还是工具提示文本 + 图片 hash 缓存

**Tech Stack:** Python 3.13, aiohttp, httpx, Pydantic

---

### Task 1: 配置层 — config.yaml + app_config.py

**Files:**
- Modify: `config.yaml`
- Modify: `core/app_config.py`

- [ ] **Step 1: config.yaml 加 vision 配置**

在 `config.yaml` 末尾追加：

```yaml
# Vision model for describe_image tool (e.g., mimo/mimo-v2.5)
vision:
  model: mimo/mimo-v2.5
```

- [ ] **Step 2: app_config.py 加 VisionConfig**

在 `core/app_config.py` 中，在现有 `LLMConfig` / `TTSConfig` 等附近新增：

```python
@dataclass
class VisionConfig:
    """Vision model config — used by describe_image tool."""
    model: str = ConfigField(default="mimo/mimo-v2.5")
```

在 `AppConfig` dataclass 字段中追加：

```python
vision: VisionConfig = field(default_factory=VisionConfig)
```

- [ ] **Step 3: 验证**

```bash
python -c "from core.app_config import AppConfig; c = AppConfig(); print(c.vision.model)"
# Expected: mimo/mimo-v2.5
```

- [ ] **Step 4: Commit**

```bash
git add config.yaml core/app_config.py
git commit -m "feat: VisionConfig — vision.model 配置"
```

---

### Task 2: Registry 加 supports_multimodal 标识

**Files:**
- Modify: `Module/llm/registry.py`

- [ ] **Step 1: _BACKENDS 每个 provider 加 supports_multimodal**

```python
_BACKENDS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "beta_url": "https://api.deepseek.com/beta",
        "env_key": "DEEPSEEK_API_KEY",
        "supports_prefix": True,
        "supports_multimodal": False,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "OLLAMA_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
    "gemini": {
        "base_url": "",
        "env_key": "GEMINI_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "env_key": "MIMO_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": True,
    },
    "custom": {
        "base_url": "",
        "env_key": "",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
}
```

- [ ] **Step 2: LLMFactory 加 supports_multimodal 类方法**

在 `LLMFactory` 类中新增：

```python
@classmethod
def supports_multimodal(cls, model_id: str) -> bool:
    """检测 provider 是否支持多模态（图片输入）。"""
    provider = model_id.split("/")[0] if "/" in model_id else model_id
    return bool(cls._BACKENDS.get(provider, {}).get("supports_multimodal", False))
```

- [ ] **Step 3: 验证**

```bash
python -c "
from Module.llm.registry import LLMFactory
print('mimo:', LLMFactory.supports_multimodal('mimo/mimo-v2.5'))
print('deepseek:', LLMFactory.supports_multimodal('deepseek/deepseek-v4-flash'))
"
# Expected: mimo: True, deepseek: False
```

- [ ] **Step 4: Commit**

```bash
git add Module/llm/registry.py
git commit -m "feat: LLMFactory.supports_multimodal() — provider 多模态能力检测"
```

---

### Task 3: 视觉工具插件 — DescribeImagePlugin

**Files:**
- Create: `core/tools/vision/__init__.py`
- Create: `core/tools/vision/plugin.py`
- Create: `core/tools/vision/config.yaml`
- Modify: `core/tools/__init__.py`
- Modify: `.gitignore`

- [ ] **Step 1: 创建目录和空文件**

```bash
mkdir -p core/tools/vision
touch core/tools/vision/__init__.py
touch core/tools/vision/config.yaml
```

- [ ] **Step 2: 写 DescribeImagePlugin**

在 `core/tools/vision/plugin.py`：

```python
"""DescribeImagePlugin — 委托多模态大模型理解图片。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from Module.tool.protocol import ToolPlugin, ToolResult
from Module.tool.safety import SafetyProfile
from Module.llm.registry import LLMFactory
from Module.schema.message_format import _resolve_image_source

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "cache" / "vision"


class DescribeImageParams(BaseModel):
    prompt: str = Field(
        default="请详细描述这张图片的内容、人物、场景和细节。",
        description="对图片的提问或描述要求",
    )
    image_index: int = Field(
        default=0,
        ge=0,
        description="图片序号，0 表示第一张",
    )


class DescribeImagePlugin(ToolPlugin):
    name: str = "describe_image"
    description: str = (
        "查看用户发送的图片并描述其内容。当用户发送图片时，"
        "先调用此工具获取图片描述，再基于描述回复用户。"
    )
    param_model: type = DescribeImageParams
    capabilities: set = set()
    safety = SafetyProfile(requires_human_approval=False)

    def setup(self, ctx) -> None:
        self._ctx = ctx

    async def execute(self, *, ctx, emit=None, params: DescribeImageParams) -> ToolResult:
        # 1. 从 session 取最近一条用户消息
        session = ctx.get("session")
        if session is None:
            return ToolResult.fail(name=self.name, error="session 不可用")

        recent = session.recent_messages(20)
        user_msg = None
        for m in reversed(recent):
            if m.get("role") == "user":
                user_msg = m
                break
        if user_msg is None:
            return ToolResult.fail(name=self.name, error="未找到用户消息")

        content = user_msg.get("content", [])
        if not isinstance(content, list):
            return ToolResult.fail(name=self.name, error="消息不含附件")

        # 2. 找到目标图片
        images = [p for p in content if isinstance(p, dict) and p.get("type") == "image"]
        if params.image_index >= len(images):
            return ToolResult.fail(
                name=self.name,
                error=f"图片序号 {params.image_index} 超出范围 (共 {len(images)} 张)",
            )
        image = images[params.image_index]
        source = image.get("source", "")
        mime = image.get("mime", "image/png")

        # 3. 解析图片 → base64 data URI
        data_uri = _resolve_image_source(source)
        if not data_uri.startswith("data:"):
            # 无法解析为 data URI，尝试作为远程 URL
            pass

        # 4. 检查缓存
        image_data = None
        if data_uri.startswith("data:"):
            # 从 data URI 提取 bytes 做 hash
            b64_part = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            try:
                image_data = base64.b64decode(b64_part)
            except Exception:
                pass

        if image_data:
            config = ctx.get("config")
            vision_model = getattr(config, "vision", None)
            vision_model_id = getattr(vision_model, "model", "mimo/mimo-v2.5") if vision_model else "mimo/mimo-v2.5"
            cache_key = hashlib.sha256(image_data + vision_model_id.encode()).hexdigest()
            cached = _load_cache(cache_key)
            if cached:
                logger.info("describe_image 缓存命中: %s", cache_key[:12])
                return ToolResult.ok(content=cached, name=self.name)

        # 5. 调 MIMO 看图
        try:
            vision_model_id = "mimo/mimo-v2.5"
            config = ctx.get("config")
            vision_cfg = getattr(config, "vision", None) if config else None
            if vision_cfg:
                vision_model_id = getattr(vision_cfg, "model", "mimo/mimo-v2.5")

            backend = LLMFactory.create(vision_model_id)
            messages = [
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}},
                    {"type": "text", "text": params.prompt},
                ]},
            ]
            response = await backend.chat(messages)
            description = response.get("content", "").strip()
        except Exception as e:
            logger.exception("describe_image 失败")
            return ToolResult.fail(name=self.name, error=str(e))
        finally:
            if backend:
                try:
                    await backend.close()
                except Exception:
                    pass

        if not description:
            return ToolResult.fail(name=self.name, error="MIMO 返回空描述")

        # 6. 写入缓存
        if image_data:
            _save_cache(cache_key, description, vision_model_id)

        return ToolResult.ok(content=description, name=self.name)


def _cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.json"


def _load_cache(key: str) -> str | None:
    p = _cache_path(key)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("description")
        except Exception:
            pass
    return None


def _save_cache(key: str, description: str, model: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import time
    _cache_path(key).write_text(
        json.dumps({
            "hash": key,
            "description": description,
            "model": model,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
```

- [ ] **Step 3: 更新 __init__.py**

`core/tools/vision/__init__.py`：

```python
from .plugin import DescribeImagePlugin

__all__ = ["DescribeImagePlugin"]
```

- [ ] **Step 4: 注册到 ALL_PLUGINS**

在 `core/tools/__init__.py` 新增 import 并加入 `ALL_PLUGINS`：

```python
from .vision import DescribeImagePlugin

ALL_PLUGINS: list[type] = [
    ...
    DescribeImagePlugin,
]
```

- [ ] **Step 5: 更新 .gitignore**

```bash
# 追加行
echo "data/cache/" >> .gitignore
```

- [ ] **Step 6: 验证工具可加载**

```bash
python -c "
from core.tools import ALL_PLUGINS
names = [p.name for p in ALL_PLUGINS]
print('describe_image' in names, names)
"
# Expected: True ...
```

- [ ] **Step 7: Commit**

```bash
git add core/tools/vision/ core/tools/__init__.py .gitignore
git commit -m "feat: DescribeImagePlugin — 委托多模态大模型理解图片"
```

---

### Task 4: prompt_assembler — 纯文本 LLM 遇图处理

**Files:**
- Modify: `core/prompt_assembler.py`

- [ ] **Step 1: build_messages() 签名加 supports_multimodal 参数**

改 `build_messages()` 签名，加 `supports_multimodal: bool = False` 参数。当前消息遇图逻辑 — 找到约 line 140：

```python
if content:
    has_images = any(p.get("type") == "image" for p in content)
    if has_images and not supports_multimodal:
        # 纯文本 LLM → 注入工具提示，不发 image_url
        text = content_text_from_raw(content)
        hint = "[用户发送了一张图片。使用 describe_image 工具查看。]"
        user_text = f"{hint}\n{text}" if text else hint
        messages.append({"role": "user", "content": user_text})
    else:
        messages.append({"role": "user", "content": content_to_openai(content)})
else:
    messages.append({"role": "user", "content": user_message})
```

- [ ] **Step 2: 验证 — 跑现有测试**

```bash
python -m pytest core/tests/ -v --asyncio-mode=auto -k 'prompt or phase2' 2>&1 | tail -10
```

- [ ] **Step 3: Commit**

```bash
git add core/prompt_assembler.py
git commit -m "feat: prompt_assembler — 纯文本LLM遇图注入工具提示"
```

---

### Task 5: cognitive_loop + agent — 传入 supports_multimodal

**Files:**
- Modify: `core/cognitive_loop.py`
- Modify: `core/agent.py`

- [ ] **Step 1: cognitive_loop 传入 supports_multimodal**

`_think_loop()` 中调用 `build_messages()` 时（约 line 315），加一个参数：

```python
from Module.llm.registry import LLMFactory
supports_mm = LLMFactory.supports_multimodal(self._llm.model)

messages = await self._prompt.build_messages(
    user_message=user_message, user_name=user_name,
    stt_emotion=stt_emotion, stt_events=stt_events,
    content=content,
    supports_multimodal=supports_mm,
)
```

- [ ] **Step 2: agent.py — ToolManager context 加 session**

在 `core/agent.py` 找到 ToolManager 初始化和 setup 调用处，确保 session 已注入 context。查看 `_setup_tools()` 或类似方法，确认 `ctx` 中已有 `"session"` 键。

跟踪代码：`ToolManager.load_all()` → 遍历 plugins → `plugin.setup(ctx)`，`ctx` 来自 `self._ctx`。查看 agent.py 中 `self._ctx` 的设置：

```bash
grep -n 'self._ctx\|_tool_ctx\|context.*session' core/agent.py
```

如果已有 session → 不需改动。如果缺失 → 在 `start()` 或 `_setup_context()` 上加：

```python
self._ctx["session"] = self._session
```

- [ ] **Step 3: 验证**

```bash
python -m pytest core/tests/ -v --asyncio-mode=auto -x 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```bash
git add core/cognitive_loop.py core/agent.py core/prompt_assembler.py
git commit -m "feat: cognitive_loop 传入 supports_multimodal + agent context 加 session"
```

---

### Task 6: 端到端验证

- [ ] **Step 1: 跑全部测试**

```bash
python -m pytest core/tests/ Module/ -v --asyncio-mode=auto 2>&1 | tail -5
```

- [ ] **Step 2: 启动服务手动测试**

```bash
python -m Module.input
```

浏览器测试：
1. 确保 LLM=DeepSeek，发送图片+文字
2. 检查：DeepSeek 是否看到 `[用户发送了一张图片。使用 describe_image 工具查看。]`
3. 检查：DeepSeek 是否调用 describe_image 工具
4. 检查：工具返回图片描述
5. 检查：DeepSeek 基于描述生成回复
6. 再次发同一张图 → 缓存命中

- [ ] **Step 3: Commit final fixes**

```bash
git add -A && git commit -m "chore: describe_image 端到端验证通过"
```
