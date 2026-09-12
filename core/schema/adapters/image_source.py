"""图片源解析器 —— 把消息里的图片 src 解析为「LLM 可直接消费」的形态。

## 为什么独立成模块

此前该逻辑以 `resolve_image_source` 私有函数寄生在 `core/schema/adapters/message_adapter.py`
里，导致 vision 插件（`core/tools/vision/plugin.py`）必须**跨包导入私有符号**才能复用
（评估 `ARCH-INC-P1-03` §4「配套 API 收口」明确要求提取为独立的 Image Source Resolver）。

它本身也不属于「消息格式适配」：`message_adapter` 关心的是 Part ↔ OpenAI/Legacy 结构转换，
而这里关心的是**文件系统与附件的定位**（本地路径、`/static/*`、`/api/attachments/{id}`、
data URI、远程 URL）。两者变更原因不同，故拆分。

## 语义

| 输入形态 | 输出 |
|---|---|
| `data:` URI | 原样返回 |
| `/api/attachments/{id}` | 经 `AttachmentFileResolver` 定位落盘文件 → data URI；找不到则原样返回 |
| `http(s)://localhost...` / `http(s)://127.0.0.1...` | 尝试在 uploads/static 目录找同名文件 → data URI；否则原样返回 |
| `http(s)://其他` | 原样返回（交给模型服务端拉取） |
| `/` 开头的本地绝对路径 | 在 uploads/static 目录找同名文件 → data URI；否则原样返回 |
| 其他（相对路径等） | **原样返回，不做任何文件系统访问** |

设计约定：**解析失败一律原样返回**，不抛异常、不返回空——调用方（prompt 组装）不应因
某张图片定位不到而整体失败。

## 安全边界（CR-09，2026-09-12）

本模块**只允许从两处读取本地文件**，二者都是服务端可控位置：

1. **附件注册表**（`/api/attachments/{id}` → `AttachmentFileResolver`）；
2. **白名单目录内的裸文件名**（`_find_by_filename`，仅在 `data/uploads/images/` 与 `static/` 下按 basename 查找）。

**绝不接受客户端提供的任意路径。** 历史实现有一条「相对路径 → `Path(src).exists()` → 读文件」的兜底分支，
它构成**服务端任意相对路径读**：`POST /api/chat` / v1 WS 的 `content` 是客户端可控 `list[dict]`，
`src=".env"` 即可读出进程权限内任意文件并作为 image data URI 送往第三方 LLM + 落库可读回。
该分支已移除（见下方 `resolve_image_source`）。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from paths import ProjectPath

from core.schema.adapters import attachment_resolver as _attachment_resolver
from core.schema.part import ImageMime

_IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()
_STATIC_DIR = ProjectPath.STATIC.resolve()

# 附件稳定路径 /api/attachments/{attachment_id}（批次 7 消息关联 wire 格式）
_ATTACHMENT_SRC_RE = re.compile(r"^/api/attachments/([^/]+)$")

_MIME_BY_SUFFIX = {
    ".png": ImageMime.PNG, ".jpg": ImageMime.JPEG, ".jpeg": ImageMime.JPEG,
    ".gif": ImageMime.GIF, ".webp": ImageMime.WEBP,
}


def _to_data_uri(path: Path) -> str:
    """读取本地文件并编码为 data URI。

    Args:
        path: 本地图片文件路径。

    Returns:
        base64 编码的 data URI。
    """
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), ImageMime.PNG)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _find_by_filename(filename: str) -> Path | None:
    """在 uploads/static 目录中按文件名查找（两个目录顺序固定）。"""
    if not filename:
        return None
    for search_dir in (_IMAGES_DIR, _STATIC_DIR):
        candidate = search_dir / filename
        if candidate.exists():
            return candidate
    return None


def resolve_image_source(src: str) -> str:
    """把图片 src 解析为 data URI 或原样返回远程 URL。

    Args:
        src: 图片源——data URI / 附件稳定路径 / 本地路径 / 远程 URL。

    Returns:
        本地文件可定位时返回 base64 data URI，否则原样返回 `src`
        （解析失败不抛异常——prompt 组装不应因单张图定位失败而整体失败）。
    """
    if src.startswith("data:"):
        return src

    match = _ATTACHMENT_SRC_RE.match(src)
    if match:
        local_path = _attachment_resolver.resolve(match.group(1))
        if local_path is not None and local_path.is_file():
            return _to_data_uri(local_path)
        return src

    if src.startswith("http://") or src.startswith("https://"):
        # 仅本机 URL 才尝试映射到落盘文件；外部 URL 交给模型服务端拉取
        if "://localhost" in src or "://127.0.0.1" in src:
            found = _find_by_filename(src.rsplit("/", 1)[-1])
            if found is not None:
                return _to_data_uri(found)
        return src

    if src.startswith("/"):
        found = _find_by_filename(src.rsplit("/", 1)[-1])
        if found is not None:
            return _to_data_uri(found)
        return src

    # CR-09（2026-09-12）：此处原有「相对路径 → Path(src).exists() → 读文件」兜底分支，
    # 构成服务端任意相对路径读（src=".env" / "../../etc/passwd" 均可）。客户端可控
    # content 会一路走到这里，故**必须**保持「非白名单形态一律原样返回、不碰文件系统」。
    # 需要读本地文件请走 /api/attachments/{id}（附件注册表）或白名单目录内的裸文件名。
    return src
