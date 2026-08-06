"""AttachmentFileResolver — 附件 ID → 磁盘路径的进程内映射。

批次 7（spec §4.5，plan review Minor 4 定一，不落 storage）：`content_to_openai`
的 `_resolve_image_source`（message_adapter.py）把本地 src 转 data URI 喂 LLM，
需要知道附件落盘位置。Mongo 离线时附件记录不可查，故用模块级 dict 缓存
`attachment_id → (kind, stored_name)`，据此定位 `data/uploads/{kind}/{stored_name}`。

值含 kind（四轮 review Important #2）——Mongo 离线兜底时据此定位目录。

维护者：
- 上传/消息关联时 register(attachment_id, kind, stored_name)
- 软删/GC 清理时 unregister(attachment_id)
- 启动 GC 扫描时 load(records) 预热

同步安全：纯 dict 读写，GIL 内原子，无需锁。
"""

from pathlib import Path

from paths import ProjectPath

# attachment_id → (kind, stored_name)
_registry: dict[str, tuple[str, str]] = {}


def register(attachment_id: str, kind: str, stored_name: str) -> None:
    """登记附件 ID → (kind, stored_name) 映射。"""
    _registry[attachment_id] = (kind, stored_name)


def unregister(attachment_id: str) -> None:
    """移除附件 ID 映射（软删/GC 清理时调用）。未登记时静默。"""
    _registry.pop(attachment_id, None)


def load(records: list[dict]) -> None:
    """从附件记录批量预热映射（启动 GC 扫描时调用）。

    Args:
        records: attachments 集合记录列表；缺 attachment_id/kind/stored_name
            的记录跳过。
    """
    for rec in records:
        aid = rec.get("attachment_id")
        kind = rec.get("kind")
        stored_name = rec.get("stored_name")
        if aid and kind and stored_name:
            _registry[aid] = (kind, stored_name)


def resolve(attachment_id: str) -> Path | None:
    """返回附件落盘路径 `data/uploads/{kind}/{stored_name}`，未登记返回 None。

    Args:
        attachment_id: 附件 ID（att_ 前缀）。

    Returns:
        基于 ProjectPath.DATA_UPLOADS 的绝对 Path，未登记返回 None。
    """
    entry = _registry.get(attachment_id)
    if entry is None:
        return None
    kind, stored_name = entry
    return ProjectPath.DATA_UPLOADS.resolve() / kind / stored_name
