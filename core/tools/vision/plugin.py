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

        # 4. 检查缓存
        image_data = None
        if data_uri.startswith("data:"):
            b64_part = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            try:
                image_data = base64.b64decode(b64_part)
            except Exception:
                pass

        vision_model_id = "mimo/mimo-v2.5"
        config = ctx.get("config")
        if config:
            vision_cfg = getattr(config, "vision", None)
            if vision_cfg:
                vision_model_id = getattr(vision_cfg, "model", "mimo/mimo-v2.5")

        if image_data:
            cache_key = hashlib.sha256(image_data + vision_model_id.encode()).hexdigest()
            cached = _load_cache(cache_key)
            if cached:
                logger.info("describe_image 缓存命中: %s", cache_key[:12])
                return ToolResult.ok(name=self.name, content=cached, duration_ms=0.0)
        else:
            cache_key = None

        # 5. 调 MIMO 看图
        backend = None
        try:
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
        if cache_key and image_data:
            _save_cache(cache_key, description, vision_model_id)

        return ToolResult.ok(name=self.name, content=description, duration_ms=0.0)


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
