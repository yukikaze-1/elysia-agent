"""DescribeImagePlugin — 委托多模态大模型理解图片。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from Module.tool.protocol import ToolPlugin, ToolResult
from Module.tool.safety import SafetyProfile
from Module.llm.registry import LLMFactory
from core.schema.message import UserMessage
from core.schema.part import ImagePart
from core.schema.adapters.message_adapter import _resolve_image_source

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "cache" / "vision"


class DescribeImageParams(BaseModel):
    prompt: str = Field(
        default="请详细描述这张图片的内容、人物、场景和细节。",
        description="对图片的提问或描述要求。批量模式时，会对每张图片应用相同提示。",
    )
    image_index: int = Field(
        default=0,
        ge=0,
        description="[单图模式] 图片序号，0 表示第一张。与 image_indices 互斥。",
    )
    image_indices: list[int] | None = Field(
        default=None,
        description="[批量模式] 图片序号列表，例如 [0, 1, 2]。一次性查看多张图片，减少工具调用次数。",
    )

    @model_validator(mode="after")
    def check_mutual_exclusion(self):
        """批量模式与单图模式互斥。"""
        if self.image_indices is not None and self.image_index != 0:
            raise ValueError("image_indices 和 image_index 不能同时使用。批量模式请只提供 image_indices。")
        if self.image_indices is not None and len(self.image_indices) == 0:
            raise ValueError("image_indices 不能为空列表。")
        return self


class DescribeImagePlugin(ToolPlugin):
    name: str = "describe_image"
    description: str = (
        "查看用户发送的图片并描述其内容。当用户发送图片时，"
        "先调用此工具获取图片描述，再基于描述回复用户。"
        "单张图片使用 image_index 参数，多张图片使用 image_indices 参数一次性批量查看。"
    )
    param_model: type = DescribeImageParams
    capabilities: set = set()
    safety = SafetyProfile(requires_human_approval=False, request_timeout=120.0)

    async def setup(self, ctx) -> None:
        self._ctx = ctx

    # ------------------------------------------------------------------
    # 公共入口 — 根据参数分发到单图或批量路径
    # ------------------------------------------------------------------
    async def execute(self, *, ctx, emit=None, params: DescribeImageParams) -> ToolResult:
        session = ctx.get("session")
        if session is None:
            return ToolResult.fail(name=self.name, error="session 不可用")

        recent = session.recent_messages(20)
        user_msg = None
        for m in reversed(recent):
            if isinstance(m, UserMessage):
                user_msg = m
                break
        if user_msg is None:
            return ToolResult.fail(name=self.name, error="未找到用户消息")

        images = [p for p in user_msg.content if isinstance(p, ImagePart)]

        if params.image_indices is not None:
            return await self._describe_batch(ctx, images, params)
        else:
            return await self._describe_single(ctx, images, params)

    # ------------------------------------------------------------------
    # 单图模式 — 保留原有逻辑不变
    # ------------------------------------------------------------------
    async def _describe_single(self, ctx, images: list[dict], params: DescribeImageParams) -> ToolResult:
        if params.image_index >= len(images):
            return ToolResult.fail(
                name=self.name,
                error=f"图片序号 {params.image_index} 超出范围 (共 {len(images)} 张)",
            )
        image = images[params.image_index]
        source = image.get("src", "")

        data_uri = _resolve_image_source(source)

        image_data = None
        if data_uri.startswith("data:"):
            b64_part = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            try:
                image_data = base64.b64decode(b64_part)
            except Exception:
                pass

        vision_model_id = self._get_vision_model_id(ctx)

        if image_data:
            cache_key = hashlib.sha256(image_data + vision_model_id.encode()).hexdigest()
            cached = _load_cache(cache_key)
            if cached:
                logger.info("describe_image 缓存命中: %s", cache_key[:12])
                return ToolResult.ok(name=self.name, content=cached, duration_ms=0.0)
        else:
            cache_key = None

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
            description = response.strip()
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

        if cache_key and image_data:
            _save_cache(cache_key, description, vision_model_id)

        return ToolResult.ok(name=self.name, content=description, duration_ms=0.0)

    # ------------------------------------------------------------------
    # 批量模式 — 一次 MIMO 调用处理多张图片
    # ------------------------------------------------------------------
    async def _describe_batch(self, ctx, images: list[dict], params: DescribeImageParams) -> ToolResult:
        indices = params.image_indices

        # 校验索引范围
        bad = [i for i in indices if i < 0 or i >= len(images)]
        if bad:
            return ToolResult.fail(
                name=self.name,
                error=f"图片序号 {bad} 超出范围 (共 {len(images)} 张)",
            )

        # 解析所有图片 → base64 data URI
        resolved: list[tuple[int, str]] = []
        for idx in indices:
            image = images[idx]
            source = image.get("src", "")
            data_uri = _resolve_image_source(source)
            resolved.append((idx, data_uri))

        vision_model_id = self._get_vision_model_id(ctx)

        # 构建多图消息：N 个 image_url + 1 个 text（图片在前，文本在后，MIMO 要求）
        content_array: list[dict] = []
        for idx, data_uri in resolved:
            content_array.append({
                "type": "image_url",
                "image_url": {"url": data_uri, "detail": "auto"},
            })
        content_array.append({"type": "text", "text": params.prompt})

        backend = None
        try:
            backend = LLMFactory.create(vision_model_id)
            messages = [{"role": "user", "content": content_array}]
            response = await backend.chat(messages)
            description = response.strip()
        except Exception as e:
            logger.exception("describe_image (batch) 失败")
            return ToolResult.fail(name=self.name, error=str(e))
        finally:
            if backend:
                try:
                    await backend.close()
                except Exception:
                    pass

        if not description:
            return ToolResult.fail(name=self.name, error="MIMO 返回空描述")

        return ToolResult.ok(name=self.name, content=description, duration_ms=0.0)

    # ------------------------------------------------------------------
    # 共享工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _get_vision_model_id(ctx) -> str:
        """读取 vision model ID，默认 mimo/mimo-v2.5。"""
        config = ctx.get("config")
        if config:
            vision_cfg = getattr(config, "vision", None)
            if vision_cfg:
                return getattr(vision_cfg, "model", "mimo/mimo-v2.5")
        return "mimo/mimo-v2.5"


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
